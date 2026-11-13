# Pre-phase feasibility spike

**Date:** 2026-11-13 · **Checklist:** Pre-phase · **Specs affected:** 01, 03, 04, 05, 08, 10

The spike tested three assumptions the plan rests on, using real data: release histories
from GitHub and PyPI, real wheels diffed with griffe, and a live fetch of OpenAI's
deprecations page. Spike scripts are not in the repo; they ran in a throwaway
environment, and every number below can be reproduced from the sources named.

## Verdict

**The plan holds, with one change of emphasis and one real correction.** API-level
model retirements are the richest source of fixable changes, and Stripe is a much
better SDK source than OpenAI. The correction: griffe's built-in breaking-change check
cannot be used as the API diff, because it misses everything in Stripe; spec 03 needs
its own comparison on top of griffe's loader. Four adjustments are listed at the end.

| Question | Answer |
|---|---|
| Can griffe diff real SDK versions in reasonable time? | **Yes.** 1–23 s per version to load, under 1 s to compare |
| Can griffe's built-in breaking-change check be used as is? | **No.** It misses all of Stripe (aliases into private modules), and is noisy on `openai`. Spec 03 needs its own comparison on top of griffe's loader |
| Are there enough fixable SDK breaking changes? | **Few in `openai`, enough in `stripe`** |
| Can the deprecations page be fetched by a plain HTTP client? | **Yes**, and a Markdown version with a clean table exists |

---

## 1. griffe on real `openai` versions

Wheels were downloaded from PyPI, hash-checked and unzipped; griffe loaded them
statically (`allow_inspection=False`), so no package code ran.

| Diff | Load old | Load new | Diff | Flagged | On public paths |
|---|---|---|---|---|---|
| 1.109.1 → 2.0.0 (major) | 7.7 s | 3.6 s | 0.3 s | 0 | 0 |
| 2.54.0 → 3.0.0 (major) | 12.3 s | 7.0 s | 0.6 s | 1 | 0 |
| 2.0.0 → 2.54.0 (≈ 1 year of minors) | 1.0 s | 1.9 s | 0.4 s | 10 | 10 |
| 3.0.0 → 3.49.0 (3 months of minors) | 2.6 s | 22.5 s | 0.8 s | 46 | 42 |

Loads are slower when the files are not yet in the OS cache. griffe loaded the
whole SDK: about 920 modules, 1,870 classes, 1,740 functions;
`Completions.create` resolved with all 39 parameters.

**Finding 1: both recent `openai` majors are invisible to griffe, correctly.**
- 2.0.0 changed the *type* of a response field (`output` became "string or list").
  griffe does not report attribute type changes, and the spec already treats
  response-shape changes as report-only.
- 3.0.0 replaced the HTTP library (`httpx` → HTTPX2). This breaks user code only where
  it passes a custom `http_client=`. It is a dependency change, not an `openai` API
  change.

**Finding 2: griffe catches real breaks the release notes never announced.** Between
2.0.0 and 2.54.0, ten public objects were removed in minor releases with no breaking
label, for example `ComputerToolParam.display_width`, `display_height` and
`environment`. This confirms the value of diff-only records (spec 03 F3.9). These are
keys of a dict the caller passes in, so finding them needs the scanner to look at dict
literals, not only keyword arguments.

**Finding 3: raw griffe output is too noisy for the release gate.** Of the 42 public
"breakages" between 3.0.0 and 3.49.0:
- 26 are "parameter default changed", where a default moved from one "not given"
  placeholder to another. No caller breaks.
- 18 are "class became attribute", where generated type classes became type aliases.
  No caller breaks.
- About 2 are real removals.

Used unfiltered, almost every release would pass the gate. A filter is required
(change 2 below).

## 2. Breaking changes in the two SDKs

### `openai`

- 464 stable releases; 392 since 1.0.0 (Nov 2023); 111 in the last 12 months.
- Majors: 1.0 (Nov 2023), 2.0 (Sep 2025), 3.0 (Aug 2026).
- Releases ever labelled breaking: **4**; since 1.0: **3 items**, two response-type
  changes and one dependency swap. **None is a renamed or removed parameter.**
- Deprecations since 1.0: mostly whole APIs (Assistants, Sora video), which have no
  mechanical fix. One clean rename: `files.retrieve_content` → `files.content`.

`openai` SDK changes alone would give the codemods and the agent very little to do.

### `stripe`

- 406 stable releases; a new major roughly every six months, each tied to a new pinned
  API version.
- Breaking items since 2023, from release notes and `CHANGELOG.md`: well over 100,
  mostly parameter and field removals, plus a steady stream of mechanical renames:

| Version | Change | Fix |
|---|---|---|
| 9.0.0 | `features` → `marketing_features` on `stripe.Product` params | Codemod |
| 10.0.0 | `volume_decimal` → `quantity_decimal` on Issuing fuel params | Codemod |
| 11.0.0 | `usage_threshold_config` → `usage_threshold` on `billing.Alert` | Codemod, if the shape is unchanged |
| 13.0.0 | `StripeClient.parse_thin_event` → `parse_event_notification` | Codemod |
| 13.0.0 | `http_client.Urllib2Client` → `UrllibClient` | Codemod |
| 16.0.0 | `stripe.Reversal` → `stripe.TransferReversal` | Codemod |
| 16.0.0 | Path parameters positional-only in all service methods | Codemod: `retrieve(id=x)` → `retrieve(x)` |
| 12.0.0 | `coupon=` removed in favour of `discounts=[...]`; `Invoice.upcoming` removed | Agent, or report-only |

**Finding 4: Stripe's release-note format changes over time.** Up to v12 breaks sit
under a "Breaking changes" heading. From v13 they are marked inline with ⚠️. From v16
the GitHub release body is only a link to `CHANGELOG.md`, so watching GitHub Releases
alone sees **nothing** for Stripe's two latest majors.

### griffe on `stripe` 15.6.2 → 16.0.0

**Finding 5: griffe's built-in breaking-change check sees nothing in Stripe.** It
reported **0** breakages for a major release whose changelog lists a class rename, a
removed method and a signature change on every service method. The cause is how Stripe
is laid out: its 283 public names are mostly **aliases** (`from stripe._reversal import
Reversal`) into **private** modules. griffe's checker skips both aliases and
underscore modules, so it compares almost nothing.

A short custom comparison that walks the public names and follows each alias to its
target (load 9.5 s, compare 0.2 s) finds every documented break:

| Documented in the changelog | Found by following aliases |
|---|---|
| `stripe.Reversal` renamed to `stripe.TransferReversal` | `Reversal` removed and `TransferReversal` added, paired as a rename candidate |
| `StripeObject.request()` removed | `request` removed from 88 public classes |
| Path parameters made positional-only | 652 parameters changed from positional-or-keyword to positional-only |
| `payment_method_types` removed from several params | **Missed**: these are keys of a params `TypedDict` taken as `**params` |

So spec 03 cannot rely on `griffe.find_breaking_changes`. It must use griffe to load
the surface, then do its own comparison that follows aliases and also compares
`TypedDict` keys (the gap shared with `openai`'s `ComputerToolParam`).

## 3. OpenAI's deprecations page

| Check | Result |
|---|---|
| Plain HTTP fetch | HTTP 200, no bot challenge. `platform.openai.com/docs/deprecations` redirects to `developers.openai.com/api/docs/deprecations` |
| Server-rendered | Yes: model names and 46 dates present in the HTML |
| Stable between fetches | Yes: reduced text identical across two fetches |
| Markdown version | **Yes**: the same URL plus `.md` returns `text/markdown`, 64 kB |
| Conditional requests | Yes: `ETag` and `Last-Modified` headers |
| Structure | A table per section: shutdown date, model IDs, status, replacement, notice date |

**Finding 6: the Markdown table makes API-level changes the strongest source.** Upcoming
retirements as of 2026-11-13: **34 rows, 52 model IDs**.

| Kind | Rows | Example |
|---|---|---|
| One replacement, same API | about 20 | `gpt-5` → `gpt-6.1-sol`, shutdown 2026-12-11 |
| One replacement, different API | 3 | `tts-1` → `gpt-realtime-2.1-mini` (speech endpoint to realtime) |
| A choice, or extra settings | 10 | `gpt-image-1.5` → `gpt-image-2.6-sunburst` or `-flare`; `o3-pro` → `gpt-6.1-sol` with `reasoning.mode: pro` |
| No replacement | 1 | `gpt-5.5-cyber-preview` |

The "different API" rows show that matching the model string is not enough: the
scanner must also check which SDK method the string is passed to.

**A live demo case exists:** `gpt-5`, `gpt-5-mini`, `gpt-5-nano` and `o3` shut down on
**2026-12-11**, four weeks after this spike.

---

## Changes this spike makes to the plan

1. **Spec 01 — `CHANGELOG.md` fallback is required, not optional.** When a release body
   is empty or only links elsewhere, read the matching section of the repository's
   `CHANGELOG.md`. Without it, Stripe 16 and 17 are invisible.
2. **Spec 03 — own comparison on top of griffe's loader.** Do not use
   `griffe.find_breaking_changes`. Walk the public names, follow aliases into private
   modules, and compare: public names removed (pairing an added name as a rename
   candidate), public methods removed, parameters removed, renamed or changed in kind,
   and keys removed from `TypedDict` params. Ignore default changes between "not given"
   placeholders, and class-to-alias changes in generated type modules. This feeds both
   the release gate and diff-only records.
3. **Spec 02 — extraction must handle both note formats** (headings and inline ⚠️),
   and the 30-note eval set should include both.
4. **Spec 10 — parse the deprecations table directly.** Fetch the `.md` version and
   parse the table with plain code; use the LLM only for the replacement column when it
   is not a single model ID. Spec 04's literal match must also check the SDK method
   (chat, audio, image, realtime), not only the string.

**Applied 2026-11-13** to specs 01 (F1.14, F1.17), 02 (F2.11–F2.13), 03 (F3.2–F3.4,
F3.13–F3.15), 10 (F10.1–F10.5, F10.8, F10.10, F10.15, F10.16) and, as a follow-on,
spec 04: API family on literal matches (F4.12) and `TypedDict` key matching (F4.13).

Two decisions follow from the numbers:

- **Keep both providers.** `openai` contributes mainly through its deprecations page;
  `stripe` contributes the SDK renames. Dropping `stripe` (first item on the cut list)
  would leave the SDK path with almost nothing to fix. Consider moving it lower on the
  cut list.
- **Fixture repos are essential for the evals.** Real fixable SDK breaks number in the
  low tens over three years, which supports seeding fixture repos from these real
  changes (spec 08), rather than relying on finding real affected repos alone.

## Not covered

- The custom alias-following comparison was run only on Stripe 15.6.2 → 16.0.0. Its
  noise level on `openai` and on consecutive single releases is not measured, so the
  gate's false-positive rate per release is an estimate.
- `TypedDict` key comparison was not prototyped.
- `openai` 3.50.0 and Stripe 16.1.0 and 17.0.0 were not diffed: PyPI downloads ran at
  roughly 1–4 minutes per wheel on this connection and one stalled.
- Whether `usage_threshold` in Stripe 11 kept the old shape was not checked.
- Repo usage of these APIs (how many real repos call `stripe.Reversal`, or pass
  `model="gpt-5"`) was not measured; that is the Phase 1 eval-repo search.
