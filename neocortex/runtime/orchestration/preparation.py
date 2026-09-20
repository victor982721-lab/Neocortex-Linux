"""Bounded preparation of one explicit run, before opening state writers.

Preparation observes local metadata and the existing boundary. It never scans
Corpus content, downloads models, runs inference, or authorizes a file effect.
Route owners still validate their inputs and mutable dependencies at execution.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from neocortex.runtime.control.cancellation import CancellationRequested

PreparationStatus = Literal["ready", "unavailable", "blocked", "not_checked"]
PREPARATION_SCHEMA = "neocortex.run-preparation/v1"


class PreparationUnavailable(RuntimeError):
    """A required effect prerequisite could not be established."""


class Boundary(Protocol):
    @property
    def effective_signature(self) -> str: ...
    def verify(self) -> None: ...


@dataclass(frozen=True, slots=True)
class PreparationRequest:
    root: Path
    state_directory: Path
    selected_routes: tuple[str, ...]
    apply_requested: bool
    max_checks: int = 64
    time_budget_seconds: float = 15.0
    cancelled: Callable[[], bool] | None = None
    effective_options: Mapping[str, object] = field(default_factory=dict)
    run_budget: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.root.is_absolute() or not self.state_directory.is_absolute():
            raise ValueError("preparation requires the effective absolute roots")
        if len(self.selected_routes) > 32 or len(set(self.selected_routes)) != len(self.selected_routes):
            raise ValueError("preparation route selection must be bounded and unique")
        if not 1 <= self.max_checks <= 128 or not 0 < self.time_budget_seconds <= 60:
            raise ValueError("preparation budget is outside its bounds")


@dataclass(frozen=True, slots=True)
class PreparationCheck:
    check_id: str
    scope: str
    status: PreparationStatus
    reason_code: str
    required_for: str
    evidence_ref: str | None
    executed: bool
    observed_at_ns: int | None

    def to_dict(self) -> dict[str, object]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


@dataclass(frozen=True, slots=True)
class PreparationReport:
    request: PreparationRequest
    checks: tuple[PreparationCheck, ...]
    boundary_signature: str

    def require_effects_ready(self) -> None:
        if not self.request.apply_requested:
            return
        blocked = [c.check_id for c in self.checks
                   if c.required_for == "corpus_effects" and c.status != "ready"]
        if blocked:
            raise PreparationUnavailable("effect preparation unavailable: " + ", ".join(blocked))

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": PREPARATION_SCHEMA,
            "root": str(self.request.root), "state_directory": str(self.request.state_directory),
            "selected_routes": list(self.request.selected_routes),
            "apply_requested": self.request.apply_requested,
            "effective_options": dict(self.request.effective_options),
            "run_budget": dict(self.request.run_budget),
            "boundary_signature": self.boundary_signature,
            "status": "ready" if all(c.status == "ready" for c in self.checks) else "partial",
            "checks": [check.to_dict() for check in self.checks],
            "content_validation": {"executed": False, "count": None, "reason_code": "deferred_to_route_owner"},
            "content_crc": {"executed": False, "count": None, "reason_code": "deferred_to_content_read"},
            "runtime_inference": {"executed": False, "count": None, "reason_code": "deferred_to_model_owner"},
        }


def prepare_run(
    request: PreparationRequest, boundary: Boundary, *,
    probes: Sequence[tuple[str, str, str, Callable[[], tuple[PreparationStatus, str, str | None]]]],
) -> PreparationReport:
    """Compose cheap owner probes; retain not-checked results on exhausted budget."""
    started = time.monotonic()
    checks: list[PreparationCheck] = []
    if len(probes) > 126:
        raise ValueError("preparation probe count exceeds the report bound")
    boundary.verify()
    checks.append(PreparationCheck("boundary", "root_state", "ready", "owner_boundary_verified",
                                   "corpus_effects", boundary.effective_signature, True, time.time_ns()))
    for check_id, scope, required_for, probe in probes:
        if request.cancelled is not None and request.cancelled():
            raise CancellationRequested("run preparation cancelled")
        if len(checks) >= request.max_checks or time.monotonic() - started >= request.time_budget_seconds:
            checks.append(PreparationCheck(check_id, scope, "not_checked", "preparation_budget_exhausted",
                                           required_for, None, False, None))
            continue
        try:
            status, reason, evidence_ref = probe()
            if status not in {"ready", "unavailable", "blocked", "not_checked"}:
                raise ValueError("owner preparation status is invalid")
        except CancellationRequested:
            raise
        except Exception as exc:
            status, reason, evidence_ref = "unavailable", "owner_probe_failed:" + type(exc).__name__, None
        if request.cancelled is not None and request.cancelled():
            raise CancellationRequested("run preparation cancelled after owner probe")
        if time.monotonic() - started >= request.time_budget_seconds:
            status, reason = "blocked", "preparation_deadline_exceeded_after_probe"
        checks.append(PreparationCheck(check_id, scope, status, reason, required_for,
                                       evidence_ref, status != "not_checked", time.time_ns()))
    boundary.verify()
    if request.cancelled is not None and request.cancelled():
        raise CancellationRequested("run preparation cancelled after boundary verification")
    exhausted = time.monotonic() - started >= request.time_budget_seconds or any(
        check.reason_code == "preparation_budget_exhausted" for check in checks
    )
    checks.append(PreparationCheck(
        "preparation_budget", "invocation", "blocked" if exhausted else "ready",
        "preparation_budget_exhausted" if exhausted else "preparation_budget_available",
        "corpus_effects", None, True, time.time_ns(),
    ))
    return PreparationReport(request, tuple(checks), boundary.effective_signature)


_ROUTE_PACKAGES: Mapping[str, tuple[str, ...]] = {
    "text": (), "docx": (), "office": (),
    "pdf": ("pymupdf", "pdfminer"), "image": ("PIL", "pytesseract"),
    "audio": ("faster_whisper", "ctranslate2", "av"),
    "video": ("av", "PIL"),
}


def prepare_framework_run(
    config: Any, boundary: Boundary, selected_routes: Sequence[str], *,
    semantic_requested: bool = False, cancelled: Callable[[], bool] | None = None,
) -> PreparationReport:
    """Compose only dependencies selected by this invocation, using public owners."""
    request = PreparationRequest(Path(config.root), Path(config.state_directory),
                                 tuple(selected_routes), bool(config.apply_actions), cancelled=cancelled,
                                 effective_options={name: getattr(config, name, None) for name in (
                                     "audio_model_name", "audio_local_models_only", "pdf_ocr_mode",
                                     "image_document_ocr_mode", "video_ocr_mode",
                                     "video_ffmpeg_path",
                                 )},
                                 run_budget={name: getattr(config, name, None) for name in (
                                     "run_max_items", "run_max_bytes", "run_time_budget_seconds",
                                     "global_memory_budget_bytes", "global_cpu_slots",
                                 )})
    probes: list[tuple[str, str, str, Callable[[], tuple[PreparationStatus, str, str | None]]]] = []

    def sqlite_probe() -> tuple[PreparationStatus, str, str | None]:
        from neocortex.platform.policy import current_platform_policy
        from neocortex.platform.sqlite_runtime_attestation import observe_platform_native_runtime
        result = observe_platform_native_runtime(
            receipts_directory=current_platform_policy().state_directory / "installation-receipts",
            probe_timeout_seconds=request.time_budget_seconds,
        )
        status = result.get("status", "unaccredited")
        # Unaccredited development interpreters remain useful for read-only
        # analysis; they cannot acquire Corpus mutation authority here.
        observed = result.get("observed", {})
        return ("ready" if status == "approved" else "unavailable",
                "sqlite_" + str(status), observed.get("identity_sha256"))

    probes.append(("sqlite_runtime", "native_runtime", "corpus_effects", sqlite_probe))

    def capacity_probe() -> tuple[PreparationStatus, str, str | None]:
        observed = os.statvfs(request.state_directory)
        available = observed.f_bavail * observed.f_frsize
        return "ready", "free_space_observed_not_reserved", f"statvfs:available_bytes={available}"

    probes.append(("state_capacity", "state_storage", "route_execution", capacity_probe))
    if set(request.selected_routes) & {"pdf", "image", "audio", "video"}:
        def temporary_probe() -> tuple[PreparationStatus, str, str | None]:
            root = Path(tempfile.gettempdir()).resolve(strict=True)
            if root == request.root or root.is_relative_to(request.root):
                return "blocked", "temporary_root_inside_corpus", str(root)
            # A closed anonymous probe cannot leave an unmanaged payload.
            with tempfile.TemporaryFile(dir=root) as stream:
                stream.write(b"NCTX")
                stream.flush()
                os.fsync(stream.fileno())
            return "ready", "private_temporary_io_verified", str(root)
        probes.append(("temporary_io", "temporary_storage", "route_execution", temporary_probe))
    for route in request.selected_routes:
        packages = _ROUTE_PACKAGES.get(route)

        def package_probe(packages=packages) -> tuple[PreparationStatus, str, str | None]:
            if packages is None:
                return "not_checked", "extension_owner_preparation_not_registered", None
            missing = [name for name in packages if importlib.util.find_spec(name) is None]
            return ("unavailable", "package_metadata_missing", ",".join(missing)) if missing else (
                "ready", "package_metadata_present_runtime_deferred", None)

        probes.append((route + "_packages", route, "route_execution", package_probe))
        ocr_option = "image_document_ocr_mode" if route == "image" else route + "_ocr_mode"
        if (route in {"pdf", "image", "video"}
                and getattr(config, ocr_option, "auto") not in {"never", "off"}):
            def ocr_probe(route=route) -> tuple[PreparationStatus, str, str | None]:
                selected = getattr(config, route + "_tesseract_cmd", None) or "tesseract"
                path = shutil.which(selected)
                return ("ready", "executable_located_runtime_deferred", path) if path else (
                    "unavailable", "tesseract_missing", None)
            probes.append((route + "_ocr_executable", route, "route_execution", ocr_probe))
    if "video" in request.selected_routes:
        def ffmpeg_probe() -> tuple[PreparationStatus, str, str | None]:
            selected = getattr(config, "video_ffmpeg_path", None) or "ffmpeg"
            path = shutil.which(selected)
            return ("ready", "executable_located_runtime_deferred", path) if path else (
                "unavailable", "ffmpeg_missing", None)
        probes.append(("video_executable", "video", "route_execution", ffmpeg_probe))
    if "audio" in request.selected_routes or semantic_requested:
        def models_probe() -> tuple[PreparationStatus, str, str | None]:
            from neocortex.runtime.config.model_management import WHISPER_MODEL_ID, inspect_models
            if semantic_requested:
                result = inspect_models()
            else:
                cache = getattr(config, "audio_model_cache_directory", None)
                if cache is None or Path(cache).name != "whisper" or config.audio_model_name != "small":
                    return "not_checked", "custom_audio_model_requires_owner_validation", None
                result = inspect_models(models_root=Path(cache).parent, model_ids=(WHISPER_MODEL_ID,))
            return ("ready" if result["all_prepared"] else "unavailable",
                    "local_model_metadata_present" if result["all_prepared"] else "local_model_assets_missing",
                    str(result["models_root"]))
        probes.append(("local_models", "selected_inference", "route_execution", models_probe))
    if request.apply_requested:
        def kio_probe() -> tuple[PreparationStatus, str, str | None]:
            from neocortex.safety.kio_trash import preflight_kio_trash
            observed = preflight_kio_trash()
            return "ready", "kio_client_configuration_verified", str(observed.client)
        probes.append(("kio_trash", "corpus_mutation", "corpus_effects", kio_probe))
    result = prepare_run(request, boundary, probes=probes)
    result.require_effects_ready()
    return result


__all__ = ["PreparationCheck", "PreparationReport", "PreparationRequest", "PreparationUnavailable",
           "prepare_framework_run", "prepare_run"]
