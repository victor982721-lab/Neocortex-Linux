"""NumPy 2 alias equivalence and private native-binding failure boundaries."""
from __future__ import annotations

import importlib
import os
from pathlib import Path
from types import SimpleNamespace
import warnings

import numpy
import pytest

from neocortex.semantic import semantic_exact_index_format as binding
from neocortex.semantic.semantic_exact_index import (
    ExactIndexUnavailable, open_exact_index, prepare_exact_index,
)
from neocortex.semantic.semantic_search_repository import search_exact_page
from tests.semantic_exact_index_fixtures import published_text_fixture
from tests.test_semantic_exact_index_equivalence import _page_oracle

TEST_CAPABILITIES = ("inference",)
pytestmark = pytest.mark.capability("inference")


def _legacy_binding() -> dict[str, object]:
    # Deliberately exercise the deprecated alias once inside a local warning
    # capture. Production code and every other test run with warnings as errors.
    with warnings.catch_warnings(record=True) as emitted:
        warnings.simplefilter("always", DeprecationWarning)
        from numpy.core import _multiarray_umath as legacy
    native = importlib.import_module("numpy._core._multiarray_umath")
    assert legacy is native
    assert all(issubclass(value.category, DeprecationWarning) for value in emitted)
    import sys
    path = Path(legacy.__file__)
    return {
        "numpy_version": numpy.__version__,
        "numpy_core_module": "numpy.core._multiarray_umath",
        "numpy_core_file": path.name,
        "numpy_core_sha256": binding._numpy_core_sha256(path),
        "cpu_features": sorted(name for name, enabled in legacy.__cpu_features__.items() if enabled),
        "byteorder": sys.byteorder,
        "cache_tag": sys.implementation.cache_tag,
    }


def test_numpy2_native_import_preserves_complete_legacy_binding() -> None:
    old = _legacy_binding()
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        assert binding._numpy_runtime_binding(numpy) == old


def test_previous_alias_index_reopens_with_identical_scores_and_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = published_text_fixture(tmp_path, rows=24)
    expected = search_exact_page(fixture.database, fixture.query, batch_size=8, text_scope="content")
    old = _legacy_binding()
    directory = tmp_path / "legacy-index"
    with monkeypatch.context() as old_runtime:
        old_runtime.setattr(binding, "_numpy_runtime_binding", lambda *_args, **_kwargs: dict(old))
        prepare_exact_index(
            fixture.database, directory, model_signature=fixture.model.model_signature,
        ).close()
    with open_exact_index(fixture.database, directory) as handle:
        actual = search_exact_page(
            fixture.database, fixture.query, batch_size=8, text_scope="content", exact_index=handle,
        )
        assert handle.usage_summary()["used_queries"] == 1
    assert _page_oracle(actual) == _page_oracle(expected)


@pytest.mark.parametrize("version", ("1.26.4", "2.0.2", "3.0.0", "unknown", None))
def test_unsupported_numpy_major_or_missing_version_fails_closed(version: object) -> None:
    with pytest.raises(binding.DerivedViewContractError):
        binding._numpy_runtime_binding(SimpleNamespace(__version__=version))


@pytest.mark.parametrize("failure", ("missing_module", "wrong_module", "missing_file", "non_elf", "empty_features", "bad_features"))
def test_native_extension_contract_is_validated_before_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    binding._numpy_runtime_binding(numpy)  # Populate the real build cache first.
    native = importlib.import_module("numpy._core._multiarray_umath")
    fake = SimpleNamespace(
        __name__=native.__name__, __file__=native.__file__, __cpu_features__=native.__cpu_features__,
    )
    if failure == "missing_module":
        def unavailable(_name: str) -> object:
            raise ImportError("test native extension unavailable")
        monkeypatch.setattr(binding.importlib, "import_module", unavailable)
    else:
        if failure == "wrong_module":
            fake.__name__ = "unexpected.native"
        elif failure == "missing_file":
            fake.__file__ = None
        elif failure == "non_elf":
            binary = tmp_path / "native.so"
            binary.write_bytes(b"not ELF")
            fake.__file__ = str(binary)
        elif failure == "empty_features":
            fake.__cpu_features__ = {}
        elif failure == "bad_features":
            fake.__cpu_features__ = {"AVX": "true"}
        monkeypatch.setattr(binding.importlib, "import_module", lambda _name: fake)
    with pytest.raises(binding.DerivedViewContractError):
        binding._numpy_runtime_binding(numpy)


def test_changed_cpu_features_invalidate_only_derived_view(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = published_text_fixture(tmp_path, rows=24)
    directory = tmp_path / "index"
    prepare_exact_index(fixture.database, directory, model_signature=fixture.model.model_signature).close()
    before = fixture.database.read_bytes()
    native = importlib.import_module("numpy._core._multiarray_umath")
    features = dict(native.__cpu_features__)
    enabled = next(name for name, value in features.items() if value)
    features[enabled] = False
    monkeypatch.setattr(native, "__cpu_features__", features)
    assert enabled not in binding._numpy_runtime_binding(numpy)["cpu_features"]
    with pytest.raises(ExactIndexUnavailable):
        open_exact_index(fixture.database, directory)
    assert search_exact_page(fixture.database, fixture.query).complete
    assert fixture.database.read_bytes() == before
    assert directory.is_dir()


@pytest.mark.parametrize("change", ("replace", "mtime", "ctime"))
def test_binary_drift_during_hash_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    path = tmp_path / "native.so"
    path.write_bytes(b"\x7fELF" + b"original-build")
    original = binding._sha256_fd
    def drift(fd: int, **kwargs: object) -> str:
        result = original(fd, **kwargs)
        if change == "replace":
            replacement = tmp_path / "replacement"
            replacement.write_bytes(path.read_bytes())
            replacement.replace(path)
        elif change == "mtime":
            info = path.stat()
            os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000))
        else:
            path.chmod(0o640)
        return result
    monkeypatch.setattr(binding, "_sha256_fd", drift)
    with pytest.raises(binding.DerivedViewContractError, match="changed while hashing"):
        binding._numpy_core_sha256(path)


def test_cached_binding_preserves_cancellation() -> None:
    binding._numpy_runtime_binding(numpy)
    cancellation = RuntimeError("cancel current operation")
    def cancel() -> None:
        raise cancellation
    with pytest.raises(RuntimeError) as caught:
        binding._numpy_runtime_binding(numpy, cancellation_check=cancel)
    assert caught.value is cancellation
