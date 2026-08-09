from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from _02_Deduplicacion import FileSnapshot, snapshot_path
from _04_Nucleo_Operativo.cancellation import CancellationToken
from _04_Nucleo_Operativo.document_catalog import (
    document_catalog_database,
    list_catalog_documents,
    update_document_catalog,
)
from _04_Nucleo_Operativo.document_cache_sync import synchronize_moved_document
from _04_Nucleo_Operativo.file_identity import file_key_from_snapshot
from _04_Nucleo_Operativo.route_filters import CandidateSelection
from _04_Nucleo_Operativo.text_route import TextRoute, TextRouteConfig
from _04_Nucleo_Operativo.text_state import read_text_status, search_text_state


class FakeTextFrameworkState:
    def __init__(self, candidates: dict[str, tuple[FileSnapshot, ...]]) -> None:
        self.candidates = candidates

    def selected_route_candidate_counts(
        self,
        _run_id: int,
        mime: str,
        max_file_bytes: int | None,
        route_name: str,
        _selection: CandidateSelection,
    ) -> tuple[int, int]:
        assert route_name == "text"
        candidates = self.candidates.get(mime, ())
        eligible = tuple(
            item for item in candidates if max_file_bytes is None or item.size <= max_file_bytes
        )
        return len(candidates), len(eligible)

    def iter_selected_route_candidates(
        self,
        _run_id: int,
        mime: str,
        route_name: str,
        _selection: CandidateSelection,
    ):
        assert route_name == "text"
        yield from self.candidates.get(mime, ())


def _route(
    state: Path,
    candidates: dict[str, tuple[FileSnapshot, ...]],
    *,
    run_id: int = 1,
    **limits,
) -> TextRoute:
    return TextRoute(
        TextRouteConfig(state_path=state, **limits),
        FakeTextFrameworkState(candidates),
        run_id,
        cancellation=CancellationToken(),
    )


def _fixture_corpus(root: Path) -> dict[str, tuple[FileSnapshot, ...]]:
    by_mime: dict[str, list[FileSnapshot]] = {}

    def add(name: str, payload: str | bytes, mime: str) -> None:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload.encode("utf-8") if isinstance(payload, str) else payload)
        by_mime.setdefault(mime, []).append(snapshot_path(path))

    for index in range(15):
        add(
            f"notas/ficha-{index:02d}.txt",
            f"Ficha {index:02d}: coordinación selectiva del relevador diferencial",
            "text/plain",
        )
    add("informe.md", "# Informe\nCalibración del transformador encapsulado", "text/markdown")
    add("mediciones.csv", "equipo,valor\ninterruptor,42\n", "text/csv")
    add("ajustes.tsv", "parametro\tvalor\nretardo\t0.3\n", "text/tab-separated-values")
    add(
        "portal.html",
        "<html><style>oculto</style><body>Manual visible de subestación</body></html>",
        "text/html",
    )
    add("equipos.xml", "<raiz><equipo>Transformador principal</equipo></raiz>", "application/xml")
    add(
        "inventario.json",
        json.dumps({"activo": "pararrayos", "estado": "vigente"}),
        "application/json",
    )
    add(
        "correo.eml",
        (
            "From: Operacion <operacion@example.test>\n"
            "To: Victor <victor@example.test>\n"
            "Subject: Prueba del alimentador norte\n"
            "MIME-Version: 1.0\n"
            "Content-Type: text/plain; charset=utf-8\n\n"
            "Resultado satisfactorio de la protección de sobrecorriente.\n"
        ),
        "message/rfc822",
    )
    add("windows.txt", "Revisión de tensión y conexión", "text/plain")
    add("sin-extension", "Bitácora operativa del interruptor", "text/plain")
    add("config.local", "umbral=proteccion\nmodo=seguro", "text/plain")
    return {mime: tuple(items) for mime, items in by_mime.items()}


def test_indexes_25_generic_text_fixtures_and_replays_exact_cache(tmp_path: Path) -> None:
    candidates = _fixture_corpus(tmp_path / "corpus")
    state = tmp_path / "text.sqlite3"

    first = _route(state, candidates).run()
    second = _route(state, candidates, run_id=2).run()

    assert first.candidate_pool == 25
    assert first.candidates == 25
    assert first.processed == 25
    assert first.extracted == 25
    assert first.errors == 0
    assert first.emails == 1
    assert first.text_chars > 1_000
    assert second.processed == 0
    assert second.cache_hits == 25
    assert second.text_chars == first.text_chars

    status = read_text_status(state)
    assert status.available is True
    assert status.documents == 25
    assert status.complete == 25
    assert status.errors == 0
    assert status.content_kinds >= 8

    hit = search_text_state(state, "relevador diferencial", 5)[0]
    assert hit.content_kind == "txt"
    assert "ficha-" in hit.path
    email = search_text_state(state, "sobrecorriente", 5)[0]
    assert email.content_kind == "email"
    assert email.title == "Prueba del alimentador norte"
    assert email.author == "Operacion <operacion@example.test>"
    html = search_text_state(state, "subestación", 5)[0]
    assert html.content_kind == "html"
    assert "oculto" not in (html.snippet or "")


def test_text_route_refreshes_a_renamed_path_without_duplicate_identity(tmp_path: Path) -> None:
    source = tmp_path / "original.txt"
    source.write_text("protección diferencial de barras", encoding="utf-8")
    state = tmp_path / "text.sqlite3"
    first_snapshot = snapshot_path(source)
    _route(state, {"text/plain": (first_snapshot,)}).run()

    renamed = tmp_path / "renombrado.txt"
    source.rename(renamed)
    second_snapshot = snapshot_path(renamed)
    assert second_snapshot.file_id == first_snapshot.file_id
    replay = _route(state, {"text/plain": (second_snapshot,)}, run_id=2).run()

    assert replay.cache_hits == 1
    assert search_text_state(state, "diferencial", 5)[0].path == os.fspath(renamed)
    with sqlite3.connect(state) as connection:
        assert connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1


def test_text_cache_path_sync_updates_document_and_fts(tmp_path: Path) -> None:
    source = tmp_path / "original.txt"
    destination = tmp_path / "organizados" / "renombrado.txt"
    source.write_text("protección diferencial de barras", encoding="utf-8")
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    snapshot = snapshot_path(source)
    _route(
        state_directory / "text.sqlite3",
        {"text/plain": (snapshot,)},
    ).run()

    result = synchronize_moved_document(
        state_directory,
        source_kind="text",
        file_key=file_key_from_snapshot(snapshot),
        old_path=os.fspath(source),
        new_path=os.fspath(destination),
        volume_id=str(snapshot.volume_id),
        file_id=str(snapshot.file_id),
    )

    assert result.complete
    text_result = next(item for item in result.databases if item.database == "text")
    assert text_result.status == "synced"
    assert text_result.updated_rows == 2
    with sqlite3.connect(state_directory / "text.sqlite3") as connection:
        assert connection.execute("SELECT path FROM documents").fetchone()[0] == os.fspath(
            destination
        )
        assert connection.execute("SELECT path FROM document_fts").fetchone()[0] == os.fspath(
            destination
        )


def test_text_route_records_malformed_input_and_prunes_stale_rows(tmp_path: Path) -> None:
    good = tmp_path / "good.txt"
    bad = tmp_path / "bad.txt"
    good.write_text("evidencia vigente", encoding="utf-8")
    bad.write_bytes(b"\x81\x8d\x8f\x90\x9d")
    state = tmp_path / "text.sqlite3"

    summary = _route(
        state,
        {"text/plain": (snapshot_path(good), snapshot_path(bad))},
    ).run()

    assert summary.extracted == 1
    assert summary.errors == 1
    assert read_text_status(state).errors == 1
    pruned = _route(state, {}, run_id=2).run()
    assert pruned.cache_documents_pruned == 2
    assert read_text_status(state).documents == 0


def test_text_route_enforces_size_and_count_limits(tmp_path: Path) -> None:
    paths = []
    for index in range(3):
        path = tmp_path / f"{index}.txt"
        path.write_text("x" * (index + 1), encoding="utf-8")
        paths.append(snapshot_path(path))

    summary = _route(
        tmp_path / "text.sqlite3",
        {"text/plain": tuple(paths)},
        max_file_bytes=2,
        max_documents=1,
    ).run()

    assert summary.candidate_pool == 3
    assert summary.candidates == 1
    assert summary.skipped_by_size == 1
    assert summary.skipped_by_count == 1


def test_email_text_enters_catalog_with_subject_driven_meaningful_name(
    tmp_path: Path,
) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    source = corpus / "Documento recuperado.eml"
    source.write_text(
        "From: Operacion <operacion@example.test>\n"
        "To: Victor <victor@example.test>\n"
        "Subject: Informe de mantenimiento del transformador norte\n"
        "MIME-Version: 1.0\n"
        "Content-Type: text/plain; charset=utf-8\n\n"
        "Se concluyó la inspección y prueba de protección diferencial.\n",
        encoding="utf-8",
    )
    state = tmp_path / "state"
    state.mkdir()
    _route(
        state / "text.sqlite3",
        {"message/rfc822": (snapshot_path(source),)},
    ).run()

    summaries = update_document_catalog(state)
    text_summary = next(item for item in summaries if item.source_kind == "text")

    assert text_summary.candidates == 1
    assert text_summary.classified == 1
    documents = list_catalog_documents(
        state / "document_catalog.sqlite3",
        limit=10,
    )
    assert len(documents) == 1
    assert documents[0].source_kind == "text"
    assert documents[0].path == str(source)
    with document_catalog_database(
        state / "document_catalog.sqlite3",
        readonly=True,
    ) as connection:
        classification = json.loads(
            connection.execute("SELECT classification_json FROM documents").fetchone()[0]
        )
    assert classification["suggested_stem"] == ("Informe de mantenimiento del transformador norte")


@pytest.mark.parametrize(
    ("config", "message"),
    (
        ({"max_file_bytes": 0}, "max_file_bytes"),
        ({"max_documents": 0}, "max_documents"),
        ({"max_text_chars": 0}, "max_text_chars"),
        ({"worker_timeout_seconds": 0}, "worker limits"),
        ({"worker_memory_bytes": 0}, "worker limits"),
    ),
)
def test_text_route_rejects_invalid_limits(
    tmp_path: Path,
    config: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _route(tmp_path / "text.sqlite3", {}, **config).run()
