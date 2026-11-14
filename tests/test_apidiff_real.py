"""Regression against real SDK wheels (spec 03 acceptance criteria).

Skipped unless GUARDIAN_TEST_WHEELS points at a directory of unpacked wheels named
`<package>-<version>/`, so the normal suite and CI never depend on PyPI. To run it:

    GUARDIAN_TEST_WHEELS=/path/to/wheels python -m pytest tests/test_apidiff_real.py -s

The wheels are stripe 15.6.2 and 16.0.0, and openai 3.0.0 and 3.49.0.
"""

from __future__ import annotations

import os
from collections import Counter
from pathlib import Path

import pytest

from guardian.apidiff import compare as cmp
from guardian.apidiff.compare import ApiDiff, compare
from guardian.apidiff.surface import Surface, build_surface

WHEELS = os.environ.get("GUARDIAN_TEST_WHEELS")

pytestmark = pytest.mark.skipif(not WHEELS, reason="GUARDIAN_TEST_WHEELS is not set")


def _surface(package: str, version: str) -> Surface:
    path = Path(WHEELS or "") / f"{package}-{version}"
    if not path.is_dir():
        pytest.skip(f"{path} not found")
    return build_surface(path, package, version)


@pytest.fixture(scope="module")
def stripe() -> tuple[Surface, Surface, ApiDiff]:
    old, new = _surface("stripe", "15.6.2"), _surface("stripe", "16.0.0")
    return old, new, compare(old, new)


@pytest.fixture(scope="module")
def openai() -> tuple[Surface, Surface, ApiDiff]:
    old, new = _surface("openai", "3.0.0"), _surface("openai", "3.49.0")
    return old, new, compare(old, new)


def test_stripe_16_documented_breaks_are_all_found(
    stripe: tuple[Surface, Surface, ApiDiff],
) -> None:
    """griffe's own check reports nothing for this release; ours must find all of it."""
    old, _new, diff = stripe
    kinds = Counter(change.kind for change in diff.changes)
    print("\nstripe 15.6.2 -> 16.0.0:", dict(kinds), "ignored:", diff.ignored)

    # `stripe.Reversal` renamed to `stripe.TransferReversal`.
    (reversal,) = [c for c in diff.changes if c.display == "stripe.Reversal"]
    assert (reversal.kind, reversal.new_name, reversal.candidate) == (
        cmp.NAME_REMOVED,
        "stripe.TransferReversal",
        True,
    )

    # `StripeObject.request()` removed: one entry, on the declaring class.
    (request,) = [c for c in diff.changes if c.kind == cmp.METHOD_REMOVED]
    assert request.symbol == "stripe._stripe_object.StripeObject.request"
    assert "stripe.Customer.request" in request.public and len(request.public) > 80
    resolved = old.resolve("stripe.Customer.request")
    assert resolved is not None and resolved.symbol == request.symbol

    # Path parameters made positional-only in service methods.
    assert kinds[cmp.PARAM_KIND] > 500
    assert all(
        c.detail == "positional or keyword -> positional-only"
        for c in diff.changes
        if c.kind == cmp.PARAM_KIND
    )

    # `payment_method_types` removed from several params: seen on the TypedDicts and on
    # the methods that take them as **params. (The spike's prototype missed these.)
    removed_keys = {c.display for c in diff.changes if c.kind == cmp.KEY_REMOVED}
    assert "stripe.params.PaymentIntentCreateParams" in removed_keys
    kwargs = {(c.display, c.param) for c in diff.changes if c.kind == cmp.KWARG_REMOVED}
    assert ("stripe.PaymentIntent.create", "payment_method_types") in kwargs

    assert (diff.unresolved_old, diff.unresolved_new) == (0, 0)


def test_openai_minor_releases_are_mostly_noise_and_it_is_filtered(
    openai: tuple[Surface, Surface, ApiDiff],
) -> None:
    """griffe's own check flagged 42 public changes here, about 2 of them real."""
    old, _new, diff = openai
    kinds = Counter(change.kind for change in diff.changes)
    print("\nopenai 3.0.0 -> 3.49.0:", dict(kinds), "ignored:", diff.ignored)
    for change in diff.changes:
        print("  ", change.kind, change.display, change.param or "", change.new_name or "")

    assert len(diff.changes) < 15
    assert diff.ignored.get(cmp.IGNORE_PLACEHOLDER_DEFAULT, 0) > 0
    assert diff.ignored.get(cmp.IGNORE_CLASS_TO_ALIAS, 0) > 0

    # The client's attribute chain resolves to the method that implements it.
    create = old.resolve("openai.OpenAI.chat.completions.create")
    assert create is not None
    assert create.symbol == "openai.resources.chat.completions.completions.Completions.create"
    looked_up = old.lookup("OpenAI.chat.completions.create")
    assert looked_up is not None and "model:" in (looked_up.signature or "")
