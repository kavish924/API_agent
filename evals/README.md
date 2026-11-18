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
