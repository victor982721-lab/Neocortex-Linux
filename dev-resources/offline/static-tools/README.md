# Static tooling inventory

`manifest.json` is a **historical-only** hash inventory for the Linux CPython
3.14 analyzer and focused test-tool environments observed before the product
was restricted to CPython 3.13. It is retained as provenance, not as an active
toolchain. In particular, it has no `runtime_reference`: the CPython 3.13
product lock is not a static-tooling lock, and this inventory must not be used
to infer or provision a quality environment.

`requirements-available-cp314-linux.txt` is likewise the local slice recorded
by that historical snapshot (`pytest`/`pluggy`/`iniconfig`). It is not a
CPython 3.13 installation recipe.

`missing-wheel` is intentional and fail-closed: the manifest records the
observed installed version and `installed_record_sha256`, but leaves the wheel
hash absent because no compatible local artifact was found. In particular,
Ruff, Mypy, Pyright, Semgrep and their non-runtime dependencies must not be
reconstructed from an unpinned provider, a product runtime lock, or a remote
provider. An active CPython 3.13 quality environment requires its own
independently authenticated local supply and verification; no such closure is
claimed by this file.

The focused test validates the historical manifest, local hashes and available
requirements slice without inspecting a host wheelhouse, installing packages,
or acting as a repository-wide quality gate.
