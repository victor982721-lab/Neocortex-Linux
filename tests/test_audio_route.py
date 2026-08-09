from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from _02_Deduplicacion import FileSnapshot, snapshot_path
from _04_Nucleo_Operativo.audio_models import (
    AudioProcessingError,
    AudioRouteConfig,
    MediaProbe,
    TranscriptResult,
    TranscriptSegment,
    WhisperRuntime,
)
from _04_Nucleo_Operativo.audio_route import (
    AUDIO_MIME_TYPES,
    AudioRoute,
    search_audio_state,
)
from _04_Nucleo_Operativo.audio_state import audio_database
from _04_Nucleo_Operativo.cli_config import framework_config_from_args
from _04_Nucleo_Operativo.cli_parser import build_parser
from _04_Nucleo_Operativo.cli_validation import validate_arguments
from _04_Nucleo_Operativo.corpus_access import (
    CorpusAccessPolicy,
    CorpusMutationGuard,
)
from _04_Nucleo_Operativo.document_catalog import (
    document_catalog_database,
    update_document_catalog,
)
from _04_Nucleo_Operativo.document_organization import (
    apply_document_organization,
    plan_document_organization,
)
from _04_Nucleo_Operativo.route_filters import CandidateSelection
from _04_Nucleo_Operativo.cancellation import (
    CancellationRequested,
    CancellationToken,
)
from _04_Nucleo_Operativo.audio_whisper import WhisperTranscriber
from tests.internal_paths_test_support import disjoint_internal_paths_policy


# region [01] Deterministic route doubles


RUNTIME = WhisperRuntime("1.2.1", "4.8.1", 0, "cpu", "int8")
PROBE = MediaProbe(12.5, "ogg", "opus", 48_000, 1, 1, 0)


class FakeFrameworkRouteState:
    def __init__(self, candidates: dict[str, tuple[FileSnapshot, ...]]):
        self.candidates = candidates
        self.reviews: list[Any] = []
        self.resolutions: list[Any] = []

    def selected_route_candidate_counts(
        self,
        _run_id: int,
        mime: str,
        max_file_bytes: int | None,
        _route_name: str,
        _selection: CandidateSelection,
    ) -> tuple[int, int]:
        values = self.candidates.get(mime, ())
        eligible = (
            values
            if max_file_bytes is None
            else tuple(item for item in values if item.size <= max_file_bytes)
        )
        return len(values), len(eligible)

    def iter_selected_route_candidates(
        self,
        _run_id: int,
        mime: str,
        _route_name: str,
        _selection: CandidateSelection,
    ):
        yield from self.candidates.get(mime, ())

    def store_review_candidates(self, _run_id: int, candidates) -> None:
        self.reviews.extend(candidates)

    def resolve_review_candidates(
        self,
        _run_id: int,
        route_name: str,
        snapshot: FileSnapshot,
        note: str,
    ) -> None:
        self.resolutions.append((route_name, snapshot.path, note))

    def reconcile_review_candidates(
        self,
        run_id: int,
        route_name: str,
        snapshot: FileSnapshot,
        note: str,
        *,
        evaluated_reason_codes,
        active_reason_codes,
    ) -> None:
        self.resolutions.append(
            (
                run_id,
                route_name,
                snapshot.path,
                note,
                frozenset(evaluated_reason_codes),
                frozenset(active_reason_codes),
            )
        )

    def reconcile_review_candidates_batch(
        self,
        run_id: int,
        route_name: str,
        reconciliations,
    ) -> None:
        for reconciliation in reconciliations:
            self.resolutions.append(
                (
                    run_id,
                    route_name,
                    reconciliation.snapshot.path,
                    reconciliation.resolution_note,
                    frozenset(reconciliation.evaluated_reason_codes),
                    frozenset(reconciliation.active_reason_codes),
                )
            )


class FakeMemoryGate:
    peak_reserved_bytes = 0
    wait_count = 0

    @contextmanager
    def admit(self, estimated_bytes: int):
        self.peak_reserved_bytes = max(self.peak_reserved_bytes, estimated_bytes)
        yield


class FakeTranscriber:
    def __init__(self, result: TranscriptResult):
        self.result = result
        self.calls: list[Path] = []
        self.closed = False

    def transcribe(self, path: Path, *, cancellation) -> TranscriptResult:
        cancellation.checkpoint()
        self.calls.append(path)
        return self.result

    def close(self) -> None:
        self.closed = True


def _result(text: str) -> TranscriptResult:
    segments = (TranscriptSegment(0, 250, 2250, text, -0.2, 0.01),) if text else ()
    return TranscriptResult(
        text=text,
        language="es",
        language_probability=0.99,
        duration_seconds=12.5,
        speech_duration_seconds=2.0 if text else 0.0,
        segments=segments,
        model_name="small",
        backend_version="1.2.1",
        device="cpu",
        compute_type="int8",
    )


def _fake_whisper_worker(task_channel, result_channel, _settings) -> None:
    result_channel.put(("ready", RUNTIME))
    while True:
        task = task_channel.get()
        if task is None:
            return
        request_id, path, _config = task
        result_channel.put(("ok", request_id, _result(f"Transcripción de {path}")))


def _audio_route(
    database: Path,
    source: Path,
    *,
    mime: str = "application/ogg",
    result: TranscriptResult | None = None,
    media_probe=lambda *_args, **_kwargs: PROBE,
):
    framework = FakeFrameworkRouteState({mime: (snapshot_path(source),)})
    transcriber = FakeTranscriber(result or _result("Procedimiento de prueba"))
    factory_calls = []

    def factory(_config, _runtime):
        factory_calls.append(True)
        return transcriber

    route = AudioRoute(
        AudioRouteConfig(
            state_path=database,
            min_free_memory_bytes=0,
            min_free_commit_bytes=0,
        ),
        framework,  # type: ignore[arg-type]
        1,
        runtime_resolver=lambda _device, _compute: RUNTIME,
        transcriber_factory=factory,
        media_probe=media_probe,
        memory_gate=FakeMemoryGate(),
    )
    return route, framework, transcriber, factory_calls


# endregion [01]


# region [02] Incremental transcript cache, search and error review


def test_audio_route_transcribes_segments_and_reuses_cache(tmp_path: Path) -> None:
    source = tmp_path / "PTT-2026-01-01.opus"
    source.write_bytes(b"OggS deterministic fixture")
    database = tmp_path / "audio.sqlite3"
    transcript = "Pruebas eléctricas del transformador en la subestación"
    route, framework, transcriber, factory_calls = _audio_route(
        database, source, result=_result(transcript)
    )

    first = route.run()
    second = route.run()

    assert first.transcribed == 1
    assert first.transcript_chars == len(transcript)
    assert first.transcript_segments == 1
    assert second.cache_hits == 1
    assert len(transcriber.calls) == 1
    assert len(factory_calls) == 1
    assert transcriber.closed
    assert len(framework.resolutions) == 2
    assert all("audio_invalid_container" in item[4] for item in framework.resolutions)
    assert all(item[5] == frozenset() for item in framework.resolutions)
    with audio_database(database, readonly=True) as connection:
        document = connection.execute(
            """SELECT status,language,model_name,device,compute_type,text_chars,
            segment_count FROM documents"""
        ).fetchone()
        segment = connection.execute("SELECT start_ms,end_ms,text FROM segments").fetchone()
    assert tuple(document) == (
        "complete",
        "es",
        "small",
        "cpu",
        "int8",
        len(transcript),
        1,
    )
    assert tuple(segment) == (250, 2250, transcript)
    results = search_audio_state(database, "transformador", 10)
    assert len(results) == 1
    assert results[0]["path"] == str(source)


def test_audio_route_caches_valid_media_without_speech(tmp_path: Path) -> None:
    source = tmp_path / "silencio.mp3"
    source.write_bytes(b"ID3 deterministic fixture")
    database = tmp_path / "audio.sqlite3"
    route, framework, transcriber, _calls = _audio_route(
        database,
        source,
        mime="audio/mpeg",
        result=_result(""),
    )

    summary = route.run()

    assert summary.no_speech == 1
    assert summary.errors == 0
    assert framework.reviews == []
    assert len(transcriber.calls) == 1
    with audio_database(database, readonly=True) as connection:
        row = connection.execute("SELECT status,text_chars,segment_count FROM documents").fetchone()
    assert tuple(row) == ("no_speech", 0, 0)


def test_audio_model_memory_is_reserved_once_until_worker_close(
    tmp_path: Path,
) -> None:
    first = tmp_path / "primero.opus"
    second = tmp_path / "segundo.opus"
    first.write_bytes(b"OggS first fixture")
    second.write_bytes(b"OggS second fixture")
    framework = FakeFrameworkRouteState(
        {
            "application/ogg": (
                snapshot_path(first),
                snapshot_path(second),
            )
        }
    )
    events: list[str] = []

    class PersistentGate(FakeMemoryGate):
        active = False
        admissions = 0

        @contextmanager
        def admit(self, estimated_bytes: int):
            self.admissions += 1
            self.active = True
            self.peak_reserved_bytes = estimated_bytes
            events.append("enter")
            try:
                yield
            finally:
                events.append("exit")
                self.active = False

    gate = PersistentGate()

    class CheckingTranscriber(FakeTranscriber):
        def transcribe(self, path: Path, *, cancellation) -> TranscriptResult:
            assert gate.active
            events.append(f"transcribe:{path.name}")
            return super().transcribe(path, cancellation=cancellation)

        def close(self) -> None:
            assert gate.active
            events.append("close")
            super().close()

    transcriber = CheckingTranscriber(_result("Contenido técnico"))
    route = AudioRoute(
        AudioRouteConfig(
            state_path=tmp_path / "audio.sqlite3",
            min_free_memory_bytes=0,
            min_free_commit_bytes=0,
        ),
        framework,  # type: ignore[arg-type]
        1,
        runtime_resolver=lambda _device, _compute: RUNTIME,
        transcriber_factory=lambda _config, _runtime: transcriber,
        media_probe=lambda *_args, **_kwargs: PROBE,
        memory_gate=gate,
    )

    summary = route.run()

    assert summary.transcribed == 2
    assert gate.admissions == 1
    assert events == [
        "enter",
        "transcribe:primero.opus",
        "transcribe:segundo.opus",
        "close",
        "exit",
    ]


def test_audio_cancellation_closes_model_before_releasing_memory(
    tmp_path: Path,
) -> None:
    source = tmp_path / "cancelado.opus"
    source.write_bytes(b"OggS cancellation fixture")
    framework = FakeFrameworkRouteState({"application/ogg": (snapshot_path(source),)})
    events: list[str] = []

    class TrackingGate(FakeMemoryGate):
        @contextmanager
        def admit(self, estimated_bytes: int):
            self.peak_reserved_bytes = estimated_bytes
            events.append("enter")
            try:
                yield
            finally:
                events.append("exit")

    class CancellingTranscriber(FakeTranscriber):
        def transcribe(self, path: Path, *, cancellation) -> TranscriptResult:
            events.append(f"transcribe:{path.name}")
            raise CancellationRequested("deterministic cancellation")

        def close(self) -> None:
            events.append("close")
            super().close()

    transcriber = CancellingTranscriber(_result("unused"))
    route = AudioRoute(
        AudioRouteConfig(
            state_path=tmp_path / "audio.sqlite3",
            min_free_memory_bytes=0,
            min_free_commit_bytes=0,
        ),
        framework,  # type: ignore[arg-type]
        1,
        runtime_resolver=lambda _device, _compute: RUNTIME,
        transcriber_factory=lambda _config, _runtime: transcriber,
        media_probe=lambda *_args, **_kwargs: PROBE,
        memory_gate=TrackingGate(),
    )

    try:
        route.run()
    except CancellationRequested:
        pass
    else:
        raise AssertionError("audio cancellation did not propagate")

    assert transcriber.closed
    assert events == ["enter", "transcribe:cancelado.opus", "close", "exit"]


def test_invalid_audio_is_only_marked_as_deletion_candidate(tmp_path: Path) -> None:
    source = tmp_path / "corrupto.ogg"
    source.write_bytes(b"not a media container")

    def corrupt_probe(*_args, **_kwargs):
        raise AudioProcessingError(
            "audio_invalid_container",
            "invalid data found when processing input",
            recommendation="deletion_candidate",
            retryable=False,
        )

    route, framework, transcriber, factory_calls = _audio_route(
        tmp_path / "audio.sqlite3",
        source,
        media_probe=corrupt_probe,
    )

    summary = route.run()

    assert summary.errors == 1
    assert summary.deletion_candidates == 1
    assert factory_calls == []
    assert transcriber.calls == []
    assert len(framework.reviews) == 1
    assert framework.reviews[0].recommendation == "deletion_candidate"
    assert source.is_file()


def test_isolated_whisper_supervisor_reuses_one_worker_without_model_download(
    tmp_path: Path,
) -> None:
    source = tmp_path / "mensaje.opus"
    source.write_bytes(b"fixture")
    transcriber = WhisperTranscriber(
        AudioRouteConfig(
            state_path=tmp_path / "unused.sqlite3",
            worker_startup_timeout_seconds=10,
            file_timeout_seconds=10,
            worker_memory_bytes=512 * 1024 * 1024,
        ),
        RUNTIME,
        worker_target=_fake_whisper_worker,
    )
    cancellation = CancellationToken()
    try:
        first = transcriber.transcribe(source, cancellation=cancellation)
        process = transcriber._process
        assert process is not None
        process_id = process.pid
        second = transcriber.transcribe(source, cancellation=cancellation)
        process = transcriber._process
        assert process is not None
        assert process.pid == process_id
    finally:
        transcriber.close()

    assert first.text.startswith("Transcripción de")
    assert second.text == first.text


# endregion [02]


# region [03] Catalog, organization and synchronized audio paths


@pytest.mark.skipif(
    os.name != "nt",
    reason="identity-bound corpus mutation is available only on Windows",
)
def test_transcript_is_classified_organized_and_audio_cache_is_updated(
    tmp_path: Path,
) -> None:
    corpus = tmp_path / "corpus"
    state = tmp_path / "state"
    corpus.mkdir()
    state.mkdir()
    source = corpus / "Procedimiento SERINTRA.opus"
    source.write_bytes(b"OggS deterministic fixture")
    transcript = "Procedimiento e instructivo SERINTRA para mantenimiento de subestaciones"
    route, _framework, _transcriber, _calls = _audio_route(
        state / "audio.sqlite3", source, result=_result(transcript)
    )
    assert route.run().transcribed == 1

    summaries = update_document_catalog(state)
    audio_summary = next(item for item in summaries if item.source_kind == "audio")
    assert audio_summary.classified == 1
    catalog_path = state / "document_catalog.sqlite3"
    with document_catalog_database(catalog_path, readonly=True) as catalog:
        row = catalog.execute(
            """SELECT primary_kind,primary_organization,confidence
            FROM documents WHERE source_kind='audio'"""
        ).fetchone()
    assert row["primary_kind"] == "procedimiento"
    assert row["primary_organization"] == "SERINTRA"
    assert float(row["confidence"]) >= 0.72

    organization_root = tmp_path / "organizados"
    assert plan_document_organization(catalog_path, organization_root).planned == 1
    applied = apply_document_organization(
        catalog_path,
        organization_root,
        mutation_guard=CorpusMutationGuard(
            CorpusAccessPolicy.capture("normal", corpus),
            disjoint_internal_paths_policy(tmp_path),
        ),
        max_actions=1,
    )
    destination = (
        organization_root
        / "Empresas"
        / "SERINTRA"
        / "Audio"
        / "Operacion_y_mantenimiento"
        / "Procedimientos_e_instructivos"
        / source.name
    )
    assert applied.applied == 1
    assert applied.cache_synced == 1
    assert destination.is_file()
    with audio_database(state / "audio.sqlite3", readonly=True) as connection:
        assert connection.execute("SELECT path FROM documents").fetchone()[0] == str(destination)
        assert connection.execute("SELECT path FROM audio_inventory").fetchone()[0] == str(
            destination
        )
        assert connection.execute("SELECT path FROM transcript_fts").fetchone()[0] == str(
            destination
        )


# endregion [03]


# region [04] MIME and CLI contracts


def test_audio_mime_contract_covers_current_indexed_formats() -> None:
    assert {
        "application/ogg",
        "audio/mpeg",
        "audio/wav",
        "audio/flac",
        "audio/mp4",
    }.issubset(AUDIO_MIME_TYPES)


def test_audio_cli_configuration_is_explicit_and_validated() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "--route",
            "audio",
            "--whisper-model",
            "base",
            "--whisper-device",
            "cpu",
            "--audio-language",
            "es",
            "--audio-max-count",
            "25",
            "--no-audio-include-video",
            "--no-audio-vad",
            "--audio-worker-memory-mb",
            "2048",
        ]
    )
    validate_arguments(args)
    config = framework_config_from_args(args)

    assert config.audio_model_name == "base"
    assert config.audio_device == "cpu"
    assert config.audio_language == "es"
    assert config.audio_max_documents == 25
    assert not config.audio_include_video
    assert not config.audio_vad_filter
    assert config.audio_worker_memory_bytes == 2048 * 1024 * 1024


# endregion [04]
