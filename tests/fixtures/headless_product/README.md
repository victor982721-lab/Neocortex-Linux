# Bounded installed-product fixtures

Authored synthetic content only; no personal corpus, models, productive SQLite
state, application code, or downloaded documents. `expected.json` records exact
fixture hashes and content invariants, not hashes of generated databases.

- `base/`: 20 files with text, a byte-identical pair, Unicode names, CSV and a
  four-file Python project; code is content and must never be executed
- `documents/`: one native-text PDF, one DOCX and one ODT, authored as minimal
  standard PDF/XML/ZIP data with fixed ZIP timestamps
- `image/`: a 1400 × 900 synthetic text raster for Pillow and real Tesseract OCR;
  no inference weights are needed or simulated
- `video/`: a two-second, 10 fps, 128 × 96 MPEG-4 synthetic color clip without audio,
  created with FFmpeg's color source; the test uses the real video route
- `video_low_fps/`: the same synthetic scene at 2 fps, preserving the final-frame
  sampling regression rather than masking a partial result as complete
- `audio/`: a one-second 16 kHz PCM synthetic tone for an explicit missing-backend
  `--all` run; no transcript or inference result is simulated
- `legacy/`: the DOCX fixture converted once with LibreOffice's MS Word 97
  exporter to exercise the real headless legacy Office integration

All fixture bytes are included directly in the repository and excluded from the
installed package. The tests copy at most 21 files into a private temporary
corpus, invoke an installed Neocortex through `-I -m neocortex` and its console
entry point from an unrelated directory, then inspect terminal state through
`SQLiteReadSession`. Replay compares semantic counters, content and publication
invariants, not machine-dependent paths, timestamps, inode values or binary
SQLite equality. The isolated inventory-owner test compares physical identity
only for the same copied tree within a single environment.

Run `tests/test_headless_product_workflows.py` using an isolated installed venv
and the documented pytest capability selector. `NEOCORTEX_TEST_PYTHON` optionally
selects a different installed venv; an explicit but invalid installation fails,
while source-only pytest runs skip these installed workflows with a reason.
LibreOffice conversion retains its existing `non_replayable` contract: a second
run executes again and records that fact, unlike reusable text/image caches.
The negative `--all` workflow requires an installation without inference engines,
retains all registered routes in the failed run, and checks that completed text
and Code owners remain queryable. Its Python network audit is process-local and
asserts the only children are local FFmpeg/FFprobe version probes.
External-tool tests skip only when the named real executable is absent; missing
Python requirements for a selected capability are processing failures, not mocks.
