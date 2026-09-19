"""Pure, bounded code reading and analysis, shared by owner and process workers.

This module never opens route state or executes/imports observed source files.
Only registered analyzer implementation recipes are imported in a worker.
"""

from __future__ import annotations

import os
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass, replace

from neocortex.deduplication import FileChangedError, FileSnapshot, stat_matches_snapshot
from neocortex.deduplication.io import native_io_path
from neocortex.runtime.control.cancellation import CancellationRequested, CancellationToken
from neocortex.semantic.semantic_models import fingerprint_bytes

from .code_contracts import (
    AnalysisStatus,
    ArtifactClassification,
    CodeAnalysis,
    CodeFileInput,
    CodeRouteConfig,
    DiagnosticRecord,
    DiagnosticSeverity,
)
from .code_state import SkippedCodeObservation
from .ingestion.code_analyzers import AnalyzerRegistry, AnalyzerSpec
from .ingestion.code_detection import classify_artifact, decode_text, looks_binary


def _read_exact_snapshot(
    snapshot: FileSnapshot,
    limit: int,
    cancellation: CancellationToken | None = None,
) -> bytes:
    """Read a regular non-reparse file once, bounded and identity checked."""

    path = native_io_path(snapshot.path)
    try:
        path_stat = os.lstat(path)
    except OSError as exc:
        raise FileChangedError(f"cannot inspect {snapshot.path}: {exc}") from exc
    file_attributes = int(getattr(path_stat, "st_file_attributes", 0))
    reparse_attribute = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    if stat.S_ISLNK(path_stat.st_mode) or file_attributes & reparse_attribute:
        raise FileChangedError(f"refusing link or reparse point: {snapshot.path}")
    if not stat.S_ISREG(path_stat.st_mode):
        raise FileChangedError(f"refusing non-regular file: {snapshot.path}")
    if snapshot.size > limit:
        raise ValueError(f"file exceeds configured code limit ({snapshot.size}>{limit})")

    flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0))
    flags |= int(getattr(os, "O_NOFOLLOW", 0))
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb", buffering=0) as stream:
            descriptor = None
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or not stat_matches_snapshot(snapshot, before):
                raise FileChangedError(
                    f"inventory identity changed before reading: {snapshot.path}"
                )
            chunks: list[bytes] = []
            remaining = snapshot.size
            while remaining:
                if cancellation is not None:
                    cancellation.checkpoint()
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise FileChangedError(f"unexpected end of file while reading: {snapshot.path}")
                chunks.append(chunk)
                remaining -= len(chunk)
            if stream.read(1):
                raise FileChangedError(f"file grew while reading: {snapshot.path}")
            if cancellation is not None:
                cancellation.checkpoint()
            after = os.fstat(stream.fileno())
            if not stat_matches_snapshot(snapshot, after):
                raise FileChangedError(f"inventory identity changed while reading: {snapshot.path}")
            return b"".join(chunks)
    except FileChangedError:
        raise
    except OSError as exc:
        raise FileChangedError(f"cannot read {snapshot.path}: {exc}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _diagnostic(
    code: str,
    message: str,
    *,
    severity: DiagnosticSeverity = DiagnosticSeverity.ERROR,
    confirmed: bool = True,
) -> DiagnosticRecord:
    return DiagnosticRecord(
        source="neocortex-code-route",
        code=code,
        severity=severity,
        message=message[:4096],
        tool_name="neocortex-code-route",
        tool_version="1",
        confirmed=confirmed,
        confidence=1.0 if confirmed else 0.7,
    )


def validate_code_snapshot(snapshot: FileSnapshot) -> None:
    """Revalidate a queued observation immediately before owner publication."""

    try:
        observed = os.lstat(native_io_path(snapshot.path))
    except OSError as exc:
        raise FileChangedError(f"cannot inspect {snapshot.path}: {exc}") from exc
    if not stat.S_ISREG(observed.st_mode) or not stat_matches_snapshot(snapshot, observed):
        raise FileChangedError(f"inventory identity changed before publication: {snapshot.path}")


@dataclass(slots=True)
class CodeContentProcessor:
    """Analysis collaborators with no SQLite connection or mutable route state."""

    config: CodeRouteConfig
    processing_signature: str
    analyzers: AnalyzerRegistry

    def error_observation(
        self, snapshot: FileSnapshot, exc: Exception
    ) -> SkippedCodeObservation:
        """Preserve retry evidence for failed identity checks and analyzers."""

        expected = isinstance(exc, (FileChangedError, OSError, UnicodeError, ValueError))
        provenance: dict[str, object]
        if expected:
            provenance = {"transient": isinstance(exc, FileChangedError)}
            if isinstance(exc, FileChangedError):
                provenance.update({"retryable": True, "recommendation": "retry"})
        else:
            provenance = {"analyzer_failure": type(exc).__name__}
        return self._skipped_observation(
            snapshot,
            classify_artifact(snapshot.path, ""),
            AnalysisStatus.ERROR,
            _diagnostic(
                type(exc).__name__ if expected else "analyzer_failure",
                str(exc) if expected else f"{type(exc).__name__}: {exc}",
            ),
            provenance=provenance,
        )

    def process_candidate(
        self,
        snapshot: FileSnapshot,
        preloaded_raw: bytes | None = None,
        *,
        cancellation: CancellationToken | None = None,
        reader: Callable[[FileSnapshot, int, CancellationToken | None], bytes] = (
            _read_exact_snapshot
        ),
    ) -> CodeCandidateResult:
        """Read and parse one candidate without touching persistent state."""

        read_ns = 0
        bytes_read = 0
        try:
            if preloaded_raw is None:
                read_started = time.perf_counter_ns()
                raw = reader(snapshot, self.config.max_file_bytes, cancellation)
                read_ns = time.perf_counter_ns() - read_started
                bytes_read = len(raw)
            else:
                raw = preloaded_raw
            result, text_chars, analyze_ns = self._analyze_bytes(snapshot, raw)
            if cancellation is not None:
                cancellation.checkpoint()
            return CodeCandidateResult(result, bytes_read, text_chars, read_ns, analyze_ns)
        except CancellationRequested:
            raise
        except Exception as exc:
            return CodeCandidateResult(
                self.error_observation(snapshot, exc),
                bytes_read=bytes_read,
                read_ns=read_ns,
                stale_inventory=isinstance(exc, FileChangedError),
            )

    def _skipped_observation(
        self,
        snapshot: FileSnapshot,
        classification: ArtifactClassification,
        status: AnalysisStatus,
        diagnostic: DiagnosticRecord,
        *,
        raw: bytes | None = None,
        text: str = "",
        encoding: str | None = None,
        parser_kind: str = "route-guard",
        provenance: dict[str, object] | None = None,
    ) -> SkippedCodeObservation:
        raw_fingerprint = None if raw is None else fingerprint_bytes(raw)
        return SkippedCodeObservation(
            snapshot=snapshot,
            classification=classification,
            processing_signature=self.processing_signature,
            status=status,
            analyzer_id="neocortex-code-route",
            analyzer_version="1",
            parser_kind=parser_kind,
            diagnostic=diagnostic,
            encoding=encoding,
            text_excerpt=text,
            text_truncated=bool(text) and len(text) >= self.config.max_text_chars,
            raw_xxh3_128=(None if raw_fingerprint is None else raw_fingerprint.xxh3_128),
            raw_xxh3_64_guard=(None if raw_fingerprint is None else raw_fingerprint.xxh3_64_guard),
            provenance=provenance or {},
        )

    def _analyze_bytes(
        self, snapshot: FileSnapshot, raw: bytes
    ) -> tuple[CodeAnalysis | SkippedCodeObservation, int, int]:
        if looks_binary(raw):
            classification = classify_artifact(snapshot.path, "")
            return (
                self._skipped_observation(
                    snapshot,
                    classification,
                    AnalysisStatus.BINARY,
                    _diagnostic(
                        "binary_payload",
                        "candidate contains binary control bytes and was not parsed",
                        severity=DiagnosticSeverity.INFO,
                    ),
                    raw=raw,
                    provenance={"binary_probe_bytes": min(len(raw), 8192)},
                ),
                0,
                0,
            )

        text, encoding, encoding_evidence = decode_text(raw, snapshot.path)
        truncated = len(text) > self.config.max_text_chars
        if truncated:
            text = text[: self.config.max_text_chars]
        classification = classify_artifact(snapshot.path, text)
        classification = replace(
            classification,
            evidence=tuple(dict.fromkeys((*classification.evidence, *encoding_evidence))),
        )
        source = CodeFileInput(
            snapshot=snapshot,
            text=text,
            raw_bytes=raw,
            encoding=encoding,
            classification=classification,
            processing_signature=self.processing_signature,
        )

        policy_code = None
        if classification.generated and not self.config.include_generated:
            policy_code = "generated_excluded_by_policy"
        elif classification.vendored and not self.config.include_vendored:
            policy_code = "vendored_excluded_by_policy"
        if policy_code is not None:
            return (
                self._skipped_observation(
                    snapshot,
                    classification,
                    AnalysisStatus.TEXT_ONLY,
                    _diagnostic(
                        policy_code,
                        "artifact remained searchable but structural analysis was disabled",
                        severity=DiagnosticSeverity.INFO,
                    ),
                    raw=raw,
                    text=text,
                    encoding=encoding,
                    parser_kind="policy-text-only",
                    provenance={"policy": policy_code},
                ),
                len(text),
                0,
            )

        started = time.perf_counter_ns()
        analyzer = (
            self.analyzers.analyzer_for(None)
            if truncated
            else self.analyzers.analyzer_for(classification.language)
        )
        analysis = analyzer.analyze(source, self.config)
        analyze_ns = time.perf_counter_ns() - started
        if truncated:
            analysis = replace(
                analysis,
                status=AnalysisStatus.PARTIAL,
                text_truncated=True,
                diagnostics=(
                    *analysis.diagnostics,
                    _diagnostic(
                        "text_limit",
                        "searchable text was bounded by code max_text_chars",
                        severity=DiagnosticSeverity.WARNING,
                    ),
                ),
                provenance={
                    **analysis.provenance,
                    "text_limit_chars": self.config.max_text_chars,
                    "native_parser_skipped": True,
                },
            )
        return analysis, len(text), analyze_ns


@dataclass(frozen=True, slots=True)
class CodeCandidateTask:
    """Only content and immutable processing recipes cross the process pipe."""

    snapshot: FileSnapshot
    config: CodeRouteConfig
    processing_signature: str
    specs: tuple[AnalyzerSpec, ...]
    preloaded_raw: bytes | None = None


@dataclass(frozen=True, slots=True)
class CodeCandidateResult:
    """One bounded result retained until the owner publishes it atomically."""

    observation: CodeAnalysis | SkippedCodeObservation
    bytes_read: int = 0
    text_chars: int = 0
    read_ns: int = 0
    analyze_ns: int = 0
    stale_inventory: bool = False


def process_code_candidate(task: CodeCandidateTask) -> CodeCandidateResult:
    """Spawn-safe worker entry; observed source is parsed as data only."""

    from neocortex.runtime.control.elastic_workers import current_worker_cancellation

    processor = CodeContentProcessor(
        task.config, task.processing_signature, AnalyzerRegistry(task.specs)
    )
    return processor.process_candidate(
        task.snapshot, task.preloaded_raw, cancellation=current_worker_cancellation()
    )
