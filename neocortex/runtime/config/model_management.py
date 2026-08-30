"""Explicit, sequential preparation and local-only model inspection."""

from __future__ import annotations
import gc
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from neocortex.platform_policy import current_platform_policy

from neocortex.foundation.processing_provenance import distribution_component
from neocortex.semantic.semantic_config import production_models
from neocortex.semantic.semantic_preparation import (
    SemanticModelUnavailableError,
    prepare_semantic_models,
    require_local_fastembed_model,
)

MODELS_REPORT_SCHEMA_VERSION = 1
WHISPER_MODEL_ID = "Systran/faster-whisper-small"
WHISPER_REQUIRED_FILES = ("model.bin", "config.json", "tokenizer.json")


@dataclass(frozen=True, slots=True)
class ManagedModelStatus:
    model_id: str
    kind: str
    prepared: bool
    reason: str
    location: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _semantic_statuses(models_root: Path) -> tuple[ManagedModelStatus, ...]:
    cache = models_root / "fastembed"
    statuses: list[ManagedModelStatus] = []
    for model in production_models():
        try:
            require_local_fastembed_model(model, cache)
        except (OSError, SemanticModelUnavailableError, ValueError) as exc:
            statuses.append(
                ManagedModelStatus(
                    model.model_id,
                    f"fastembed-{model.modality.value}",
                    False,
                    str(exc),
                    str(cache),
                )
            )
        else:
            statuses.append(
                ManagedModelStatus(
                    model.model_id,
                    f"fastembed-{model.modality.value}",
                    True,
                    "available",
                    str(cache),
                )
            )
    return tuple(statuses)


def _valid_whisper_directory(candidate: Path) -> bool:
    return candidate.is_dir() and all(
        (candidate / filename).is_file() and (candidate / filename).stat().st_size > 0
        for filename in WHISPER_REQUIRED_FILES
    )


def _whisper_snapshot_directory(cache: Path) -> Path | None:
    direct_candidates = (cache, cache / "small", cache / "faster-whisper-small")
    for candidate in direct_candidates:
        try:
            if _valid_whisper_directory(candidate):
                return candidate
        except OSError:
            return None
    repository = cache / "models--Systran--faster-whisper-small"
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
    return ManagedModelStatus(
        WHISPER_MODEL_ID,
        "whisper-cpu-int8",
        snapshot is not None,
        "available" if snapshot is not None else "whisper_small_cache_incomplete",
        str(cache if snapshot is None else snapshot),
    )


def _nudenet_status() -> ManagedModelStatus:
    component = distribution_component(
        "adult-model",
        "nudenet",
        artifact_relative_path="nudenet/320n.onnx",
    )
    artifact = component.get("artifact")
    prepared = bool(
        component.get("status") == "available"
        and isinstance(artifact, dict)
        and isinstance(artifact.get("xxh3_128"), str)
        and int(artifact.get("size_bytes", 0)) > 0
    )
    return ManagedModelStatus(
        "nudenet/320n.onnx",
        "bundled-nudenet",
        prepared,
        "available" if prepared else "bundled_nudenet_model_unavailable",
        "installed-distribution:nudenet",
    )


def inspect_models(*, models_root: Path | None = None) -> dict[str, object]:
    """Inspect local files and package metadata without creating any path."""

    root = current_platform_policy().models_directory if models_root is None else models_root
    statuses = (*_semantic_statuses(root), _whisper_status(root), _nudenet_status())
    return {
        "schema_version": MODELS_REPORT_SCHEMA_VERSION,
        "kind": "models_report",
        "models_root": str(root),
        "all_prepared": all(status.prepared for status in statuses),
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
    semantic_preparer: Callable[..., object] = prepare_semantic_models,
    whisper_preparer: Callable[[Path], None] = _prepare_whisper_default,
) -> dict[str, object]:
    """Prepare every production model sequentially, retaining partial caches."""

    policy = current_platform_policy()
    root = policy.models_directory if models_root is None else models_root
    fastembed_cache = root / "fastembed"
    whisper_cache = root / "whisper"
    root.mkdir(parents=True, exist_ok=True)
    semantic_preparer(
        policy.state_directory,
        model_cache_override=fastembed_cache,
        include_compact=True,
        local_files_only=False,
        threads=None,
    )
    gc.collect()
    whisper_cache.mkdir(parents=True, exist_ok=True)
    whisper_preparer(whisper_cache)
    gc.collect()
    report = inspect_models(models_root=root)
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
