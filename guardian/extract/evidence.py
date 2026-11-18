"""The evidence check (spec 02 F2.5).

A record is kept only if its evidence quote appears in the release note. Both sides
are normalised the same way first, so formatting the model drops or adds does not
matter, but the words must be there:

- the ⚠ / ⚠️ marker is removed;
- Markdown emphasis and code markers (`*`, `_`, backticks) are removed;
- list and heading markers at the start of a line are removed;
- runs of whitespace become one space.

Comparison is case-sensitive. Anything looser would weaken the guard, which exists to
stop a model from inventing a change and attributing it to the provider.
"""

from __future__ import annotations

import re

# Shorter normalised quotes say too little to prove anything ("the", "removed").
MIN_QUOTE_CHARS = 15

_WARNING = re.compile("⚠️?")
_LINE_MARKER = re.compile(r"(?m)^[ \t]*(?:[*\-+][ \t]+|\d+[.)][ \t]+|#{1,6}[ \t]+|>[ \t]?)+")
_EMPHASIS = re.compile(r"[*_`]")
_SPACE = re.compile(r"\s+")


def normalise(text: str) -> str:
    text = _WARNING.sub("", text)
    text = _LINE_MARKER.sub("", text)
    text = _EMPHASIS.sub("", text)
    return _SPACE.sub(" ", text).strip()


def quote_in_note(quote: str, note: str) -> bool:
    """Is `quote` (normalised) a substring of `note` (normalised)?"""
    wanted = normalise(quote)
    return len(wanted) >= MIN_QUOTE_CHARS and wanted in normalise(note)
