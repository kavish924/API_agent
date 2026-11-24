"""Scan a repository for the call sites a run's changes affect (spec 04).

Two passes over the files that could matter:

1. files that mention the SDK are parsed, and their module-level clients recorded so
   other modules can import them (F4.4);
2. those files, plus files that import a module holding a client, are matched against
   the change index.

A file that fails to parse is skipped with a warning (F4.9). No findings means the run
ends quietly (F4.8).
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import libcst as cst

from guardian.apidiff.reconcile import GradedChange
from guardian.apidiff.surface import Surface
from guardian.common.log import get_logger
from guardian.scan import rules as R
from guardian.scan.analyzer import Context, FileAnalyzer, Finding
from guardian.scan.files import SourceFile, mentions, module_names, python_files

log = get_logger(__name__)


@dataclass
class ScanResult:
    findings: list[Finding] = field(default_factory=list)
    files_total: int = 0  # Python files after ignore rules
    files_parsed: int = 0
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (file, reason)
    seconds: float = 0.0
    unmatchable_changes: int = 0  # changes no code shape can show (e.g. behaviour)

    def by_tier(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for finding in self.findings:
            counts[finding.tier] = counts.get(finding.tier, 0) + 1
        return dict(sorted(counts.items()))

    def sites(self) -> int:
        """Distinct places in the code, however many changes point at each."""
        return len({(f.file, f.line, f.column) for f in self.findings})


def scan_repo(
    root: Path,
    package: str,
    surface: Surface,
    changes: Sequence[GradedChange],
    *,
    literals: Sequence[R.LiteralQuery] = (),
    families: Sequence[tuple[str, str]] | None = None,
) -> ScanResult:
    """Find the code in `root` that `changes` (graded against `surface`) would break."""
    started = time.perf_counter()
    result = ScanResult()
    index = R.build_index(changes, surface)
    result.unmatchable_changes = len(index.unmatchable)
    ctx = Context(
        package=package,
        surface=surface,
        index=index,
        change_confidence=[change.confidence for change in changes],
        literals=literals,
        families=families if families is not None else R.FAMILIES.get(package, ()),
    )

    files = list(python_files(root))
    result.files_total = len(files)
    if index.empty and not literals:
        result.seconds = time.perf_counter() - started
        return result

    texts: dict[str, str] = {}
    first: list[SourceFile] = []
    for source in files:
        try:
            text = source.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            result.skipped.append((source.relative, f"unreadable: {type(exc).__name__}"))
            continue
        texts[source.relative] = text
        if mentions(text, {package}):
            first.append(source)

    parsed: dict[str, tuple[FileAnalyzer, object]] = {}
    for source in first:
        loaded = _parse(ctx, source, texts[source.relative], result)
        if loaded is None:
            continue
        analyzer, wrapper = loaded
        exported = analyzer.exports(wrapper)  # type: ignore[arg-type]
        if exported:
            for name in module_names(source.relative):
                ctx.exports[name] = exported
        parsed[source.relative] = loaded

    # Files that import a module holding a client, without naming the SDK themselves.
    leaves = {name.rsplit(".", 1)[-1] for name in ctx.exports}
    exported_names = {n for names in ctx.exports.values() for n in names}
    for source in files:
        if source.relative in parsed or source.relative not in texts:
            continue
        text = texts[source.relative]
        if leaves and mentions(text, leaves) and mentions(text, exported_names | leaves):
            loaded = _parse(ctx, source, text, result)
            if loaded is not None:
                parsed[source.relative] = loaded

    for relative, (analyzer, wrapper) in parsed.items():
        try:
            result.findings.extend(analyzer.run(wrapper))  # type: ignore[arg-type]
        except Exception as exc:  # a bug on one odd file must not lose the others
            log.warning(
                "scan failed on a file", extra={"fields": {"file": relative, "error": repr(exc)}}
            )
            result.skipped.append((relative, f"analysis error: {type(exc).__name__}"))

    result.files_parsed = len(parsed)
    result.findings.sort(key=lambda f: (f.file, f.line, f.column, f.change or -1, f.query or -1))
    result.seconds = time.perf_counter() - started
    log.info(
        "scan finished",
        extra={
            "fields": {
                "files": result.files_total,
                "parsed": result.files_parsed,
                "skipped": len(result.skipped),
                "findings": len(result.findings),
                "seconds": round(result.seconds, 2),
            }
        },
    )
    return result


def _parse(
    ctx: Context, source: SourceFile, text: str, result: ScanResult
) -> tuple[FileAnalyzer, object] | None:
    try:
        return FileAnalyzer.parse(ctx, source.relative, module_names(source.relative), text)
    except cst.ParserSyntaxError as exc:
        log.warning(
            "file skipped: does not parse",
            extra={"fields": {"file": source.relative, "line": exc.raw_line}},
        )
        result.skipped.append((source.relative, f"syntax error at line {exc.raw_line}"))
    except RecursionError:
        result.skipped.append((source.relative, "too deeply nested to parse"))
    return None
