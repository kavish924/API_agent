"""`CHANGELOG.md` fallback (spec 01 F1.14).

Some projects publish GitHub releases whose body is empty or only points elsewhere.
From v16 on, every stripe-python release body is one line linking to `CHANGELOG.md`.
For those, the matching changelog section is the real release note.
"""

from __future__ import annotations

import re

EMPTY_BODY = "empty-body"
POINTER_BODY = "pointer-body"

_HEADING = re.compile(r"^(#{1,4})\s+(.*)$")
_TAG = re.compile(r"<[^>]+>")
_VERSION_LIKE = re.compile(r"(?<![\w.])\d+\.\d+(?:\.\d+)?")
_LINK = re.compile(r"https?://|\]\(")
_MENTIONS_NOTES = re.compile(r"change\s?log|release notes", re.IGNORECASE)
_BULLET = re.compile(r"^\s*[*\-+]\s+\S", re.MULTILINE)

POINTER_MAX_CHARS = 300


def pointer_reason(body: str) -> str | None:
    """Why a release body is not a usable note, or None when it is one.

    A pointer body is short, links somewhere, mentions a changelog or release notes,
    and lists no changes of its own. The no-bullets test keeps short but real notes
    (openai-python's always carry a "Full Changelog" link) from being misread.
    """
    text = body.strip()
    if not text:
        return EMPTY_BODY
    if (
        len(text) <= POINTER_MAX_CHARS
        and _LINK.search(text)
        and _MENTIONS_NOTES.search(text)
        and not _BULLET.search(text)
    ):
        return POINTER_BODY
    return None


def extract_section(changelog: str, version: str) -> str | None:
    """The changelog section for `version`: its heading up to the next version heading.

    The heading must contain the version as a whole token, so `6.0.0` does not match
    `16.0.0` and `16.0.0` does not match `16.0.0b1`. Handles Stripe's
    `## <a id="16-0-0"></a>16.0.0 - 2026-09-30`.
    """
    wanted = re.compile(r"(?<![\w.])" + re.escape(version) + r"(?!\.?\w)")
    lines = changelog.splitlines()
    start: int | None = None
    level = 0
    for index, line in enumerate(lines):
        match = _HEADING.match(line)
        if not match:
            continue
        title = _TAG.sub("", match.group(2))
        if start is None:
            if wanted.search(title):
                start, level = index, len(match.group(1))
        elif len(match.group(1)) <= level and _VERSION_LIKE.search(title):
            return "\n".join(lines[start:index]).strip() + "\n"
    if start is None:
        return None
    return "\n".join(lines[start:]).strip() + "\n"
