"""Explicit, sequential preparation and local-only model inspection."""

from __future__ import annotations
import gc
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from neocortex.platform.policy import current_platform_policy
# Keep the product namespace explicit for the foundation migration contract.
# neocortex.foundation supplies the shared provenance/identity primitives.
# Keep the product namespace explicit for the foundation migration contract.
# neocortex.foundation supplies the shared provenance/identity primitives.

from neocortex.semantic.semantic_config import (
    SemanticModelUnavailableError,
    fastembed_cache_contract,
    local_fastembed_snapshot,
    production_models,
)

MODELS_REPORT_SCHEMA_VERSION = 2
WHISPER_MODEL_ID = "Systran/faster-whisper-small"
WHISPER_REQUIRED_FILES = ("model.bin", "config.json", "tokenizer.json")


class SemanticPreparer(Protocol):
    """Callable contract used to acquire the selected semantic models."""

    def __call__(
        self,
        state_directory: Path,
        *,
        model_cache_override: Path,
        include_compact: bool,
        local_files_only: bool,
        threads: int | None,
        model_ids: Sequence[str] | None = None,
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class ManagedModelStatus:
    model_id: str
    kind: str
    prepared: bool
    reason: str
    location: str
    files_available: bool = False
    backend_components: tuple[dict[str, object], ...] = ()
    required_files: tuple[str, ...] = ()
    repository_id: str | None = None
    model_signature: str | None = None
    runtime_verified: bool = False

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _backend_metadata(*, audio: bool) -> tuple[dict[str, object], ...]:
    from neocortex.capabilities.runtime import inspect_python_component

    if audio:
        components = (
            inspect_python_component("faster-whisper", "faster_whisper", extra="audio"),
            inspect_python_component("ctranslate2", "ctranslate2", extra="audio"),
        )
    else:
        components = (
            inspect_python_component("fastembed", "fastembed", extra="semantic"),
            inspect_python_component(
                "onnxruntime",
                "onnxruntime",
                extra="semantic",
                owner_distribution="fastembed",
            ),
        )
    return tuple(component.to_dict() for component in components)


def _semantic_statuses(
    models_root: Path,
    selected_ids: frozenset[str],
) -> tuple[ManagedModelStatus, ...]:
    cache = models_root / "fastembed"
    statuses: list[ManagedModelStatus] = []
    components = _backend_metadata(audio=False)
    for model in production_models():
        if model.model_id not in selected_ids:
            continue
        contract = fastembed_cache_contract(model.model_signature)
        snapshot = cache
        try:
            snapshot = local_fastembed_snapshot(model, cache)
        except (OSError, SemanticModelUnavailableError, ValueError) as exc:
            files_available, reason = False, str(exc)
        else:
            files_available, reason = True, "local_files_present_runtime_not_verified"
        backend_available = all(component["available"] for component in components)
        if files_available and not backend_available:
            reason = "semantic_backend_requirements_unmet"
        statuses.append(
            ManagedModelStatus(
                model.model_id,
                f"fastembed-{model.modality.value}",
                files_available and backend_available,
                reason,
                str(snapshot),
                files_available,
                components,
                contract.required_files,
                contract.repository_id,
                model.model_signature,
            )
        )
    return tuple(statuses)


def _valid_whisper_directory(candidate: Path) -> bool:
    return candidate.is_dir() and all(
        (candidate / filename).is_file() and (candidate / filename).stat().st_size > 0
        for filename in WHISPER_REQUIRED_FILES
    )


def _whisper_snapshot_directory(
    cache: Path,
    model_id: str = WHISPER_MODEL_ID,
) -> Path | None:
    basename = model_id.rsplit("/", 1)[-1]
    direct_candidates = (cache, cache / basename.removeprefix("faster-whisper-"), cache / basename)
    for candidate in direct_candidates:
        try:
            if _valid_whisper_directory(candidate):
                return candidate
        except OSError:
            return None
    repository = cache / ("models--" + model_id.replace("/", "--"))
    reference = repository / "refs" / "main"
    try:
        commit = reference.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        return None
    if not 40 <= len(commit) <= 64 or any(
        character not in "0123456789abcdef" for character in commit
    ):
        return None
    snapshot = repository / "snapshots" / commit
    try:
        return snapshot if _valid_whisper_directory(snapshot) else None
    except OSError:
        return None


def _whisper_status(models_root: Path) -> ManagedModelStatus:
    cache = models_root / "whisper"
    snapshot = _whisper_snapshot_directory(cache)
    components = _backend_metadata(audio=True)
    prepared = snapshot is not None and all(item["available"] for item in components)
    return ManagedModelStatus(
        WHISPER_MODEL_ID,
        "whisper-cpu-int8",
        prepared,
        (
            "local_files_present_runtime_not_verified"
            if prepared
            else "whisper_backend_requirements_unmet"
            if snapshot is not None
            else "whisper_small_cache_incomplete"
        ),
        str(cache if snapshot is None else snapshot),
        snapshot is not None,
        components,
        WHISPER_REQUIRED_FILES,
        WHISPER_MODEL_ID,
    )


def _selected_models(model_ids: Sequence[str] | None) -> frozenset[str]:
    known = frozenset(model.model_id for model in production_models()) | {WHISPER_MODEL_ID}
    if model_ids is None:
        return known
    selected = frozenset(model_ids)
    if not selected or selected - known:
        raise ValueError(f"invalid production model selection: {sorted(selected - known)!r}")
    return selected


def inspect_models(
    *,
    models_root: Path | None = None,
    model_ids: Sequence[str] | None = None,
) -> dict[str, object]:
    """Inspect local files and package metadata without creating any path."""

    root = current_platform_policy().models_directory if models_root is None else models_root
    selected = _selected_models(model_ids)
    semantic_ids = selected - {WHISPER_MODEL_ID}
    statuses = (
        *(_semantic_statuses(root, semantic_ids) if semantic_ids else ()),
        *((_whisper_status(root),) if WHISPER_MODEL_ID in selected else ()),
    )
    return {
        "schema_version": MODELS_REPORT_SCHEMA_VERSION,
        "kind": "models_report",
        "models_root": str(root),
        "all_prepared": all(status.prepared for status in statuses),
        "selection": "all" if model_ids is None else "explicit",
        "all_production_models_selected": selected == _selected_models(None),
        "runtime_verified": False,
        "models": [status.to_dict() for status in statuses],
    }


def _prepare_whisper_default(cache: Path) -> None:
    from faster_whisper import WhisperModel  # type: ignore[import-untyped]

    model = WhisperModel(
        "small",
        device="cpu",
        compute_type="int8",
        download_root=str(cache),
        local_files_only=False,
    )
    del model
    gc.collect()


def prepare_models(
    *,
    models_root: Path | None = None,
    model_ids: Sequence[str] | None = None,
    semantic_preparer: SemanticPreparer | None = None,
    whisper_preparer: Callable[[Path], None] = _prepare_whisper_default,
) -> dict[str, object]:
    """Explicitly acquire selected models sequentially, retaining partial caches.

    Only this preparation operation authorizes acquisition; status and ordinary
    offline processing never call it. Omitting model_ids retains full preparation.
    """

    selected = _selected_models(model_ids)
    semantic_ids = tuple(
        model.model_id for model in production_models() if model.model_id in selected
    )
    policy = current_platform_policy()
    root = policy.models_directory if models_root is None else models_root
    fastembed_cache = root / "fastembed"
    whisper_cache = root / "whisper"
    root.mkdir(parents=True, exist_ok=True)
    if semantic_ids:
        if semantic_preparer is None:
            from neocortex.semantic.semantic_preparation import prepare_semantic_models

            preparer: SemanticPreparer = prepare_semantic_models
        else:
            preparer = semantic_preparer
        if model_ids is None:
            preparer(
                policy.state_directory,
                model_cache_override=fastembed_cache,
                include_compact=True,
                local_files_only=False,
                threads=None,
            )
        else:
            preparer(
                policy.state_directory,
                model_cache_override=fastembed_cache,
                include_compact=True,
                local_files_only=False,
                threads=None,
                model_ids=semantic_ids,
            )
        gc.collect()
    if WHISPER_MODEL_ID in selected:
        whisper_cache.mkdir(parents=True, exist_ok=True)
        whisper_preparer(whisper_cache)
        gc.collect()
    report = (
        inspect_models(models_root=root)
        if model_ids is None
        else inspect_models(models_root=root, model_ids=model_ids)
    )
    if not report["all_prepared"]:
        raise RuntimeError("model_preparation_incomplete")
    return report


__all__ = [
    "MODELS_REPORT_SCHEMA_VERSION",
    "WHISPER_MODEL_ID",
    "ManagedModelStatus",
    "inspect_models",
    "prepare_models",
]
