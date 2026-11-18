"""Turn a release note into checked change records (spec 02).

`extract_note` is the core: prompt, at most two attempts per part (F2.4), the checks,
de-duplication. It needs only an LLM client, so the eval harness uses it directly.

`Extractor` wraps it for stored documents: one extraction per (package, version,
revision, prompt version, model), cached in the blob store with everything that was
rejected and why (F2.7, F2.10), and the accepted records written to `change_records`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from sqlalchemy import delete

from guardian.common.blobstore import BlobStore
from guardian.common.db import SessionFactory, session_scope
from guardian.common.llm import LLMClient, LLMOutputError, Message
from guardian.common.log import get_logger
from guardian.common.models import ChangeRecord
from guardian.extract.evidence import quote_in_note
from guardian.extract.prompt import (
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    note_message,
    retry_message,
    split_note,
)
from guardian.extract.schema import NEVER_FIXABLE, ExtractedChange, NoteChange, NoteExtraction
from guardian.watch.documents import DocumentStore, StoredDocument
from guardian.watch.sources import SDK_SOURCES, SdkSource, source_for_package

log = get_logger(__name__)

EXTRACTED = "extracted"
FAILED = "extraction-failed"

# Why a record the model returned was not kept.
EVIDENCE_NOT_IN_NOTE = "evidence-not-in-note"
DUPLICATE = "duplicate"

PURPOSE = "extraction"


class ExtractionUnavailable(Exception):
    """No LLM provider is configured, so extraction cannot run at all."""


@dataclass(frozen=True)
class Rejected:
    change: NoteChange
    reason: str


@dataclass
class NoteResult:
    status: str  # EXTRACTED | FAILED
    records: list[ExtractedChange] = field(default_factory=list)
    rejected: list[Rejected] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    parts: int = 1
    model: str = ""

    @property
    def breaking(self) -> bool:
        return self.status == EXTRACTED and bool(self.records)

    def keys(self) -> set[tuple[str, str, str | None, str | None]]:
        return {record.key() for record in self.records}

    def to_json(self) -> str:
        return json.dumps(
            {
                "prompt_version": PROMPT_VERSION,
                "status": self.status,
                "model": self.model,
                "parts": self.parts,
                "records": [record.model_dump(mode="json") for record in self.records],
                "rejected": [
                    {"reason": item.reason, "change": item.change.model_dump(mode="json")}
                    for item in self.rejected
                ],
                "errors": self.errors,
            },
            indent=1,
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, text: str) -> NoteResult:
        data = json.loads(text)
        return cls(
            status=data["status"],
            records=[ExtractedChange.model_validate(item) for item in data["records"]],
            rejected=[
                Rejected(NoteChange.model_validate(item["change"]), item["reason"])
                for item in data["rejected"]
            ],
            errors=data["errors"],
            parts=data["parts"],
            model=data["model"],
        )


def llm_available(llm: LLMClient) -> bool:
    return llm.provider.name != "none"


def _ask(
    llm: LLMClient, package: str, version: str, part: str, label: tuple[int, int] | None
) -> NoteExtraction | str:
    """At most two attempts (F2.4). Returns the output, or the last validation error."""
    messages = [note_message(package, version, part, label)]
    error = ""
    for attempt in (1, 2):
        try:
            return llm.structured(
                purpose=PURPOSE, system=SYSTEM_PROMPT, messages=messages, schema=NoteExtraction
            )
        except LLMOutputError as exc:
            error = str(exc)
            log.warning(
                "extraction output invalid",
                extra={"fields": {"package": package, "version": version, "attempt": attempt}},
            )
            raw = exc.raw if isinstance(exc.raw, str) else json.dumps(exc.raw, ensure_ascii=False)
            messages = [
                *messages,
                Message(role="assistant", content=(raw or "")[:8000]),
                retry_message(error),
            ]
    return error


def extract_note(
    llm: LLMClient, provider: str, package: str, version: str, note: str
) -> NoteResult:
    """Extract and check the change records of one release note."""
    if not llm_available(llm):
        raise ExtractionUnavailable("no LLM provider is configured (GUARDIAN_LLM_PROVIDER)")
    parts = split_note(note)
    result = NoteResult(status=EXTRACTED, parts=len(parts), model=llm.default_model)
    seen: set[tuple[str, str, str | None, str | None]] = set()

    for index, part in enumerate(parts, 1):
        label = (index, len(parts)) if len(parts) > 1 else None
        answer = _ask(llm, package, version, part, label)
        if isinstance(answer, str):
            # Two invalid answers: the whole note fails and no records are kept (F2.4).
            return NoteResult(
                status=FAILED,
                errors=[f"part {index}: {answer[:1000]}"],
                parts=len(parts),
                model=result.model,
            )
        for change in answer.changes:
            if not quote_in_note(change.evidence_quote, note):
                result.rejected.append(Rejected(change, EVIDENCE_NOT_IN_NOTE))
                continue
            record = ExtractedChange(
                provider=provider,
                package=package,
                version=version,
                change_type=change.change_type,
                qualified_symbol=change.qualified_symbol.strip().strip("`").removesuffix("()"),
                affected_parameter=change.affected_parameter,
                replacement=change.replacement,
                # The model's opinion is overridden where it cannot be right (F2.3).
                fixable=change.fixable and change.change_type not in NEVER_FIXABLE,
                evidence_quote=change.evidence_quote,
                migration_hint=change.migration_hint,
            )
            if record.key() in seen:
                result.rejected.append(Rejected(change, DUPLICATE))
                continue
            seen.add(record.key())
            result.records.append(record)
    return result


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9.-]+", "-", name).strip("-") or "unset"


class Extractor:
    """Cached extraction of stored documents, with the accepted records in the database."""

    def __init__(
        self,
        llm: LLMClient,
        sessions: SessionFactory,
        blobs: BlobStore,
        sources: tuple[SdkSource, ...] = SDK_SOURCES,
    ) -> None:
        self._llm = llm
        self._sessions = sessions
        self._blobs = blobs
        self._sources = sources
        self.documents = DocumentStore(sessions, blobs)

    @property
    def available(self) -> bool:
        return llm_available(self._llm)

    def cache_key(self, document: StoredDocument) -> str:
        model = _safe(self._llm.default_model)
        return (
            f"sdk/{document.package}/extraction/p{PROMPT_VERSION}-{model}/"
            f"{document.version}/r{document.revision}.json"
        )

    def cached(self, document: StoredDocument) -> NoteResult | None:
        key = self.cache_key(document)
        if not self._blobs.exists(key):
            return None
        result = NoteResult.from_json(self._blobs.get(key).decode())
        return result if result.status == EXTRACTED else None

    def extract(self, document: StoredDocument) -> NoteResult:
        """Extract a stored note once; later calls return the cached result (F2.7)."""
        cached = self.cached(document)
        if cached is not None:
            return cached
        source = source_for_package(document.package, self._sources)
        provider = source.provider if source is not None else document.package
        result = extract_note(
            self._llm, provider, document.package, document.version, self.documents.text(document)
        )
        # Kept for audit either way; only a success is reused (a failure is retried).
        self._blobs.put(self.cache_key(document), result.to_json().encode())
        if result.status == EXTRACTED:
            self._store(document, result)
        log.info(
            "note extracted",
            extra={
                "fields": {
                    "package": document.package,
                    "version": document.version,
                    "revision": document.revision,
                    "status": result.status,
                    "records": len(result.records),
                    "rejected": len(result.rejected),
                    "parts": result.parts,
                }
            },
        )
        return result

    def _store(self, document: StoredDocument, result: NoteResult) -> None:
        with session_scope(self._sessions) as session:
            session.execute(
                delete(ChangeRecord).where(
                    ChangeRecord.document_id == document.id, ChangeRecord.origin == "notes"
                )
            )
            for record in result.records:
                session.add(
                    ChangeRecord(
                        document_id=document.id,
                        layer="sdk",
                        origin="notes",
                        package=record.package,
                        version=record.version,
                        change_type=record.change_type.value,
                        qualified_symbol=record.qualified_symbol,
                        affected_parameter=record.affected_parameter,
                        replacement=record.replacement,
                        fixable=record.fixable,
                        evidence_quote=record.evidence_quote,
                        migration_hint=record.migration_hint,
                    )
                )

    def previous(self, document: StoredDocument) -> NoteResult | None:
        """The extraction of the revision before `document`, for edit comparison."""
        if document.revision <= 1:
            return None
        source = source_for_package(document.package, self._sources)
        if source is None:
            return None
        earlier = self.documents.revision(source, document.version, document.revision - 1)
        return self.extract(earlier) if earlier is not None else None
