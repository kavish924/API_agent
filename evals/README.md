# Evals

Each pipeline stage has its own eval (spec 08). Labelled data lives here; results are
written to `evals/results/`.

## Extraction (spec 02, spec 08 F8.1)

- `extraction/labels.jsonl`: one release note per line, with the changes it must
  yield (`expected`) and must not (`forbidden`). The notes themselves are in
  `tests/fixtures/notes/`.
- Run: `make eval-extraction` (or `python -m guardian.extract.evaluation`). Needs a
  configured LLM provider.
- Scores: recall over every expected change; precision over notes labelled
  `complete` only; forbidden hits; records rejected by the evidence check.
- Targets: precision >= 0.85, recall >= 0.80, no forbidden hits.

**Status:** 5 seed notes, one per release-note format the spike found, labelled by
Claude from the real text and marked for human review. The spec asks for 30 notes
labelled by a person before the model is run on them; that set is still to be built.

## Scanner (spec 04, spec 08 F8.2–F8.4)

- `scanner/cases.py`: small repositories, each using a real SDK one way, with the lines
  that should be found ending in `# expect`. The changes are real: stripe 15.6.2 →
  16.0.0 and openai 2.0.0 → 2.54.0.
- Cases are **in scope** (patterns the scanner is meant to handle), **known limits**
  (wrapped clients, dynamic calls; scored separately), or **negative** (the SDK is
  used, but nothing that changed).
- Three configurations are scored on the same cases: the scanner, libcst matching by
  spelling only, and grep.
- Run: `make eval-scanner` (fetches the wheels once), or
  `make eval-scanner ARGS="--wheels DIR"` with unpacked wheels.

**Latest result** (`results/scanner-20261118T074136Z.json`), 29 cases, 5 negative:

| Cases | Configuration | Recall | Precision |
|---|---|---|---|
| in scope | scanner | 1.00 (24/24) | 1.00 |
| in scope | libcst by name | 1.00 | 0.75 |
| in scope | grep | 1.00 | 0.71 |
| known limit | scanner | 0.00 (0/4) | n/a |
| known limit | grep | 1.00 (4/4) | 1.00 |

**Read with care:** the cases were written by Claude while building the scanner, so
they test the patterns it was designed for. The spec's 6–8 real repositories, pinned
just before a known break, are still to be found and will be the harder test.
