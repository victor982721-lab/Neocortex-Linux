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

The active CPython 3.13 closure is recorded separately in
`manifest-cp313.json`, `provenance-cp313.json` and
`../locks/quality-cp313-linux-x86_64.lock`. It contains only the four
development analyzers (Ruff, Mypy, Pyright and Semgrep) and their exact
hash-pinned dependencies; runtime, build and base-test locks remain separate.
The wheel files are deliberately outside the repository at
`$HOME/.local/share/Neocortex/tooling/qa-supply-cp313-20260927/wheels`.
`provenance-cp313.json` records the authenticated PyPI resolution, the local
supply-manifest hash and the offline reinstall command. The lock is usable only
when that local supply is present; it does not silently fall back to a network
index.

`missing-wheel` is intentional and fail-closed: the manifest records the
observed installed version and `installed_record_sha256`, but leaves the wheel
hash absent because no compatible local artifact was found. In particular,
Ruff, Mypy, Pyright, Semgrep and their non-runtime dependencies must not be
reconstructed from an unpinned provider, a product runtime lock, or a remote
provider. An active CPython 3.13 quality environment requires its own
independently authenticated local supply and verification; no such closure is
claimed by this file.

The focused tests validate the historical manifest and the active CPython 3.13
lock/provenance records without inspecting a host wheelhouse, installing
packages, or acting as a repository-wide quality gate.

For this host, Semgrep's default Linux `io_uring` backend fails before scanning
with `io_uring_queue_init` allocation errors. The individual Semgrep check is
therefore run with `EIO_BACKEND=posix --jobs 1`; this is an invocation-local
fallback and does not change host limits or aggregate the QA tools.
