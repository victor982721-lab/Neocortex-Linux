# Development fixtures: original DEV + retired R1

**RETIRED_HOLDOUT_R1_NOW_DEVELOPMENT**. This is a development-only view,
not a new acceptance set or an independent holdout. R1 failed its single
reserved attempt for candidate `fd0d3fa0a6b7fecd38a594607466b4703aa5ac89`
and was explicitly retired by the root coordinator before its release for
diagnosis. It must never be reused or reported as a blind acceptance holdout.
The retirement does not revise or replace that `NOT_APPROVED` outcome.

## Scope and reporting

The `development-expanded-r1` split contains 40 files, 39 logical resources
and 30 queries: 24 positive and 6 negative. Always report these groups
separately, even when also reporting a combined development diagnostic:

- Original DEV: 24 files, 23 logical resources, 20 queries, **16 positive / 4
  negative** (`Q*` query IDs; `D*` file IDs)
- Retired R1: 16 files, 16 logical resources, 10 queries, **8 positive / 2
  negative** (`R*` query IDs; `H*` file IDs)

The functional v1 manifest and judgment schemas are unchanged; only the view
split is `development-expanded-r1`. Files are copied directly into `corpus/`
without collisions, all source bytes, fixture IDs, resource IDs, revision pins,
query text, query IDs and relevance judgments remain exact. No cases are
pruned, relabeled, repaired, rewritten or added.

`provenance/original-dev/` and `provenance/retired-r1/` contain byte-identical
copies of each original `manifest.json` and `queries.json`, under separate
paths to prevent basename collisions. The original frozen v1 dataset remains
untouched; its freeze SHA-256 is
`02c43c7100db4493785b3bd69ae43358115e800050eac8ac1d0fff4817921ce9`.

A development result cannot erase the failed original gate, authorize
publication or installation, or substitute for new independent acceptance.
No model execution, ingestion, production SQLite access or corpus mutation
was performed to construct this copy-only view.
