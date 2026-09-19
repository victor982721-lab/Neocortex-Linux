"""Deterministic, compact processing signatures with auditable provenance.

The signature uses the optional native XXH3 backend when present and the
SHA-256 fallback otherwise. The manifest records the effective backend and
contains only configuration, runtime versions and bounded artifact metadata;
file contents are streamed and never retained in memory.
"""

from __future__ import annotations

import json
import math
import os
import stat
import threading
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from functools import lru_cache, wraps
from importlib import metadata
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from neocortex.foundation.hash_compat import (
    HASH_ALGORITHM_64,
    HASH_ALGORITHM_128,
    HASH_BACKEND,
    HAS_NATIVE_XXHASH,
    hash_backend_component,
)
from neocortex.foundation.hash_compat import xxhash

from neocortex.runtime.control.bounded_subprocess import run_bounded_capture


# region [01] Canonical manifest and signature contracts

PROCESSING_PROVENANCE_SCHEMA = "neocortex.processing-provenance/v1"
ROUTE_SUMMARY_SCHEMA = "neocortex.route-summary/v1"
_SIGNATURE_VERSION = "psig-v1"
_ARTIFACT_READ_BYTES = 1024 * 1024
_VERSION_OUTPUT_MAX_BYTES = 256 * 1024
_LANGUAGE_OUTPUT_MAX_BYTES = 1024 * 1024
_SAFE_SEGMENT = re.compile(r"[^a-zA-Z0-9_.-]+")


_PROVENANCE_REVISION = 0
_PROVENANCE_REVISION_LOCK = threading.RLock()


def processing_provenance_cache(*, maxsize: int) -> Callable[[Callable[..., Any]], Any]:
    """Bound a cache to the public environment revision, including in-flight calls.

    Consumers keep their own cache and imports. Revision is a cache key, never
    part of the public processing signature or persisted manifest. Existing
    cache_clear/cache_info test seams remain available on the wrapper.
    """
    def decorate(function: Callable[..., Any]) -> Any:
        @lru_cache(maxsize=maxsize)
        def cached(revision: int, *args: Any, **kwargs: Any) -> Any:
            return function(*args, **kwargs)

        @wraps(function)
        def call(*args: Any, **kwargs: Any) -> Any:
            for _attempt in range(3):
                with _PROVENANCE_REVISION_LOCK:
                    revision = _PROVENANCE_REVISION
                result = cached(revision, *args, **kwargs)
                with _PROVENANCE_REVISION_LOCK:
                    if revision == _PROVENANCE_REVISION:
                        return result
            raise RuntimeError("processing provenance changed repeatedly during observation")

        call.cache_clear = cached.cache_clear  # type: ignore[attr-defined]
        call.cache_info = cached.cache_info  # type: ignore[attr-defined]
        return call
    return decorate


class ProcessingArtifactChangedError(RuntimeError):
    """A behavior-affecting file changed during a bounded observation."""


# dev/ino distinguish replacements; ctime also catches in-place rewrites that
# restore mtime/size. Mode prevents cached results bypassing non-regular checks.
_ArtifactIdentity = tuple[int, int, int, int, int, int]


def _artifact_identity(value: os.stat_result) -> _ArtifactIdentity:
    if not stat.S_ISREG(value.st_mode):
        raise FileNotFoundError("processing artifact must be a regular file")
    return (value.st_dev, value.st_ino, value.st_mode, value.st_size,
            value.st_mtime_ns, value.st_ctime_ns)


def _observe_artifact(path: Path) -> tuple[Path, _ArtifactIdentity]:
    resolved = path.expanduser().resolve(strict=True)
    return resolved, _artifact_identity(resolved.stat())


def _require_same_artifact(path: Path, resolved: Path, expected: _ArtifactIdentity) -> None:
    current, identity = _observe_artifact(path)
    if current != resolved or identity != expected:
        raise ProcessingArtifactChangedError("processing artifact changed during observation")


@dataclass(frozen=True, slots=True)
class ProcessingProvenance:
    """Immutable signature plus the exact canonical manifest that produced it."""

    signature: str
    manifest_json: str

    @property
    def manifest(self) -> dict[str, Any]:
        value = json.loads(self.manifest_json)
        if not isinstance(value, dict):  # pragma: no cover - constructor invariant
            raise TypeError("processing provenance manifest must be an object")
        return value


def _safe_segment(value: str) -> str:
    result = _SAFE_SEGMENT.sub("-", value.strip()).strip("-")
    if not result:
        raise ValueError("processing signature segment cannot be blank")
    return result


def _canonical_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("processing provenance cannot contain non-finite floats")
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise TypeError("processing provenance keys must be non-empty strings")
            normalized[key] = _canonical_value(item)
        return {key: normalized[key] for key in sorted(normalized)}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    raise TypeError(f"unsupported processing provenance value: {type(value).__name__}")


def build_processing_provenance(
    pipeline: str,
    algorithm_version: str,
    configuration: Mapping[str, Any],
    components: Iterable[Mapping[str, Any]],
    *,
    compatibility_tag: str,
) -> ProcessingProvenance:
    """Build a stable signature from sorted, canonical processing inputs."""

    normalized_components = [_canonical_value(component) for component in components]
    names: set[str] = set()
    for component in normalized_components:
        if not isinstance(component, dict):
            raise TypeError("processing components must be objects")
        name = component.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("every processing component requires a name")
        if name in names:
            raise ValueError(f"duplicate processing component: {name}")
        names.add(name)
    # Ordinary string names are unique, so serialization cannot break a tie.
    # Subclasses may customize comparison or conversion; retain the original
    # composite key for them, including its canonical-JSON tie breaker.
    if all(type(name) is str for name in names):
        normalized_components.sort(key=lambda item: item["name"])
    else:
        normalized_components.sort(
            key=lambda item: (
                str(item["name"]),
                json.dumps(item, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
            )
        )
    # Reuse the already canonical component trees instead of visiting twice.
    manifest = {
        "schema": PROCESSING_PROVENANCE_SCHEMA,
        "pipeline": _canonical_value(pipeline),
        "algorithm_version": _canonical_value(algorithm_version),
        "configuration": _canonical_value(configuration),
        "components": normalized_components,
    }
    manifest_json = json.dumps(
        manifest,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = xxhash.xxh3_128(manifest_json.encode("utf-8")).hexdigest()
    signature = "|".join(
        (
            _SIGNATURE_VERSION,
            _safe_segment(pipeline),
            _safe_segment(compatibility_tag),
            digest,
        )
    )
    return ProcessingProvenance(signature, manifest_json)


# endregion [01]


# region [02] Python distributions and streamed artifacts


def installed_distribution_version(distribution: str) -> str | None:
    """Return the installed distribution version without importing its package."""

    try:
        return metadata.version(distribution)
    except metadata.PackageNotFoundError:
        return None


def python_runtime_component() -> dict[str, Any]:
    """Describe the interpreter whose standard library affects extraction."""

    return {
        "name": "python-runtime",
        "kind": "runtime",
        "implementation": platform.python_implementation(),
        "version": platform.python_version(),
        "cache_tag": sys.implementation.cache_tag,
        "hash_backend": HASH_BACKEND,
        "hash_algorithm_128": HASH_ALGORITHM_128,
        "hash_algorithm_64": HASH_ALGORITHM_64,
    }


@processing_provenance_cache(maxsize=64)
def _fingerprint_file_cached(path: str, identity: _ArtifactIdentity) -> str:
    digest = xxhash.xxh3_128()
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        if _artifact_identity(os.fstat(stream.fileno())) != identity:
            raise ProcessingArtifactChangedError("processing artifact changed before hashing")
        while chunk := stream.read(_ARTIFACT_READ_BYTES):
            digest.update(chunk)
        if _artifact_identity(os.fstat(stream.fileno())) != identity:
            raise ProcessingArtifactChangedError("processing artifact changed while hashing")
    _require_same_artifact(Path(path), Path(path), identity)
    return digest.hexdigest()


def fingerprint_file_xxh3_128(path: Path) -> str:
    """Return a cached hash only for the same coherently observed file revision."""
    resolved, identity = _observe_artifact(path)
    digest = _fingerprint_file_cached(str(resolved), identity)
    _require_same_artifact(path, resolved, identity)
    return digest


def file_artifact(path: Path, *, label: str | None = None) -> dict[str, Any]:
    """Return metadata and digest from the same verified regular-file revision."""
    resolved, identity = _observe_artifact(path)
    digest = _fingerprint_file_cached(str(resolved), identity)
    _require_same_artifact(path, resolved, identity)
    return {
        "name": label or resolved.name,
        "size_bytes": identity[3],
        "xxh3_128": digest,
        "hash_backend": HASH_BACKEND,
        "hash_algorithm_128": HASH_ALGORITHM_128,
        "hash_algorithm_64": HASH_ALGORITHM_64,
    }


def distribution_component(
    name: str,
    distribution: str,
    *,
    artifact_relative_path: str | None = None,
) -> dict[str, Any]:
    """Describe an installed Python distribution and an optional bundled model."""

    if distribution.casefold() == "xxhash" and not HAS_NATIVE_XXHASH:
        return hash_backend_component(name)
    version = installed_distribution_version(distribution)
    component: dict[str, Any] = {
        "name": name,
        "kind": "python-distribution",
        "distribution": distribution,
        "status": "available" if version is not None else "unavailable",
        "version": version,
    }
    if artifact_relative_path is None:
        return component
    try:
        installed = metadata.distribution(distribution)
        artifact_path = Path(str(installed.locate_file(artifact_relative_path)))
        component["artifact"] = file_artifact(
            artifact_path,
            label=Path(artifact_relative_path).name,
        )
    except (FileNotFoundError, metadata.PackageNotFoundError, OSError):
        component["artifact"] = {
            "name": Path(artifact_relative_path).name,
            "status": "unavailable",
        }
    return component


# endregion [02]


# region [03] Native executable and Tesseract runtime probes


CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _resolved_executable(explicit: str | None, default_name: str) -> Path:
    if explicit:
        discovered = shutil.which(explicit)
        candidate = Path(discovered or explicit).expanduser().resolve(strict=True)
    else:
        discovered = shutil.which(default_name)
        if discovered is None:
            raise FileNotFoundError(f"{default_name} executable was not found")
        candidate = Path(discovered).resolve(strict=True)
    if not candidate.is_file():
        raise FileNotFoundError(f"executable is not a file: {candidate}")
    return candidate


def _safe_error(exc: BaseException) -> str:
    return str(exc).encode("utf-8", "replace").decode("utf-8")[:500]


def _completed_output_lines(result: subprocess.CompletedProcess[bytes]) -> list[str]:
    combined = result.stdout + (b"\n" if result.stderr else b"") + result.stderr
    return combined.decode("utf-8", "replace").splitlines()


@processing_provenance_cache(maxsize=32)
def _executable_component_json(
    name: str,
    command_path: str,
    identity: _ArtifactIdentity,
    version_arguments: tuple[str, ...],
    timeout_seconds: float,
) -> str:
    try:
        command = Path(command_path)
        _require_same_artifact(command, command, identity)
        result = run_bounded_capture(
            [str(command), *version_arguments],
            timeout_seconds=timeout_seconds,
            stdout_limit_bytes=_VERSION_OUTPUT_MAX_BYTES,
            stderr_limit_bytes=_VERSION_OUTPUT_MAX_BYTES,
            creationflags=CREATE_NO_WINDOW,
        )
        output = _completed_output_lines(result)
        if result.returncode != 0 or not output:
            raise RuntimeError(f"{command.name} version probe exited {result.returncode}")
        component = {
            "name": name,
            "kind": "native-executable",
            "status": "available",
            "version": output[0].strip()[:300],
            "binary": file_artifact(command, label=command.name),
        }
        _require_same_artifact(command, command, identity)
    except Exception as exc:
        component = {
            "name": name,
            "kind": "native-executable",
            "status": "unavailable",
            "error_type": type(exc).__name__,
        }
    return json.dumps(component, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def executable_component(
    name: str,
    *,
    default_name: str,
    version_arguments: tuple[str, ...] = ("--version",),
    explicit: str | None = None,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    """Reuse a version probe only while the resolved executable is unchanged."""
    try:
        command, identity = _observe_artifact(_resolved_executable(explicit, default_name))
        value = json.loads(_executable_component_json(
            name, str(command), identity, version_arguments, timeout_seconds,
        ))
        _require_same_artifact(_resolved_executable(explicit, default_name), command, identity)
        return value
    except Exception as exc:
        return {"name": name, "kind": "native-executable", "status": "unavailable",
                "error_type": type(exc).__name__}


@dataclass(frozen=True, slots=True)
class TesseractRuntimeProvenance:
    available: bool
    command: str | None
    tessdata_dir: str | None
    version: str | None
    languages: tuple[str, ...]
    component_json: str
    unavailable_reason: str | None = None

    @property
    def component(self) -> dict[str, Any]:
        value = json.loads(self.component_json)
        if not isinstance(value, dict):  # pragma: no cover - constructor invariant
            raise TypeError("Tesseract component must be an object")
        return value

    @property
    def requested_languages(self) -> tuple[str, ...]:
        """Languages covered by the preflight, in deterministic caller order."""

        raw = self.component.get("requested_languages", ())
        if not isinstance(raw, list):
            return ()
        return tuple(value for value in raw if isinstance(value, str))

    @property
    def traineddata_hashes(self) -> tuple[tuple[str, str | None], ...]:
        """Return compact model identities suitable for per-result provenance."""

        raw = self.component.get("traineddata", ())
        if not isinstance(raw, list):
            return ()
        hashes: list[tuple[str, str | None]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            filename = item.get("name")
            if not isinstance(filename, str) or not filename.endswith(".traineddata"):
                continue
            digest = item.get("xxh3_128")
            hashes.append(
                (
                    filename.removesuffix(".traineddata"),
                    digest if isinstance(digest, str) else None,
                )
            )
        return tuple(hashes)


def _tessdata_path_from_listing(output: str) -> Path | None:
    first_line = output.splitlines()[0] if output.splitlines() else ""
    match = re.search(r'["\']([^"\']+)["\']', first_line)
    if match is None:
        return None
    try:
        return Path(match.group(1)).expanduser().resolve(strict=True)
    except OSError:
        return None


@processing_provenance_cache(maxsize=32)
def _resolve_tesseract_runtime_cached(
    explicit: str | None,
    tessdata_dir: str | None,
    requested_languages: tuple[str, ...],
    timeout_seconds: float,
) -> TesseractRuntimeProvenance:
    component: dict[str, Any] = {
        "name": "tesseract",
        "kind": "native-executable",
    }
    try:
        command = _resolved_executable(explicit, "tesseract")
        resolved_tessdata: Path | None = None
        if tessdata_dir:
            resolved_tessdata = Path(tessdata_dir).expanduser().resolve(strict=True)
            if not resolved_tessdata.is_dir():
                raise NotADirectoryError(str(resolved_tessdata))

        version_result = run_bounded_capture(
            [str(command), "--version"],
            timeout_seconds=timeout_seconds,
            stdout_limit_bytes=_VERSION_OUTPUT_MAX_BYTES,
            stderr_limit_bytes=_VERSION_OUTPUT_MAX_BYTES,
            creationflags=CREATE_NO_WINDOW,
        )
        version_lines = _completed_output_lines(version_result)
        if version_result.returncode != 0 or not version_lines:
            raise RuntimeError(f"tesseract --version exited {version_result.returncode}")
        version = version_lines[0].removeprefix("tesseract ").strip()[:300]

        language_command = [str(command)]
        if resolved_tessdata is not None:
            language_command.extend(("--tessdata-dir", str(resolved_tessdata)))
        language_command.append("--list-langs")
        language_result = run_bounded_capture(
            language_command,
            timeout_seconds=timeout_seconds,
            stdout_limit_bytes=_LANGUAGE_OUTPUT_MAX_BYTES,
            stderr_limit_bytes=_VERSION_OUTPUT_MAX_BYTES,
            creationflags=CREATE_NO_WINDOW,
        )
        if language_result.returncode != 0:
            raise RuntimeError(
                language_result.stderr.decode("utf-8", "replace")[:500]
                or f"tesseract --list-langs exited {language_result.returncode}"
            )
        language_output = language_result.stdout.decode("utf-8", "replace")
        available_languages = tuple(
            sorted(line.strip() for line in language_output.splitlines()[1:] if line.strip())
        )
        missing = tuple(
            language for language in requested_languages if language not in available_languages
        )
        if resolved_tessdata is None:
            resolved_tessdata = _tessdata_path_from_listing(language_output)

        artifacts: list[dict[str, Any]] = []
        for language in requested_languages:
            filename = f"{language}.traineddata"
            artifact_path = resolved_tessdata / filename if resolved_tessdata is not None else None
            if artifact_path is not None and artifact_path.is_file():
                artifacts.append(file_artifact(artifact_path, label=filename))
            else:
                artifacts.append({"name": filename, "status": "unresolved"})

        component.update(
            {
                "status": "missing-languages" if missing else "available",
                "version": version,
                "binary": file_artifact(command, label=command.name),
                "requested_languages": requested_languages,
                "missing_languages": missing,
                "traineddata": artifacts,
            }
        )
        component_json = json.dumps(
            component,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        if missing:
            reason = f"missing OCR languages: {', '.join(missing)}"
            return TesseractRuntimeProvenance(
                False,
                str(command),
                str(resolved_tessdata) if resolved_tessdata is not None else None,
                version,
                available_languages,
                component_json,
                reason,
            )
        return TesseractRuntimeProvenance(
            True,
            str(command),
            str(resolved_tessdata) if resolved_tessdata is not None else None,
            version,
            available_languages,
            component_json,
        )
    except Exception as exc:
        component.update(
            {
                "status": "unavailable",
                "error_type": type(exc).__name__,
            }
        )
        return TesseractRuntimeProvenance(
            False,
            None,
            tessdata_dir,
            None,
            (),
            json.dumps(
                component,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ),
            f"{type(exc).__name__}: {_safe_error(exc)}",
        )


def resolve_tesseract_runtime(
    *,
    command: str | None,
    tessdata_dir: str | None,
    language: str,
    timeout_seconds: float,
) -> TesseractRuntimeProvenance:
    """Resolve Tesseract, selected languages and traineddata fingerprints once."""

    requested = tuple(part for part in language.split("+") if part)
    if not requested:
        raise ValueError("Tesseract language must not be blank")
    if timeout_seconds <= 0:
        raise ValueError("Tesseract probe timeout must be positive")
    return _resolve_tesseract_runtime_cached(
        command,
        tessdata_dir,
        requested,
        float(timeout_seconds),
    )


def clear_processing_provenance_caches() -> None:
    """Start a new public provenance revision without importing route owners.

    Old in-flight observations cannot populate a cache key for the new revision.
    Route snapshots already retained by a running extractor remain immutable;
    callers refresh between runs, before constructing route configurations.
    """
    global _PROVENANCE_REVISION
    with _PROVENANCE_REVISION_LOCK:
        _PROVENANCE_REVISION += 1
        _fingerprint_file_cached.cache_clear()
        _executable_component_json.cache_clear()
        _resolve_tesseract_runtime_cached.cache_clear()


# endregion [03]


for _defined_value in tuple(globals().values()):
    if getattr(_defined_value, "__module__", None) == __name__:
        try:
            _defined_value.__module__ = "neocortex.foundation.processing_provenance"
        except (AttributeError, TypeError):
            pass
del _defined_value
