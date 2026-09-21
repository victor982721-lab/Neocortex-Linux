"""Reproducible, offline Identify/ZIP Intake benchmark.

The benchmark deliberately uses a synthetic tree and a fresh state directory
for every mode.  It never selects the configured NeoCortex corpus and it never
uses ``--apply``: the ZIP stage is measured in plan mode so generic containers
cannot mutate even a temporary tree while the scheduler is being compared.

The runner is intentionally in-process.  That lets it observe the detector,
ZIP classifier, progress callback, and executor seams without replacing the
product's public owners or putting instrumentation in production code.  The
receipt records raw measurements only; it does not claim a before/after
speedup.  Run the same command at two commits and compare the resulting JSON
receipts when a performance claim is needed.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final
from unittest.mock import patch


REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))


BENCHMARK_SCHEMA: Final = "neocortex.identify-zip-benchmark/v1"
FIXTURE_SCHEMA: Final = "neocortex.identify-zip-fixture/v1"
DEFAULT_TXT_COUNT: Final = 4_096
DEFAULT_IMAGE_COUNT: Final = 256
DEFAULT_PDF_COUNT: Final = 128
DEFAULT_GENERIC_ZIP_COUNT: Final = 16
DEFAULT_ATOMIC_ZIP_COUNT: Final = 24
DEFAULT_WRONG_EXTENSION_ZIP_COUNT: Final = 16
DEFAULT_ONE_MB_BYTES: Final = 1_500_000
DEFAULT_TEN_MB_BYTES: Final = 11_000_000
DEFAULT_SLOW_DELAY_SECONDS: Final = 0.250
DEFAULT_TIMEOUT_SECONDS: Final = 900.0


class BenchmarkConfigurationError(ValueError):
    """The requested synthetic benchmark is outside its bounded contract."""


class BenchmarkExecutionError(RuntimeError):
    """The benchmark could not complete a comparable measurement."""


@dataclass(frozen=True, slots=True)
class FixtureManifest:
    """Stable description of one generated synthetic corpus."""

    files: int
    bytes: int
    digest_sha256: str
    categories: Mapping[str, int]
    slow_candidate: str
    large_one_mb_bytes: int
    large_ten_mb_bytes: int

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": FIXTURE_SCHEMA,
            "files": self.files,
            "bytes": self.bytes,
            "digest_sha256": self.digest_sha256,
            "categories": dict(sorted(self.categories.items())),
            "slow_candidate": self.slow_candidate,
            "large_one_mb_bytes": self.large_one_mb_bytes,
            "large_ten_mb_bytes": self.large_ten_mb_bytes,
            "synthetic": True,
        }


@dataclass(frozen=True, slots=True)
class BenchmarkConfig:
    """Bounded fixture and run configuration."""

    txt_count: int = DEFAULT_TXT_COUNT
    image_count: int = DEFAULT_IMAGE_COUNT
    pdf_count: int = DEFAULT_PDF_COUNT
    generic_zip_count: int = DEFAULT_GENERIC_ZIP_COUNT
    atomic_zip_count: int = DEFAULT_ATOMIC_ZIP_COUNT
    wrong_extension_zip_count: int = DEFAULT_WRONG_EXTENSION_ZIP_COUNT
    large_one_mb_bytes: int = DEFAULT_ONE_MB_BYTES
    large_ten_mb_bytes: int = DEFAULT_TEN_MB_BYTES
    slow_delay_seconds: float = DEFAULT_SLOW_DELAY_SECONDS
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    modes: tuple[str, ...] = ("unlimited", "s10", "s1")

    def validate(self) -> "BenchmarkConfig":
        non_negative = (
            "txt_count",
            "image_count",
            "pdf_count",
            "generic_zip_count",
            "atomic_zip_count",
            "wrong_extension_zip_count",
        )
        for name in non_negative:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise BenchmarkConfigurationError(f"{name} must be a non-negative integer")
        for name in ("large_one_mb_bytes", "large_ten_mb_bytes"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise BenchmarkConfigurationError(f"{name} must be a positive integer")
        if self.large_ten_mb_bytes <= self.large_one_mb_bytes:
            raise BenchmarkConfigurationError("large_ten_mb_bytes must exceed large_one_mb_bytes")
        for name in ("slow_delay_seconds", "timeout_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                raise BenchmarkConfigurationError(f"{name} must be a non-negative number")
        if self.timeout_seconds < 1:
            raise BenchmarkConfigurationError("timeout_seconds must be at least one second")
        allowed = {"unlimited", "s10", "s1"}
        if not self.modes or any(mode not in allowed for mode in self.modes):
            raise BenchmarkConfigurationError(
                "modes must contain one or more of unlimited, s10, s1"
            )
        if len(set(self.modes)) != len(self.modes):
            raise BenchmarkConfigurationError("modes cannot repeat")
        return self


def _payload(label: str, length: int) -> bytes:
    """Return deterministic bytes without relying on random or external data."""

    seed = f"neocortex-identify-zip-fixture-v1:{label}".encode("ascii")
    return (seed * ((length + len(seed) - 1) // len(seed)))[:length]


def _png_payload(index: int) -> bytes:
    # The detector only needs the PNG signature for this benchmark.  Keep the
    # remainder deterministic and small; this is not a visual-quality fixture.
    return b"\x89PNG\r\n\x1a\n" + _payload(f"png:{index}", 96)


def _pdf_payload(index: int) -> bytes:
    return (
        b"%PDF-1.4\n"
        + _payload(f"pdf:{index}", 96)
        + b"\n%%EOF\n"
    )


def _ooxml_members(kind: str, index: int) -> tuple[tuple[str, bytes], ...]:
    marker = {
        "docx": "word/document.xml",
        "xlsx": "xl/workbook.xml",
        "pptx": "ppt/presentation.xml",
    }[kind]
    return (
        ("[Content_Types].xml", b"<Types xmlns=\"urn:fixture\"><Override/></Types>"),
        (marker, f"<root fixture=\"{kind}-{index}\"/>".encode("ascii")),
    )


def _atomic_members(kind: str, index: int) -> tuple[tuple[str, bytes], ...]:
    if kind in {"docx", "xlsx", "pptx"}:
        return _ooxml_members(kind, index)
    if kind == "odf":
        return (
            ("mimetype", b"application/vnd.oasis.opendocument.text"),
            ("content.xml", f"<office-body>{index}</office-body>".encode("ascii")),
            ("META-INF/manifest.xml", b"<manifest xmlns=\"urn:fixture\"/>"),
        )
    if kind == "epub":
        return (
            ("mimetype", b"application/epub+zip"),
            ("META-INF/container.xml", b"<container xmlns=\"urn:fixture\"/>"),
            ("OEBPS/content.xhtml", f"<html>{index}</html>".encode("ascii")),
        )
    if kind == "apk":
        return (("AndroidManifest.xml", _payload(f"apk:{index}", 96)),)
    if kind == "jar":
        return (("META-INF/MANIFEST.MF", b"Manifest-Version: 1.0\n"),)
    raise BenchmarkConfigurationError(f"unsupported atomic fixture kind: {kind}")


def _write_zip(path: Path, members: Iterable[tuple[str, bytes]], *, stored_first: bool = False) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in members:
            compression = zipfile.ZIP_STORED if stored_first and name == "mimetype" else zipfile.ZIP_DEFLATED
            archive.writestr(zipfile.ZipInfo(name), payload, compress_type=compression)
            total += len(payload)
    return total


def _iter_file_bytes(root: Path) -> Iterable[tuple[str, bytes]]:
    for path in sorted((candidate for candidate in root.rglob("*") if candidate.is_file()), key=lambda p: p.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        yield relative, path.read_bytes()


def _tree_digest(root: Path) -> tuple[int, int, str]:
    digest = hashlib.sha256()
    files = total_bytes = 0
    for relative, payload in _iter_file_bytes(root):
        encoded = relative.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
        files += 1
        total_bytes += len(payload)
    return files, total_bytes, digest.hexdigest()


def generate_fixture(root: Path, config: BenchmarkConfig | None = None) -> FixtureManifest:
    """Create one deterministic fixture tree and return its content manifest."""

    cfg = (config or BenchmarkConfig()).validate()
    root = Path(root)
    if not root.is_absolute():
        raise BenchmarkConfigurationError("fixture root must be absolute")
    if root.exists() or os.path.lexists(root):
        raise BenchmarkConfigurationError("fixture root must not already exist")
    root.mkdir(mode=0o700, parents=True)
    categories: Counter[str] = Counter()

    for index in range(cfg.txt_count):
        path = root / "text" / f"note-{index:06d}.txt"
        path.parent.mkdir(exist_ok=True)
        path.write_bytes((f"fixture text {index}\n" + "proteccion transformador\n").encode("utf-8"))
        categories["txt"] += 1

    slow_name = "text/00-slow-candidate.txt"
    slow_path = root / slow_name
    slow_path.write_text("deliberately slow Identify candidate\n", encoding="utf-8")
    categories["slow_candidate"] += 1

    for index in range(cfg.image_count):
        path = root / "images" / f"image-{index:06d}.png"
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(_png_payload(index))
        categories["image"] += 1

    for index in range(cfg.pdf_count):
        path = root / "pdf" / f"report-{index:06d}.pdf"
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(_pdf_payload(index))
        categories["pdf"] += 1

    for index in range(cfg.generic_zip_count):
        path = root / "zip" / f"generic-{index:06d}.zip"
        _write_zip(
            path,
            (
                ("payload/readme.txt", f"generic archive {index}\n".encode("ascii")),
                ("payload/data.txt", _payload(f"generic:{index}", 256)),
            ),
        )
        categories["generic_zip"] += 1

    atomic_kinds = ("docx", "xlsx", "pptx", "odf", "epub", "apk", "jar")
    for index in range(cfg.atomic_zip_count):
        kind = atomic_kinds[index % len(atomic_kinds)]
        suffix = ".docx" if kind == "docx" else ".xlsx" if kind == "xlsx" else ".pptx" if kind == "pptx" else ".odt" if kind == "odf" else ".epub" if kind == "epub" else ".apk" if kind == "apk" else ".jar"
        path = root / "atomic" / f"package-{index:06d}{suffix}"
        _write_zip(path, _atomic_members(kind, index), stored_first=kind in {"odf", "epub"})
        categories["atomic_zip"] += 1

    for index in range(cfg.wrong_extension_zip_count):
        path = root / "wrong-extension" / f"payload-{index:06d}.bin"
        _write_zip(
            path,
            (("payload.txt", f"wrong extension generic ZIP {index}\n".encode("ascii")),),
        )
        categories["wrong_extension_zip"] += 1

    large_one = root / "large" / "oversize-1mb.bin"
    large_one.parent.mkdir(exist_ok=True)
    large_one.write_bytes(_payload("large-one", cfg.large_one_mb_bytes))
    categories["over_1mb"] += 1

    large_ten = root / "large" / "oversize-10mb.bin"
    large_ten.write_bytes(_payload("large-ten", cfg.large_ten_mb_bytes))
    categories["over_10mb"] += 1

    files, total_bytes, digest = _tree_digest(root)
    return FixtureManifest(
        files=files,
        bytes=total_bytes,
        digest_sha256=digest,
        categories=dict(categories),
        slow_candidate=slow_name,
        large_one_mb_bytes=cfg.large_one_mb_bytes,
        large_ten_mb_bytes=cfg.large_ten_mb_bytes,
    )


@dataclass(slots=True)
class _Measurement:
    """Process-local counters collected only for one benchmark mode."""

    mode: str
    started: float = field(default_factory=time.perf_counter)
    detector_calls: int = 0
    detector_zip_calls: int = 0
    detector_open_calls: int = 0
    detector_read_bytes: int = 0
    zip_candidate_calls: int = 0
    zip_candidate_hits: int = 0
    zip_signature_reads: int = 0
    zip_classification_calls: int = 0
    zip_classification_kinds: Counter[str] = field(default_factory=Counter)
    zip_source_digest_calls: int = 0
    zip_source_digest_bytes: int = 0
    detector_active: int = 0
    max_inflight_observations: int = 0
    executor_submissions: int = 0
    executor_inflight: int = 0
    max_inflight_futures: int = 0
    identify_calls: int = 0
    identify_elapsed_seconds: float = 0.0
    inventory_calls: int = 0
    inventory_elapsed_seconds: float = 0.0
    zip_stage_calls: int = 0
    zip_elapsed_seconds: float = 0.0
    progress_events: int = 0
    progress_phase_events: Counter[str] = field(default_factory=Counter)
    progress_started: dict[str, float] = field(default_factory=dict)
    progress_elapsed: dict[str, float] = field(default_factory=dict)
    progress_last_by_phase: dict[str, float] = field(default_factory=dict)
    max_progress_stall_by_phase: dict[str, float] = field(default_factory=dict)
    progress_last_time: float | None = None
    max_progress_stall_seconds: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def progress(self, event: object) -> None:
        now = time.perf_counter()
        with self._lock:
            self.progress_events += 1
            if self.progress_last_time is not None:
                self.max_progress_stall_seconds = max(
                    self.max_progress_stall_seconds, now - self.progress_last_time
                )
            self.progress_last_time = now
            phase = getattr(event, "phase", None)
            if not isinstance(phase, str):
                return
            self.progress_phase_events[phase] += 1
            previous_phase = self.progress_last_by_phase.get(phase)
            if previous_phase is not None:
                self.max_progress_stall_by_phase[phase] = max(
                    self.max_progress_stall_by_phase.get(phase, 0.0), now - previous_phase
                )
            self.progress_last_by_phase[phase] = now
            if phase not in self.progress_started:
                self.progress_started[phase] = now
            if bool(getattr(event, "finished", False)):
                self.progress_elapsed[phase] = now - self.progress_started[phase]

    def enter_observation(self) -> None:
        with self._lock:
            self.detector_active += 1
            self.max_inflight_observations = max(self.max_inflight_observations, self.detector_active)

    def leave_observation(self) -> None:
        with self._lock:
            self.detector_active = max(0, self.detector_active - 1)

    def executor_submit(self) -> None:
        with self._lock:
            self.executor_submissions += 1
            self.executor_inflight += 1
            self.max_inflight_futures = max(self.max_inflight_futures, self.executor_inflight)

    def executor_done(self) -> None:
        with self._lock:
            self.executor_inflight = max(0, self.executor_inflight - 1)

    def to_dict(self) -> dict[str, object]:
        return {
            "detector_calls": self.detector_calls,
            "detector_zip_calls": self.detector_zip_calls,
            "detector_open_calls": self.detector_open_calls,
            "detector_read_bytes": self.detector_read_bytes,
            "zip_candidate_calls": self.zip_candidate_calls,
            "zip_candidate_hits": self.zip_candidate_hits,
            "zip_signature_reads": self.zip_signature_reads,
            "zip_classification_calls": self.zip_classification_calls,
            "zip_classification_kinds": dict(sorted(self.zip_classification_kinds.items())),
            "zip_source_digest_calls": self.zip_source_digest_calls,
            "zip_source_digest_bytes": self.zip_source_digest_bytes,
            "max_inflight_observations": self.max_inflight_observations,
            "executor_submissions": self.executor_submissions,
            "max_inflight_futures": self.max_inflight_futures,
        }


class _CountingStream:
    """Small file proxy used only around the detector's bounded header read."""

    def __init__(self, stream: Any, measurement: _Measurement):
        self._stream = stream
        self._measurement = measurement

    def __enter__(self) -> "_CountingStream":
        self._stream.__enter__()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> object:
        return self._stream.__exit__(exc_type, exc, traceback)

    def read(self, *args: object, **kwargs: object) -> object:
        value = self._stream.read(*args, **kwargs)
        if isinstance(value, (bytes, bytearray, memoryview)):
            self._measurement.detector_read_bytes += len(value)
        return value

    def __getattr__(self, name: str) -> object:
        return getattr(self._stream, name)


class _ObservedExecutor(concurrent.futures.ThreadPoolExecutor):
    """Track Identify futures without changing executor scheduling."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        self._measurement = _ACTIVE_MEASUREMENT
        prefix = kwargs.get("thread_name_prefix")
        self._observe_identify = prefix == "neocortex-identify"
        super().__init__(*args, **kwargs)

    def submit(self, fn: Callable[..., object], /, *args: object, **kwargs: object) -> concurrent.futures.Future[object]:
        future = super().submit(fn, *args, **kwargs)
        if self._observe_identify and self._measurement is not None:
            measurement = self._measurement
            measurement.executor_submit()

            def finished(_future: concurrent.futures.Future[object]) -> None:
                measurement.executor_done()

            future.add_done_callback(finished)
        return future


_ACTIVE_MEASUREMENT: _Measurement | None = None


def _git_head() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPOSITORY_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    value = result.stdout.strip()
    return value if value else None


def _git_state() -> dict[str, object]:
    """Capture whether the measured checkout is a clean commit."""

    try:
        result = subprocess.run(
            ["git", "status", "--porcelain=v1"],
            cwd=REPOSITORY_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return {"dirty": None, "status_sha256": None}
    status = result.stdout.encode("utf-8")
    return {
        "dirty": bool(status),
        "status_sha256": hashlib.sha256(status).hexdigest(),
    }


def _max_file_bytes(mode: str) -> int | None:
    return {"unlimited": None, "s10": 10_000_000, "s1": 1_000_000}[mode]


def _noop_route(_context: object) -> dict[str, object]:
    """Keep the benchmark scoped to Framework/Identify/ZIP, not heavy routes."""

    return {"benchmark": "noop"}


def _measurement_patches(measurement: _Measurement, slow_candidate: str, delay: float) -> ExitStack:
    """Install bounded, reversible probes for one run."""

    from neocortex.capabilities.formats.archive import intake as archive_intake
    from neocortex.platform import content_types
    from neocortex.runtime.orchestration import orchestrator_pipeline
    from neocortex.workflow import zip_intake_orchestrator
    from neocortex.workflow.actions import action_identify
    from neocortex.workflow.actions.actions import FrameworkActions

    stack = ExitStack()
    global _ACTIVE_MEASUREMENT
    _ACTIVE_MEASUREMENT = measurement

    original_detector = FrameworkActions._detector_function()

    def detector(path: str | Path) -> object:
        measurement.detector_calls += 1
        if Path(path).name == Path(slow_candidate).name and delay:
            time.sleep(delay)
        detected = original_detector(path)
        evidence = getattr(detected, "evidence", "")
        if isinstance(evidence, str) and (evidence.startswith("zip:") or evidence == "magic:zip"):
            measurement.detector_zip_calls += 1
        return detected

    def detector_factory() -> Callable[[str | Path], object]:
        return detector

    stack.enter_context(patch.object(FrameworkActions, "_detector_function", staticmethod(detector_factory)))

    original_observe = action_identify.observe_content_type

    def observe(*args: object, **kwargs: object) -> object:
        measurement.enter_observation()
        try:
            return original_observe(*args, **kwargs)
        finally:
            measurement.leave_observation()

    stack.enter_context(patch.object(action_identify, "observe_content_type", observe))

    original_open = getattr(content_types, "open", open)

    def detector_open(*args: object, **kwargs: object) -> _CountingStream:
        measurement.detector_open_calls += 1
        return _CountingStream(original_open(*args, **kwargs), measurement)

    stack.enter_context(patch.object(content_types, "open", detector_open, create=True))

    original_decider = getattr(zip_intake_orchestrator, "_decide_zip_candidate", None)
    if callable(original_decider):

        def decide_candidate(*args: object, **kwargs: object) -> object:
            path_value = args[1] if len(args) > 1 else kwargs.get("path")
            path = Path(path_value) if isinstance(path_value, (str, os.PathLike)) else None
            measurement.zip_candidate_calls += 1
            if path is not None and path.suffix.casefold() != ".zip":
                measurement.zip_signature_reads += 1
            result = original_decider(*args, **kwargs)
            if result is not None:
                measurement.zip_candidate_hits += 1
            return result

        stack.enter_context(
            patch.object(zip_intake_orchestrator, "_decide_zip_candidate", decide_candidate)
        )
    else:
        original_candidate = getattr(zip_intake_orchestrator, "_zip_candidate", None)
        if callable(original_candidate):

            def candidate(path: Path) -> bool:
                measurement.zip_candidate_calls += 1
                if Path(path).suffix.casefold() != ".zip":
                    measurement.zip_signature_reads += 1
                result = original_candidate(path)
                if result:
                    measurement.zip_candidate_hits += 1
                return result

            stack.enter_context(patch.object(zip_intake_orchestrator, "_zip_candidate", candidate))

    # The ZIP owner has two compatible names across the intake transition:
    # current code calls the identity-bound ``_decide_zip`` directly, while
    # the pre-integration implementation called ``_classify_zip``.  Probe the
    # effective runtime rather than inventing a second detector.
    classify_name = next(
        (name for name in ("_decide_zip", "_classify_zip", "classify_zip") if hasattr(archive_intake, name)),
        None,
    )
    if classify_name is not None:
        original_classify = getattr(archive_intake, classify_name)

        def classify(*args: object, **kwargs: object) -> object:
            measurement.zip_classification_calls += 1
            result = original_classify(*args, **kwargs)
            classification = getattr(result, "classification", result)
            kind = getattr(classification, "kind", None)
            if isinstance(kind, str):
                measurement.zip_classification_kinds[kind] += 1
            return result

        stack.enter_context(patch.object(archive_intake, classify_name, classify))

    original_digest = getattr(archive_intake, "_source_sha256", None)
    if callable(original_digest):

        def source_digest(path: Path, *, deadline: object, **kwargs: object) -> str:
            measurement.zip_source_digest_calls += 1
            try:
                measurement.zip_source_digest_bytes += int(path.stat().st_size)
            except OSError:
                pass
            try:
                return original_digest(path, deadline=deadline, **kwargs)
            except TypeError as exc:
                if "progress" not in str(exc) or not kwargs:
                    raise
                return original_digest(path, deadline=deadline)

        stack.enter_context(patch.object(archive_intake, "_source_sha256", source_digest))

    original_identify = FrameworkActions.identify_and_normalize

    def identify(self: object, *args: object, **kwargs: object) -> object:
        measurement.identify_calls += 1
        started = time.perf_counter()
        try:
            return original_identify(self, *args, **kwargs)
        finally:
            measurement.identify_elapsed_seconds += time.perf_counter() - started

    stack.enter_context(patch.object(FrameworkActions, "identify_and_normalize", identify))

    original_inventory = orchestrator_pipeline.InitialPipelineMixin._prepare_normal_inventory

    def inventory(self: object, *args: object, **kwargs: object) -> object:
        measurement.inventory_calls += 1
        started = time.perf_counter()
        try:
            return original_inventory(self, *args, **kwargs)
        finally:
            measurement.inventory_elapsed_seconds += time.perf_counter() - started

    stack.enter_context(patch.object(orchestrator_pipeline.InitialPipelineMixin, "_prepare_normal_inventory", inventory))

    original_zip_stage = zip_intake_orchestrator.run_zip_intake_stage

    def zip_stage(*args: object, **kwargs: object) -> object:
        measurement.zip_stage_calls += 1
        started = time.perf_counter()
        try:
            return original_zip_stage(*args, **kwargs)
        finally:
            measurement.zip_elapsed_seconds += time.perf_counter() - started

    stack.enter_context(patch.object(zip_intake_orchestrator, "run_zip_intake_stage", zip_stage))

    stack.enter_context(patch.object(concurrent.futures, "ThreadPoolExecutor", _ObservedExecutor))

    def close_stack() -> ExitStack:
        return stack

    # Keep a named local for debuggers and static checkers; callers use the
    # ordinary ExitStack protocol and all probes are restored on exit.
    _ = close_stack
    return stack


def _summary_mapping(value: object) -> dict[str, object]:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        converted = dataclasses.asdict(value)
        if isinstance(converted, dict):
            return converted
    if isinstance(value, Mapping):
        return dict(value)
    return {"value": str(value)}


def _run_mode(
    root: Path,
    state_directory: Path,
    mode: str,
    fixture: FixtureManifest,
    *,
    delay: float,
    timeout_seconds: float,
) -> dict[str, object]:
    from neocortex.runtime.models import FrameworkConfig
    from neocortex.runtime.orchestration.orchestrator import FrameworkOrchestrator
    from neocortex.runtime.orchestration.route_registry import RouteAdapter

    measurement = _Measurement(mode)
    config = FrameworkConfig(
        root=root,
        state_directory=state_directory,
        apply_actions=False,
        route="all",
        document_catalog_enabled=False,
        max_file_bytes=_max_file_bytes(mode),
        global_min_free_memory_bytes=0,
        global_min_free_commit_bytes=0,
        global_cpu_slots=max(1, min(8, os.cpu_count() or 1)),
        global_max_cpu_load_percent=100.0,
        global_resource_wait_timeout_seconds=10.0,
        heartbeat_interval_seconds=1.0,
    )
    started = time.perf_counter()
    try:
        with _measurement_patches(measurement, fixture.slow_candidate, delay):
            result = FrameworkOrchestrator(
                config,
                progress=measurement.progress,
                route_registry={"benchmark": RouteAdapter("benchmark", _noop_route)},
            ).run_initial()
    except BaseException as exc:
        raise BenchmarkExecutionError(f"mode {mode} failed: {type(exc).__name__}: {exc}") from exc
    elapsed = time.perf_counter() - started
    scan = _summary_mapping(result.scan)
    actions = _summary_mapping(result.actions)
    zip_result = _summary_mapping(result.zip_intake)
    physical_files = int(scan.get("files_seen", scan.get("files", 0)) or 0)
    identify_files = int(actions.get("files_checked", 0) or 0)
    duplicate_classifications = max(
        0,
        measurement.detector_zip_calls
        + measurement.zip_classification_calls
        - measurement.zip_candidate_hits,
    )
    return {
        "mode": mode,
        "max_file_bytes": _max_file_bytes(mode),
        "apply": False,
        "elapsed_seconds": elapsed,
        "cold_state": {
            "state_directory_fresh": True,
            "state_directory": str(state_directory),
            "os_page_cache_reset": False,
            "same_fixture_digest": fixture.digest_sha256,
        },
        "phases": {
            "inventory_elapsed_seconds": measurement.inventory_elapsed_seconds,
            "zip_intake_elapsed_seconds": measurement.zip_elapsed_seconds,
            "identify_elapsed_seconds": measurement.identify_elapsed_seconds,
            "progress_phase_elapsed_seconds": dict(sorted(measurement.progress_elapsed.items())),
        },
        "throughput": {
            "inventory_files_per_second": (
                physical_files / measurement.inventory_elapsed_seconds
                if measurement.inventory_elapsed_seconds > 0
                else 0.0
            ),
            "identify_files_per_second": (
                identify_files / measurement.identify_elapsed_seconds
                if measurement.identify_elapsed_seconds > 0
                else 0.0
            ),
        },
        "scheduler": {
            "max_inflight_observations": measurement.max_inflight_observations,
            "max_inflight_futures": measurement.max_inflight_futures,
            "executor_submissions": measurement.executor_submissions,
            "identify_calls": measurement.identify_calls,
        },
        "io": {
            **measurement.to_dict(),
            "duplicate_zip_classifications": duplicate_classifications,
            "observed_open_read_events": (
                measurement.detector_open_calls
                + measurement.zip_signature_reads
                + measurement.zip_source_digest_calls
            ),
        },
        "progress": {
            "events": measurement.progress_events,
            "phase_events": dict(sorted(measurement.progress_phase_events.items())),
            "max_stall_seconds": measurement.max_progress_stall_seconds,
            "max_stall_seconds_by_phase": dict(
                sorted(measurement.max_progress_stall_by_phase.items())
            ),
        },
        "inventory": scan,
        "identify": actions,
        "zip_intake": zip_result,
        "comparability": {
            "fixture_digest_equal": True,
            "state_directory_fresh": True,
            "apply_disabled": True,
            "speedup_claim": False,
            "note": "Run the same fixture/modes at a second commit before claiming before/after speedup.",
        },
        "timeout_seconds": timeout_seconds,
    }


def run_benchmark(config: BenchmarkConfig | None = None) -> dict[str, object]:
    """Generate one fixture and measure every requested size mode."""

    cfg = (config or BenchmarkConfig()).validate()
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="neocortex-identify-zip-benchmark-") as temporary:
        workspace = Path(temporary)
        canonical = workspace / "fixture"
        fixture = generate_fixture(canonical, cfg)
        results: list[dict[str, object]] = []
        for mode in cfg.modes:
            case_root = workspace / f"corpus-{mode}"
            state_directory = workspace / f"state-{mode}"
            shutil.copytree(canonical, case_root)
            state_directory.mkdir(mode=0o700)
            case_files, case_bytes, case_digest = _tree_digest(case_root)
            if (case_files, case_bytes, case_digest) != (
                fixture.files,
                fixture.bytes,
                fixture.digest_sha256,
            ):
                raise BenchmarkExecutionError(f"fixture clone changed before mode {mode}")
            results.append(
                _run_mode(
                    case_root,
                    state_directory,
                    mode,
                    fixture,
                    delay=cfg.slow_delay_seconds,
                    timeout_seconds=cfg.timeout_seconds,
                )
            )
            after_files, after_bytes, after_digest = _tree_digest(case_root)
            if (after_files, after_bytes, after_digest) != (
                fixture.files,
                fixture.bytes,
                fixture.digest_sha256,
            ):
                raise BenchmarkExecutionError(
                    f"plan-mode benchmark mutated synthetic corpus for mode {mode}"
                )
    return {
        "schema": BENCHMARK_SCHEMA,
        "status": "complete",
        "git_head": _git_head(),
        "git_state": _git_state(),
        "elapsed_seconds": time.perf_counter() - started,
        "fixture": fixture.to_dict(),
        "modes": results,
        "safe_scope": {
            "synthetic_temporary_corpus": True,
            "personal_corpus_selected": False,
            "apply_executed": False,
            "network_used": False,
            "state_cold_per_mode": True,
        },
        "before_after": {
            "measured_in_this_receipt": False,
            "speedup_claim": False,
            "required": "Run identical configuration at the baseline and fixed commits and compare raw mode rows.",
        },
    }


def compare_receipts(before: Mapping[str, object], after: Mapping[str, object]) -> dict[str, object]:
    """Return conservative raw deltas without inventing a speedup claim."""

    before_fixture = before.get("fixture")
    after_fixture = after.get("fixture")
    if not isinstance(before_fixture, Mapping) or not isinstance(after_fixture, Mapping):
        raise BenchmarkConfigurationError("receipts lack fixture manifests")
    if before_fixture.get("digest_sha256") != after_fixture.get("digest_sha256"):
        raise BenchmarkConfigurationError("before/after fixture digests differ")

    def rows(value: Mapping[str, object]) -> dict[str, Mapping[str, object]]:
        raw = value.get("modes")
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
            raise BenchmarkConfigurationError("receipt lacks mode rows")
        result: dict[str, Mapping[str, object]] = {}
        for row in raw:
            if isinstance(row, Mapping) and isinstance(row.get("mode"), str):
                result[str(row["mode"])] = row
        return result

    before_rows = rows(before)
    after_rows = rows(after)
    if set(before_rows) != set(after_rows):
        raise BenchmarkConfigurationError("before/after mode sets differ")

    deltas: dict[str, object] = {}
    for mode in sorted(before_rows):
        left = before_rows[mode]
        right = after_rows[mode]
        left_phase = left.get("phases") if isinstance(left.get("phases"), Mapping) else {}
        right_phase = right.get("phases") if isinstance(right.get("phases"), Mapping) else {}
        deltas[mode] = {
            "inventory_elapsed_delta_seconds": float(right_phase.get("inventory_elapsed_seconds", 0.0)) - float(left_phase.get("inventory_elapsed_seconds", 0.0)),
            "zip_intake_elapsed_delta_seconds": float(right_phase.get("zip_intake_elapsed_seconds", 0.0)) - float(left_phase.get("zip_intake_elapsed_seconds", 0.0)),
            "identify_elapsed_delta_seconds": float(right_phase.get("identify_elapsed_seconds", 0.0)) - float(left_phase.get("identify_elapsed_seconds", 0.0)),
            "before": left,
            "after": right,
        }
    return {
        "schema": f"{BENCHMARK_SCHEMA}/comparison",
        "fixture_digest_sha256": before_fixture.get("digest_sha256"),
        "speedup_claim": False,
        "deltas": deltas,
    }


def _receipt_path(value: str | Path | None) -> Path | None:
    if value is None:
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise BenchmarkConfigurationError("receipt path must be absolute")
    if path.exists() or os.path.lexists(path):
        raise BenchmarkConfigurationError("receipt path already exists")
    try:
        path.parent.stat()
    except OSError as exc:
        raise BenchmarkConfigurationError("receipt parent directory is unavailable") from exc
    return path


def _write_receipt(path: Path, payload: Mapping[str, object]) -> None:
    encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--txt-count", type=int, default=DEFAULT_TXT_COUNT)
    parser.add_argument("--image-count", type=int, default=DEFAULT_IMAGE_COUNT)
    parser.add_argument("--pdf-count", type=int, default=DEFAULT_PDF_COUNT)
    parser.add_argument("--generic-zip-count", type=int, default=DEFAULT_GENERIC_ZIP_COUNT)
    parser.add_argument("--atomic-zip-count", type=int, default=DEFAULT_ATOMIC_ZIP_COUNT)
    parser.add_argument("--wrong-extension-zip-count", type=int, default=DEFAULT_WRONG_EXTENSION_ZIP_COUNT)
    parser.add_argument("--large-one-mb-bytes", type=int, default=DEFAULT_ONE_MB_BYTES)
    parser.add_argument("--large-ten-mb-bytes", type=int, default=DEFAULT_TEN_MB_BYTES)
    parser.add_argument("--slow-delay-ms", type=float, default=DEFAULT_SLOW_DELAY_SECONDS * 1000)
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--modes", default="unlimited,s10,s1", help="comma-separated modes")
    parser.add_argument("--receipt", type=Path, help="absolute path outside the repository for one JSON receipt")
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    parser = _parser()
    parsed = parser.parse_args(arguments)
    try:
        config = BenchmarkConfig(
            txt_count=parsed.txt_count,
            image_count=parsed.image_count,
            pdf_count=parsed.pdf_count,
            generic_zip_count=parsed.generic_zip_count,
            atomic_zip_count=parsed.atomic_zip_count,
            wrong_extension_zip_count=parsed.wrong_extension_zip_count,
            large_one_mb_bytes=parsed.large_one_mb_bytes,
            large_ten_mb_bytes=parsed.large_ten_mb_bytes,
            slow_delay_seconds=parsed.slow_delay_ms / 1000,
            timeout_seconds=parsed.timeout_seconds,
            modes=tuple(part.strip() for part in parsed.modes.split(",") if part.strip()),
        ).validate()
        report = run_benchmark(config)
        receipt = _receipt_path(parsed.receipt)
        if receipt is not None:
            _write_receipt(receipt, report)
    except (BenchmarkConfigurationError, BenchmarkExecutionError, OSError, RuntimeError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "BENCHMARK_SCHEMA",
    "FIXTURE_SCHEMA",
    "BenchmarkConfig",
    "BenchmarkConfigurationError",
    "BenchmarkExecutionError",
    "FixtureManifest",
    "compare_receipts",
    "generate_fixture",
    "main",
    "run_benchmark",
]
