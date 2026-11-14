"""CHANGELOG.md fallback: pointer detection and section extraction. Covers F1.14."""

from __future__ import annotations

from pathlib import Path

import pytest

from guardian.watch.changelog import EMPTY_BODY, POINTER_BODY, extract_section, pointer_reason

FIXTURES = Path(__file__).parent / "fixtures"

# The real body of every stripe-python GitHub release from v16 on.
STRIPE_POINTER = (
    "See [the changelog](https://github.com/stripe/stripe-python/blob/master/"
    "CHANGELOG.md#16-0-0) for the full release notes."
)

# The shape of a short but real openai-python release note.
OPENAI_SHORT = """## 1.2.1 (2023-11-09)

Full Changelog: [v1.2.0...v1.2.1](https://github.com/openai/openai-python/compare/v1.2.0...v1.2.1)

### Chores

* **internal:** fix typo ([#1](https://github.com/openai/openai-python/issues/1))
"""


@pytest.fixture(scope="module")
def stripe_changelog() -> str:
    return (FIXTURES / "stripe_changelog_excerpt.md").read_text(encoding="utf-8")


# --- pointer detection --------------------------------------------------------


@pytest.mark.parametrize("body", ["", "   \n\t\n"])
def test_empty_bodies(body: str) -> None:
    assert pointer_reason(body) == EMPTY_BODY


def test_stripe_link_only_body_is_a_pointer() -> None:
    assert pointer_reason(STRIPE_POINTER) == POINTER_BODY


def test_short_real_note_with_a_changelog_link_is_not_a_pointer() -> None:
    assert pointer_reason(OPENAI_SHORT) is None


def test_short_note_without_links_is_a_real_note() -> None:
    assert pointer_reason("Fixes a crash when the API key is missing.") is None


def test_long_text_that_mentions_the_changelog_is_a_real_note() -> None:
    body = "See the changelog at https://example.com/changelog. " + "Details. " * 60
    assert pointer_reason(body) is None


# --- section extraction -------------------------------------------------------


def test_stripe_16_section_holds_the_reversal_rename(stripe_changelog: str) -> None:
    section = extract_section(stripe_changelog, "16.0.0")
    assert section is not None
    assert section.startswith('## <a id="16-0-0"></a>16.0.0 - 2026-09-30')
    assert "Rename `stripe.Reversal` to `stripe.TransferReversal`" in section
    # It stops at the next version heading and does not swallow its neighbours.
    assert "16.1.0" not in section
    assert "15.6.1" not in section


def test_section_ends_at_the_next_version(stripe_changelog: str) -> None:
    section = extract_section(stripe_changelog, "17.0.0")
    assert section is not None
    assert "Omit API keys from pickled StripeObjects" in section
    assert "16.1.0" not in section


def test_last_section_runs_to_the_end(stripe_changelog: str) -> None:
    section = extract_section(stripe_changelog, "15.6.1")
    assert section is not None and section.startswith("## ")


def test_unknown_version_returns_none(stripe_changelog: str) -> None:
    assert extract_section(stripe_changelog, "99.0.0") is None
    assert extract_section(stripe_changelog, "6.0.0") is None  # not a suffix match of 16.0.0


def test_version_must_match_as_a_whole_token() -> None:
    changelog = "## 16.0.0b1\n\n- beta\n\n## 16.0.0\n\n- final\n\n## 6.0.0\n\n- old\n"
    assert extract_section(changelog, "16.0.0") == "## 16.0.0\n\n- final\n"
    assert extract_section(changelog, "16.0.0b1") == "## 16.0.0b1\n\n- beta\n"
    assert extract_section(changelog, "6.0.0") == "## 6.0.0\n\n- old\n"
    assert extract_section(changelog, "0.0") is None


def test_subheadings_stay_inside_their_section() -> None:
    changelog = "# Changelog\n\n## 2.0.0 (2025-01-01)\n\n### Breaking\n\n- x\n\n## 1.9.0\n\n- y\n"
    section = extract_section(changelog, "2.0.0")
    assert section == "## 2.0.0 (2025-01-01)\n\n### Breaking\n\n- x\n"
