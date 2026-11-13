"""Shared LLM client. Covers F0.6."""

from __future__ import annotations

import pytest
from pydantic import BaseModel
from sqlalchemy import select

from guardian.common import db
from guardian.common.context import AppContext
from guardian.common.llm import (
    LLMClient,
    LLMError,
    LLMNotConfiguredError,
    LLMOutputError,
    LLMRequest,
    LLMResponse,
    Message,
    Pricing,
    StubProvider,
    ToolCall,
    ToolSpec,
    UnconfiguredProvider,
)
from guardian.common.models import LLMCall


class Change(BaseModel):
    symbol: str
    breaking: bool


def _calls(sessions: db.SessionFactory) -> list[LLMCall]:
    with db.session_scope(sessions) as session:
        return list(session.scalars(select(LLMCall).order_by(LLMCall.id)))


def test_structured_output_is_validated_and_logged(ctx: AppContext, stub: StubProvider) -> None:
    stub.push(
        LLMResponse(
            data={"symbol": "Client.create", "breaking": True}, tokens_in=120, tokens_out=30
        )
    )

    result = ctx.llm.structured(
        purpose="extraction",
        system="Extract changes.",
        messages=[Message(role="user", content="release note text")],
        schema=Change,
    )

    assert result == Change(symbol="Client.create", breaking=True)
    assert stub.requests[0].schema == Change.model_json_schema()
    (row,) = _calls(ctx.sessions)
    assert row.purpose == "extraction"
    assert row.provider == "stub"
    assert row.model == "stub-model"
    assert (row.tokens_in, row.tokens_out) == (120, 30)
    assert row.latency_ms >= 0
    assert row.input["messages"][0]["content"] == "release note text"
    assert row.output["data"] == {"symbol": "Client.create", "breaking": True}


def test_structured_output_accepts_json_text(ctx: AppContext, stub: StubProvider) -> None:
    stub.push(LLMResponse(text='{"symbol": "a.b", "breaking": false}'))
    result = ctx.llm.structured(purpose="p", system="s", messages=[], schema=Change)
    assert result.symbol == "a.b"


def test_invalid_structured_output_raises_and_is_still_logged(
    ctx: AppContext, stub: StubProvider
) -> None:
    stub.push(LLMResponse(data={"symbol": "Client.create"}))  # `breaking` is missing

    with pytest.raises(LLMOutputError) as excinfo:
        ctx.llm.structured(purpose="extraction", system="s", messages=[], schema=Change)

    assert excinfo.value.raw == {"symbol": "Client.create"}
    assert len(_calls(ctx.sessions)) == 1


def test_non_json_text_raises_output_error(ctx: AppContext, stub: StubProvider) -> None:
    stub.push(LLMResponse(text="Sure! Here are the changes."))
    with pytest.raises(LLMOutputError):
        ctx.llm.structured(purpose="p", system="s", messages=[], schema=Change)


def test_tool_call_is_returned_and_logged(ctx: AppContext, stub: StubProvider) -> None:
    read_file = ToolSpec(
        name="read_file",
        description="Read lines of a file.",
        parameters={"type": "object", "properties": {"path": {"type": "string"}}},
    )
    call = ToolCall(id="call_1", name="read_file", arguments={"path": "app/llm.py"})
    stub.push(LLMResponse(tool_calls=(call,), tokens_in=50, tokens_out=10, stop_reason="tool_use"))

    response = ctx.llm.chat(
        purpose="agent-step",
        system="Fix the call site.",
        messages=[Message(role="user", content="finding")],
        tools=[read_file],
    )

    assert response.tool_calls == (call,)
    assert stub.requests[0].tools == (read_file,)
    (row,) = _calls(ctx.sessions)
    assert row.input["tools"] == ["read_file"]
    assert row.output["tool_calls"] == [
        {"id": "call_1", "name": "read_file", "arguments": {"path": "app/llm.py"}}
    ]
    assert row.output["stop_reason"] == "tool_use"


def test_scripted_stub_drives_a_multi_step_tool_loop(ctx: AppContext, stub: StubProvider) -> None:
    """The shape spec 11 relies on: a tool result goes back in, the model finishes."""

    def second_turn(request: LLMRequest) -> LLMResponse:
        assert request.messages[-1].role == "tool"
        return LLMResponse(text="done")

    stub.push(
        LLMResponse(tool_calls=(ToolCall(id="c1", name="get_change", arguments={}),)),
        second_turn,
    )

    messages = [Message(role="user", content="finding")]
    first = ctx.llm.chat(purpose="agent-step", system="s", messages=messages)
    messages.append(Message(role="assistant", tool_calls=first.tool_calls))
    messages.append(Message(role="tool", content="change record", tool_call_id="c1"))
    second = ctx.llm.chat(purpose="agent-step", system="s", messages=messages)

    assert second.text == "done"
    assert len(_calls(ctx.sessions)) == 2


def test_cost_is_computed_from_pricing(sessions: db.SessionFactory) -> None:
    stub = StubProvider([LLMResponse(text="ok", tokens_in=1_000_000, tokens_out=500_000)])
    client = LLMClient(
        provider=stub,
        sessions=sessions,
        default_model="m",
        pricing={"m": Pricing(input_per_mtok=3.0, output_per_mtok=15.0)},
    )
    client.chat(purpose="p", system="s", messages=[])
    assert _calls(sessions)[0].cost == pytest.approx(3.0 + 7.5)


def test_provider_failure_is_logged_and_reraised(ctx: AppContext) -> None:
    with pytest.raises(LLMError, match="exhausted"):
        ctx.llm.chat(purpose="p", system="s", messages=[])  # empty script
    (row,) = _calls(ctx.sessions)
    assert "error" in row.output


def test_unconfigured_provider_fails_loudly(sessions: db.SessionFactory) -> None:
    client = LLMClient(provider=UnconfiguredProvider(), sessions=sessions, default_model="unset")
    with pytest.raises(LLMNotConfiguredError):
        client.chat(purpose="p", system="s", messages=[])
