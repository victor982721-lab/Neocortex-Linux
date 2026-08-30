"""Deterministic, compact processing signatures with auditable provenance.

The signature is an XXH3 digest of a canonical JSON manifest.  The manifest
contains only effective configuration, runtime versions and bounded artifact
metadata; file contents are streamed and never retained in memory.
"""

from __future__ import annotations

import json
import math
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from functools import lru_cache
from importlib import metadata
from pathlib import Path
from typing import Any, Iterable, Mapping

import xxhash

from neocortex.runtime.control.bounded_subprocess import run_bounded_capture


# region [01] Canonical manifest and signature contracts

PROCESSING_PROVENANCE_SCHEMA = "neocortex.processing-provenance/v1"
ROUTE_SUMMARY_SCHEMA = "neocortex.route-summary/v1"
_SIGNATURE_VERSION = "psig-v1"
_ARTIFACT_READ_BYTES = 1024 * 1024
_VERSION_OUTPUT_MAX_BYTES = 256 * 1024
_LANGUAGE_OUTPUT_MAX_BYTES = 1024 * 1024
_SAFE_SEGMENT = re.compile(r"[^a-zA-Z0-9_.-]+")


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
    normalized_components.sort(
        key=lambda item: (
            str(item["name"]),
            json.dumps(item, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
        )
    )
    manifest = _canonical_value(
        {
            "schema": PROCESSING_PROVENANCE_SCHEMA,
            "pipeline": pipeline,
            "algorithm_version": algorithm_version,
            "configuration": configuration,
            "components": normalized_components,
        }
    )
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
    }


@lru_cache(maxsize=64)
def _fingerprint_file_cached(path: str, size: int, mtime_ns: int) -> str:
    del size, mtime_ns
    digest = xxhash.xxh3_128()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(_ARTIFACT_READ_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def fingerprint_file_xxh3_128(path: Path) -> str:
    """Fingerprint an artifact incrementally using the project identity algorithm."""

    resolved = path.expanduser().resolve(strict=True)
    stat = resolved.stat()
    if not resolved.is_file():
        raise FileNotFoundError(f"processing artifact is not a file: {resolved}")
    return _fingerprint_file_cached(str(resolved), stat.st_size, stat.st_mtime_ns)


def file_artifact(path: Path, *, label: str | None = None) -> dict[str, Any]:
    """Return bounded metadata for one behavior-affecting runtime artifact."""

    resolved = path.expanduser().resolve(strict=True)
    stat = resolved.stat()
    if not resolved.is_file():
        raise FileNotFoundError(f"processing artifact is not a file: {resolved}")
    return {
        "name": label or resolved.name,
        "size_bytes": stat.st_size,
        "xxh3_128": fingerprint_file_xxh3_128(resolved),
    }


def distribution_component(
    name: str,
    distribution: str,
    *,
    artifact_relative_path: str | None = None,
) -> dict[str, Any]:
    """Describe an installed Python distribution and an optional bundled model."""

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


@lru_cache(maxsize=32)
def _executable_component_json(
    name: str,
    explicit: str | None,
    default_name: str,
    version_arguments: tuple[str, ...],
    timeout_seconds: float,
) -> str:
    try:
        command = _resolved_executable(explicit, default_name)
        result = run_bounded_capture(
            [str(command), *version_arguments],
            timeout_seconds=timeout_seconds,
            stdout_limit_bytes=_VERSION_OUTPUT_MAX_BYTES,
            stderr_limit_bytes=_VERSION_OUTPUT_MAX_BYTES,
            creationflags=CREATE_NO_WINDOW,
        )
        output = _completed_output_lines(result)
        if result.returncode != 0 or not output:
            raise RuntimeError(f"{default_name} version probe exited {result.returncode}")
        component = {
            "name": name,
            "kind": "native-executable",
            "status": "available",
            "version": output[0].strip()[:300],
            "binary": file_artifact(command, label=command.name),
        }
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
    """Probe a native executable once and retain only version/artifact metadata."""

    return json.loads(
        _executable_component_json(
            name,
            explicit,
            default_name,
            version_arguments,
            timeout_seconds,
        )
    )


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


@lru_cache(maxsize=32)
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
    """Clear runtime probe caches for tests or explicit in-process upgrades."""

    _fingerprint_file_cached.cache_clear()
    _executable_component_json.cache_clear()
    _resolve_tesseract_runtime_cached.cache_clear()


# endregion [03]


for _defined_value in tuple(globals().values()):
    if getattr(_defined_value, "__module__", None) == __name__:
        try:
            _defined_value.__module__ = "_04_Nucleo_Operativo.processing_provenance"
        except (AttributeError, TypeError):
            pass
del _defined_value
