"""Shared LLM client (spec 00 F0.6).

Every model call in the project goes through `LLMClient`, which logs it to `llm_calls`
with tokens, latency and cost. Providers sit behind `LLMProvider`; `StubProvider` plays
back a script so tests (including agent tests) are deterministic and make no network
calls.
"""

from __future__ import annotations

import json
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from guardian.common.db import SessionFactory, session_scope
from guardian.common.models import LLMCall

T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True)
class ToolSpec:
    """A tool the model may call. `parameters` is a JSON Schema object."""

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class Message:
    """Provider-neutral chat message. `role` is user, assistant or tool."""

    role: str
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None


@dataclass(frozen=True)
class LLMRequest:
    model: str
    system: str
    messages: tuple[Message, ...]
    schema: dict[str, Any] | None = None
    tools: tuple[ToolSpec, ...] = ()


@dataclass(frozen=True)
class LLMResponse:
    text: str = ""
    data: dict[str, Any] | None = None  # structured output, when a schema was given
    tool_calls: tuple[ToolCall, ...] = ()
    tokens_in: int = 0
    tokens_out: int = 0
    stop_reason: str = "end"


class LLMProvider(Protocol):
    name: str

    def complete(self, request: LLMRequest) -> LLMResponse: ...


class LLMError(Exception):
    """Base class for LLM client errors."""


class LLMNotConfiguredError(LLMError):
    """Raised when a model call is attempted with no provider configured."""


class LLMOutputError(LLMError):
    """The model's structured output did not match the schema."""

    def __init__(self, message: str, raw: dict[str, Any] | str | None) -> None:
        super().__init__(message)
        self.raw = raw


ScriptItem = LLMResponse | Callable[[LLMRequest], LLMResponse]


class StubProvider:
    """Plays back scripted responses in order and records every request it received."""

    name = "stub"

    def __init__(self, script: Iterable[ScriptItem] = ()) -> None:
        self._script: deque[ScriptItem] = deque(script)
        self.requests: list[LLMRequest] = []

    def push(self, *items: ScriptItem) -> None:
        self._script.extend(items)

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if not self._script:
            raise LLMError("StubProvider script is exhausted: the test expected fewer calls.")
        item = self._script.popleft()
        return item(request) if callable(item) else item


class UnconfiguredProvider:
    """Default provider. Fails loudly instead of pretending a model answered."""

    name = "none"

    def complete(self, request: LLMRequest) -> LLMResponse:
        raise LLMNotConfiguredError(
            "No LLM provider is configured. Set GUARDIAN_LLM_PROVIDER and GUARDIAN_LLM_MODEL."
        )


@dataclass(frozen=True)
class Pricing:
    """Price in USD per million tokens."""

    input_per_mtok: float = 0.0
    output_per_mtok: float = 0.0

    def cost(self, tokens_in: int, tokens_out: int) -> float:
        return (tokens_in * self.input_per_mtok + tokens_out * self.output_per_mtok) / 1_000_000


@dataclass
class LLMClient:
    provider: LLMProvider
    sessions: SessionFactory
    default_model: str
    pricing: Mapping[str, Pricing] = field(default_factory=dict)
    clock: Callable[[], float] = time.perf_counter

    def chat(
        self,
        *,
        purpose: str,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] = (),
        model: str | None = None,
        run_id: int | None = None,
    ) -> LLMResponse:
        """One model turn, optionally with tools. The caller runs the tool loop."""
        request = LLMRequest(
            model=model or self.default_model,
            system=system,
            messages=tuple(messages),
            tools=tuple(tools),
        )
        return self._call(request, purpose=purpose, run_id=run_id)

    def structured(
        self,
        *,
        purpose: str,
        system: str,
        messages: Sequence[Message],
        schema: type[T],
        model: str | None = None,
        run_id: int | None = None,
    ) -> T:
        """One model turn whose output must validate against `schema`.

        Raises `LLMOutputError` when it does not. The call is logged either way, so a
        caller that retries leaves one row per attempt.
        """
        request = LLMRequest(
            model=model or self.default_model,
            system=system,
            messages=tuple(messages),
            schema=schema.model_json_schema(),
        )
        response = self._call(request, purpose=purpose, run_id=run_id)
        raw: dict[str, Any] | str | None = response.data
        if raw is None:
            try:
                raw = json.loads(response.text)
            except json.JSONDecodeError as exc:
                raise LLMOutputError("Model output is not JSON.", response.text) from exc
        try:
            return schema.model_validate(raw)
        except ValidationError as exc:
            raise LLMOutputError(str(exc), raw) from exc

    def _call(self, request: LLMRequest, *, purpose: str, run_id: int | None) -> LLMResponse:
        started = self.clock()
        try:
            response = self.provider.complete(request)
        except Exception as exc:
            self._log(request, purpose, run_id, started, output={"error": repr(exc)})
            raise
        self._log(
            request,
            purpose,
            run_id,
            started,
            output={
                "text": response.text,
                "data": response.data,
                "tool_calls": [asdict(call) for call in response.tool_calls],
                "stop_reason": response.stop_reason,
            },
            tokens_in=response.tokens_in,
            tokens_out=response.tokens_out,
        )
        return response

    def _log(
        self,
        request: LLMRequest,
        purpose: str,
        run_id: int | None,
        started: float,
        *,
        output: dict[str, Any],
        tokens_in: int = 0,
        tokens_out: int = 0,
    ) -> None:
        price = self.pricing.get(request.model, Pricing())
        row = LLMCall(
            run_id=run_id,
            purpose=purpose,
            provider=self.provider.name,
            model=request.model,
            input={
                "system": request.system,
                "messages": [asdict(message) for message in request.messages],
                "schema": request.schema,
                "tools": [tool.name for tool in request.tools],
            },
            output=output,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=(self.clock() - started) * 1000,
            cost=price.cost(tokens_in, tokens_out),
        )
        with session_scope(self.sessions) as session:
            session.add(row)
