"""Local model prerequisite contracts, not simulated inference equivalence."""

from __future__ import annotations

import json
import queue
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from neocortex.capabilities.formats.audio import whisper
from neocortex.capabilities.formats.audio.models import AudioRouteConfig
from neocortex.runtime.config import model_management
from neocortex.semantic import semantic_preparation
from neocortex.semantic.semantic_config import (
    SemanticModelUnavailableError,
    fastembed_cache_contract,
    local_fastembed_snapshot,
    multilingual_text_model,
)


def _semantic_snapshot(cache: Path) -> Path:
    contract = fastembed_cache_contract(multilingual_text_model().model_signature)
    repository = cache / ("models--" + contract.repository_id.replace("/", "--"))
    reference = repository / "refs" / "main"
    reference.parent.mkdir(parents=True)
    reference.write_text("a" * 40, encoding="ascii")
    snapshot = repository / "snapshots" / ("a" * 40)
    for relative in contract.required_files:
        candidate = snapshot / relative
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_bytes(b"not inference weights; prerequisite fixture only")
    return snapshot


def _whisper_directory(directory: Path, *, tokenizer: bool = True) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for name in model_management.WHISPER_REQUIRED_FILES:
        if name == "tokenizer.json" and not tokenizer:
            continue
        (directory / name).write_bytes(b"not inference weights; prerequisite fixture only")
    return directory


def test_models_inspection_does_not_import_native_packages_or_processing(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1]
    code = f"""
import json, sys
sys.path.insert(0, {str(source)!r})
before = set(sys.modules)
from neocortex.runtime.config.model_management import inspect_models
report = inspect_models(models_root=__import__('pathlib').Path({str(tmp_path / 'absent')!r}))
added = set(sys.modules) - before
forbidden = ('fastembed', 'onnxruntime', 'faster_whisper', 'ctranslate2', 'numpy', 'xxhash')
assert not any(name.split('.')[0] in forbidden for name in added), sorted(added)
assert 'neocortex.semantic.semantic_preparation' not in added
assert 'neocortex.semantic.semantic_backends' not in added
print(json.dumps({{'count': len(report['models']), 'runtime_verified': report['runtime_verified']}}))
"""
    process = subprocess.run(
        [sys.executable, "-I", "-S", "-c", code], capture_output=True, text=True,
        timeout=20, check=False,
    )
    assert process.returncode == 0, process.stderr
    assert json.loads(process.stdout) == {"count": 5, "runtime_verified": False}
    assert not (tmp_path / "absent").exists()


@pytest.mark.parametrize("backend_present", [False, True])
def test_selected_model_separates_local_files_backend_and_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend_present: bool,
) -> None:
    snapshot = _semantic_snapshot(tmp_path / "fastembed")
    monkeypatch.setattr(
        model_management, "_backend_metadata",
        lambda **_kwargs: ({"available": backend_present, "functional_status": "not_checked"},),
    )
    report = model_management.inspect_models(
        models_root=tmp_path, model_ids=[multilingual_text_model().model_id],
    )
    assert report["selection"] == "explicit"
    assert report["all_production_models_selected"] is False
    assert report["all_prepared"] is backend_present
    assert len(report["models"]) == 1
    status = report["models"][0]
    assert status["files_available"] is True
    assert status["prepared"] is backend_present
    assert status["runtime_verified"] is False
    assert status["location"] == str(snapshot)
    assert "tokenizer.json" in status["required_files"]
    assert status["model_signature"] == multilingual_text_model().model_signature


@pytest.mark.parametrize("selection", [[], ["pretend/tiny-model"]])
def test_invalid_selection_does_not_create_models(
    tmp_path: Path, selection: list[str],
) -> None:
    target = tmp_path / "not-created"
    with pytest.raises(ValueError, match="invalid production model selection"):
        model_management.prepare_models(models_root=target, model_ids=selection)
    assert not target.exists()


def test_whisper_only_preparation_never_requires_semantic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected = [model_management.WHISPER_MODEL_ID]
    calls: list[Path] = []

    def forbidden(*_args, **_kwargs):
        raise AssertionError("unselected semantic models must not be prepared")

    monkeypatch.setattr(
        model_management, "inspect_models",
        lambda **kwargs: {"all_prepared": True, "selection": kwargs["model_ids"]},
    )
    report = model_management.prepare_models(
        models_root=tmp_path, model_ids=selected,
        semantic_preparer=forbidden, whisper_preparer=calls.append,
    )
    assert calls == [tmp_path / "whisper"]
    assert report["selection"] == selected
    assert not (tmp_path / "fastembed").exists()


def test_semantic_preparation_can_probe_one_original_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected = multilingual_text_model().model_id
    observed: list[str] = []

    def factory(model, **kwargs):
        assert kwargs["local_files_only"] is True
        observed.append(model.model_id)
        return object()

    monkeypatch.setattr(semantic_preparation, "text_probe", lambda _backend: None)
    result = semantic_preparation.prepare_semantic_models(
        tmp_path / "state", model_cache_override=tmp_path / "models",
        model_ids=[selected], local_files_only=True, backend_factory=factory,
    )
    assert observed == [selected]
    assert [item.model_id for item in result] == [selected]
    assert result[0].model_signature == multilingual_text_model().model_signature


def test_missing_semantic_cache_is_reported_before_importing_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden():
        raise AssertionError("no native backend is needed to discover missing files")

    monkeypatch.setattr(semantic_preparation, "fastembed_availability", forbidden)
    with pytest.raises(SemanticModelUnavailableError, match="semantic_model_cache_missing"):
        semantic_preparation.require_local_fastembed_model(
            multilingual_text_model(), tmp_path / "missing",
        )


def test_semantic_cache_requires_original_tokenizer_files(tmp_path: Path) -> None:
    snapshot = _semantic_snapshot(tmp_path)
    (snapshot / "tokenizer.json").unlink()
    with pytest.raises(SemanticModelUnavailableError, match="cache_incomplete"):
        local_fastembed_snapshot(multilingual_text_model(), tmp_path)


def test_whisper_missing_tokenizer_fails_before_backend_constructor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = _whisper_directory(tmp_path / "model", tokenizer=False)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("backend constructor could fetch a missing tokenizer")

    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=forbidden))
    results: queue.SimpleQueue = queue.SimpleQueue()
    whisper._whisper_worker(queue.SimpleQueue(), results, {
        "model_name": str(directory), "model_cache_directory": None,
        "local_models_only": True, "device": "cpu", "compute_type": "int8",
    })
    kind, error_type, message = results.get()
    assert kind == "init_error"
    assert error_type == "WhisperRuntimeError"
    assert "tokenizer.json" in message
    assert "will not download" in message


def test_whisper_local_hf_snapshot_uses_exact_repository(tmp_path: Path) -> None:
    repository = tmp_path / "models--Systran--faster-whisper-small"
    reference = repository / "refs" / "main"
    reference.parent.mkdir(parents=True)
    reference.write_text("b" * 40, encoding="ascii")
    snapshot = _whisper_directory(repository / "snapshots" / ("b" * 40))
    model_id, resolved = whisper.local_whisper_model("small", tmp_path)
    assert model_id == "Systran/faster-whisper-small"
    assert resolved == snapshot


def test_named_whisper_model_does_not_resolve_from_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "small").mkdir()
    cache = _whisper_directory(tmp_path / "explicit-cache")
    monkeypatch.chdir(tmp_path)
    model_id, resolved = whisper.local_whisper_model("small", cache)
    assert model_id == model_management.WHISPER_MODEL_ID
    assert resolved == cache


def test_whisper_local_provenance_fingerprints_bytes_not_cache_path(tmp_path: Path) -> None:
    first = _whisper_directory(tmp_path / "first")
    second = _whisper_directory(tmp_path / "second")
    left = whisper.whisper_local_provenance("small", first)
    right = whisper.whisper_local_provenance("small", second)
    assert left == right
    assert left["files_available"] is True
    assert left["model_id"] == model_management.WHISPER_MODEL_ID
    assert len(left["artifacts"]) == 3
    (second / "tokenizer.json").write_bytes(b"changed tokenizer bytes")
    assert whisper.whisper_local_provenance("small", second) != left


def test_whisper_reuses_existing_stat_bound_artifact_fingerprint_cache(tmp_path: Path) -> None:
    from neocortex.foundation.processing_provenance import _fingerprint_file_cached

    directory = _whisper_directory(tmp_path / "model")
    whisper.whisper_local_provenance("small", directory)
    first = _fingerprint_file_cached.cache_info()
    whisper.whisper_local_provenance("small", directory)
    second = _fingerprint_file_cached.cache_info()
    assert second.misses == first.misses
    assert second.hits - first.hits == len(model_management.WHISPER_REQUIRED_FILES)


def test_audio_api_defaults_to_offline_on_linux(tmp_path: Path) -> None:
    assert AudioRouteConfig(state_path=tmp_path / "audio.sqlite3").local_models_only is True


def test_model_prerequisite_details_preserve_reason_across_worker_boundary() -> None:
    from neocortex.semantic.semantic_backend_supervisor import (
        _raise_remote_failure, _remote_failure,
    )

    source = SemanticModelUnavailableError(
        "semantic_backend_version_mismatch", "expected fastembed version; observed another",
    )
    kind, payload = _remote_failure(source)
    with pytest.raises(SemanticModelUnavailableError) as caught:
        _raise_remote_failure(kind, payload)
    assert caught.value.reason == source.reason
    assert caught.value.detail == source.detail
    assert str(caught.value) == str(source)
