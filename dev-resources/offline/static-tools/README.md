# Static tooling inventory

`manifest.json` is the hash inventory for the Linux CPython 3.14 analyzer and
focused test-tool closures observed in the isolated tooling environments. It
keeps runtime ownership separate: packages marked `runtime-shared` resolve
through `constraints-linux-cp314.lock` and the canonical runtime wheelhouse,
while `requirements-available-cp314-linux.txt` contains only the local
`pytest`/`pluggy`/`iniconfig` slice.

`missing-wheel` is intentional and fail-closed: the manifest records the
observed installed version and `installed_record_sha256`, but leaves the wheel
hash absent because no compatible local artifact was found. In particular,
Ruff, Mypy, Pyright, Semgrep and their non-runtime dependencies must not be
reconstructed from an unpinned provider or from CPython 3.13-only binaries.

The focused test validates the manifest, local hashes, runtime separation and
the available requirements slice without installing packages or acting as a
repository-wide quality gate.
