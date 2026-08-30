from __future__ import annotations

import io
import json
import os
import stat
import time
import zipfile
from pathlib import Path

import pytest

from neocortex.deduplication import FileSnapshot, snapshot_path
from neocortex.capabilities.formats.archive.route import (
    ARCHIVE_MIME,
    ArchiveRoute,
    ArchiveRouteConfig,
)
from neocortex.capabilities.formats.archive.state import (
    archive_database,
    list_archive_members,
    read_archive_status,
    search_archive_state,
)
from neocortex.runtime.control.cancellation import CancellationToken
from neocortex.safety.route_filters import CandidateSelection


class FakeFrameworkRouteState:
    def __init__(self, candidates: tuple[FileSnapshot, ...]):
        self.candidates = candidates

    def selected_route_candidate_counts(
        self,
        _run_id: int,
        mime: str,
        max_file_bytes: int | None,
        route_name: str,
        _selection: CandidateSelection,
    ) -> tuple[int, int]:
        assert mime == ARCHIVE_MIME
        assert route_name == "archive"
        eligible = tuple(
            snapshot
            for snapshot in self.candidates
            if max_file_bytes is None or snapshot.size <= max_file_bytes
        )
        return len(self.candidates), len(eligible)

    def iter_selected_route_candidates(
        self,
        _run_id: int,
        mime: str,
        route_name: str,
        _selection: CandidateSelection,
    ):
        assert mime == ARCHIVE_MIME
        assert route_name == "archive"
        yield from self.candidates


def _zip_bytes(entries: dict[str, bytes | str]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return output.getvalue()


def _docx_bytes() -> bytes:
    return _zip_bytes(
        {
            "[Content_Types].xml": "<Types/>",
            "word/document.xml": (
                '<w:document xmlns:w="urn:w"><w:p><w:t>Informe de '
                "transformador encapsulado</w:t></w:p></w:document>"
            ),
        }
    )


def _ocr_png_bytes(text: str) -> bytes:
    pillow = pytest.importorskip("PIL.Image")
    image_draw = pytest.importorskip("PIL.ImageDraw")
    image_font = pytest.importorskip("PIL.ImageFont")
    font_path = Path("/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf")
    if not font_path.is_file():
        pytest.skip("representative OCR font is unavailable")
    image = pillow.new("RGB", (1800, 260), "white")
    draw = image_draw.Draw(image)
    font = image_font.truetype(os.fspath(font_path), 72)
    draw.text((45, 75), text, fill="black", font=font)
    output = io.BytesIO()
    image.save(output, format="PNG")
    image.close()
    return output.getvalue()


def _representative_archive(path: Path) -> int:
    deepest = _zip_bytes({"documento-profundo.txt": "evidencia dentro del tercer ZIP"})
    inner = _zip_bytes(
        {
            "subcarpeta/tercero.zip": deepest,
            "notas-internas.md": "calibración de relevador dentro del ZIP anidado",
        }
    )
    entries: dict[str, bytes | str] = {
        f"documentos/ficha-{index:02d}.txt": (
            f"Ficha técnica {index:02d}: interruptor transformador protección"
        )
        for index in range(1, 21)
    }
    entries.update(
        {
            "datos/configuracion.json": json.dumps(
                {"equipo": "subestación", "estado": "vigente"},
                ensure_ascii=False,
            ),
            "web/resumen.html": (
                "<html><body><h1>Resumen visible</h1><script>secreto()</script>"
                "<p>maniobra eléctrica</p></body></html>"
            ),
            "codigo/diagnostico.py": "def revisar_interruptor():\n    return 'correcto'\n",
            "oficina/informe.docx": _docx_bytes(),
            "subcarpeta/otro.zip": inner,
            "imagen.bin": b"\x00\x01\x02\xff",
        }
    )
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return len(entries) + 3  # descendants inside the two nested ZIP payloads


def _route(
    state: Path,
    source: Path,
    *,
    run_id: int = 1,
    **limits,
) -> ArchiveRoute:
    framework = FakeFrameworkRouteState((snapshot_path(source),))
    return ArchiveRoute(
        ArchiveRouteConfig(state_path=state, **limits),
        framework,  # type: ignore[arg-type]
        run_id,
        cancellation=CancellationToken(),
    )


def test_indexes_representative_members_nested_zip_and_replays_cache(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Información técnica.zip"
    expected_members = _representative_archive(source)
    state = tmp_path / "archive.sqlite3"
    route = _route(state, source)

    first = route.run()
    second = route.run()

    assert first.errors == 0
    assert first.containers_complete == 1
    assert first.members_seen == expected_members
    assert first.nested_archives == 2
    assert first.members_indexed >= 25
    assert first.metadata_only >= 3
    assert second.cache_hits == 1
    assert second.members_seen == first.members_seen

    deep = search_archive_state(state, "evidencia tercer ZIP")
    assert len(deep) == 1
    assert deep[0].archive_depth == 3
    assert deep[0].member_chain == (
        "subcarpeta/otro.zip!/subcarpeta/tercero.zip!/documento-profundo.txt"
    )
    assert deep[0].virtual_path == (
        f"{source}!/subcarpeta/otro.zip!/subcarpeta/tercero.zip!/documento-profundo.txt"
    )
    assert search_archive_state(state, "transformador encapsulado")[0].content_kind == "docx"
    assert "secreto" not in (search_archive_state(state, "maniobra")[0].snippet or "")

    status = read_archive_status(state)
    assert status.available
    assert status.containers == 1
    assert status.nested_archives == 2
    assert len(list_archive_members(state, 1, container_fragment="Información")) == 1


def test_unsafe_names_special_members_and_duplicates_are_skipped_but_visible_as_issues(
    tmp_path: Path,
) -> None:
    source = tmp_path / "adversarial.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("seguro.txt", "contenido seguro consultable")
        archive.writestr("../escape.txt", "no debe indexarse")
        archive.writestr("/absoluto.txt", "no debe indexarse")
        archive.writestr("directorio!/ambiguo.txt", "no debe indexarse")
        archive.writestr("Duplicado.txt", "primero")
        archive.writestr("duplicado.TXT", "segundo")
        with pytest.warns(UserWarning, match="Duplicate name"):
            archive.writestr("Duplicado.txt", "tercero")
        symlink = zipfile.ZipInfo("enlace")
        symlink.create_system = 3
        symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(symlink, "../../objetivo")
    state = tmp_path / "archive.sqlite3"

    summary = _route(state, source).run()

    assert summary.errors == 0
    assert summary.containers_partial == 1
    assert summary.safety_issues == 5
    with archive_database(state, readonly=True) as connection:
        members = {str(row[0]) for row in connection.execute("SELECT member_chain FROM documents")}
        reasons = {
            str(row[0]) for row in connection.execute("SELECT reason_code FROM archive_issues")
        }
    assert members == {"seguro.txt", "Duplicado.txt", "duplicado.TXT", "enlace"}
    assert reasons == {
        "archive_duplicate_member",
        "archive_special_member",
        "archive_unsafe_member_name",
    }


def test_nested_zip_with_executable_preamble_is_discovered_by_member_extension(
    tmp_path: Path,
) -> None:
    nested = b"MZsynthetic-stub" + _zip_bytes(
        {"interior.txt": "texto consultable dentro de ZIP con prefijo"}
    )
    source = tmp_path / "preamble.zip"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("herramienta.zip", nested)
    state = tmp_path / "archive.sqlite3"

    summary = _route(state, source).run()

    assert summary.errors == 0
    assert summary.nested_archives == 1
    hit = search_archive_state(state, "consultable prefijo")[0]
    assert hit.member_chain == "herramienta.zip!/interior.txt"
    assert hit.archive_depth == 2


@pytest.mark.skipif(os.name == "nt", reason="requires a case-sensitive filesystem")
def test_linux_case_distinct_containers_and_members_remain_independently_searchable(
    tmp_path: Path,
) -> None:
    upper = tmp_path / "Datos.zip"
    lower = tmp_path / "datos.zip"
    upper.write_bytes(
        _zip_bytes(
            {
                "Informe.txt": "evidencia mayúscula uno",
                "informe.txt": "evidencia minúscula dos",
            }
        )
    )
    lower.write_bytes(_zip_bytes({"tercero.txt": "evidencia contenedor minúsculo"}))
    state = tmp_path / "archive.sqlite3"
    framework = FakeFrameworkRouteState((snapshot_path(upper), snapshot_path(lower)))

    summary = ArchiveRoute(
        ArchiveRouteConfig(state_path=state),
        framework,  # type: ignore[arg-type]
        1,
        cancellation=CancellationToken(),
    ).run()

    assert summary.containers_complete == 2
    assert read_archive_status(state).containers == 2
    hits = search_archive_state(state, "evidencia")
    assert {hit.member_chain for hit in hits} == {
        "Informe.txt",
        "informe.txt",
        "tercero.txt",
    }


def test_depth_ratio_and_total_budgets_fail_closed_without_writing_files(
    tmp_path: Path,
) -> None:
    level_three = _zip_bytes({"oculto.txt": "no debe alcanzarse"})
    level_two = _zip_bytes({"nivel-tres.zip": level_three})
    source = tmp_path / "limits.zip"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("nivel-dos.zip", level_two)
        archive.writestr("altamente-comprimible.txt", "A" * 100_000)
    before = set(tmp_path.iterdir())

    summary = _route(
        tmp_path / "archive.sqlite3",
        source,
        max_depth=2,
        max_compression_ratio=10.0,
        max_total_uncompressed_bytes=2 * 1024 * 1024,
    ).run()

    assert summary.containers_partial == 1
    assert summary.safety_issues >= 2
    after = set(tmp_path.iterdir())
    assert after - before <= {
        tmp_path / "archive.sqlite3",
        tmp_path / "archive.sqlite3-shm",
        tmp_path / "archive.sqlite3-wal",
    }
    assert not search_archive_state(tmp_path / "archive.sqlite3", "oculto")


def test_corrupt_top_level_zip_is_cached_as_typed_error(tmp_path: Path) -> None:
    source = tmp_path / "corrupt.zip"
    source.write_bytes(b"PK\x03\x04not-a-complete-zip")
    state = tmp_path / "archive.sqlite3"
    route = _route(state, source)

    first = route.run()
    second = route.run()

    assert first.errors == 1
    assert second.cache_hits == 1
    assert second.cached_errors == 1
    with archive_database(state, readonly=True) as connection:
        row = connection.execute("SELECT status,error_type FROM containers").fetchone()
    assert tuple(row) == ("error", "archive_corrupt_container")


def test_source_removed_after_inventory_is_retryable_not_mislabeled_corrupt(
    tmp_path: Path,
) -> None:
    source = tmp_path / "removed.zip"
    source.write_bytes(_zip_bytes({"documento.txt": "contenido"}))
    state = tmp_path / "archive.sqlite3"
    route = _route(state, source)
    source.unlink()

    summary = route.run()

    assert summary.errors == 1
    with archive_database(state, readonly=True) as connection:
        row = connection.execute("SELECT status,error_type,retryable FROM containers").fetchone()
    assert tuple(row) == ("error", "archive_source_changed", 1)


def test_cache_refreshes_virtual_paths_after_container_move(tmp_path: Path) -> None:
    source = tmp_path / "original.zip"
    source.write_bytes(_zip_bytes({"documento.txt": "identidad durable"}))
    state = tmp_path / "archive.sqlite3"
    first = _route(state, source, run_id=1).run()
    moved = tmp_path / "movido.zip"
    source.rename(moved)

    second = _route(state, moved, run_id=2).run()

    assert first.cache_hits == 0
    assert second.cache_hits == 1
    hit = search_archive_state(state, "identidad durable")[0]
    assert hit.container_path == str(moved)
    assert hit.virtual_path == f"{moved}!/documento.txt"


def test_changed_container_replaces_old_members_and_full_run_prunes_stale(
    tmp_path: Path,
) -> None:
    first_source = tmp_path / "primero.zip"
    second_source = tmp_path / "segundo.zip"
    first_source.write_bytes(_zip_bytes({"anterior.txt": "contenido anterior único"}))
    second_source.write_bytes(_zip_bytes({"vigente.txt": "contenido vigente único"}))
    state = tmp_path / "archive.sqlite3"
    framework = FakeFrameworkRouteState((snapshot_path(first_source), snapshot_path(second_source)))
    ArchiveRoute(
        ArchiveRouteConfig(state_path=state),
        framework,  # type: ignore[arg-type]
        1,
        cancellation=CancellationToken(),
    ).run()

    time.sleep(0.002)
    second_source.write_bytes(_zip_bytes({"nuevo.txt": "reemplazo consultable"}))
    updated_framework = FakeFrameworkRouteState((snapshot_path(second_source),))
    summary = ArchiveRoute(
        ArchiveRouteConfig(state_path=state),
        updated_framework,  # type: ignore[arg-type]
        2,
        cancellation=CancellationToken(),
    ).run()

    assert summary.cache_hits == 0
    assert summary.cache_containers_pruned == 1
    assert summary.cache_members_pruned == 1
    assert not search_archive_state(state, "anterior único")
    assert not search_archive_state(state, "vigente único")
    assert search_archive_state(state, "reemplazo consultable")[0].member_chain == "nuevo.txt"


def test_pdf_inside_zip_indexes_native_text_in_isolated_worker(tmp_path: Path) -> None:
    fitz = pytest.importorskip("fitz")
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "proteccion diferencial archivo PDF interno")
    payload = document.tobytes()
    document.close()
    source = tmp_path / "pdf-interno.zip"
    source.write_bytes(_zip_bytes({"manuales/proteccion.pdf": payload}))
    state = tmp_path / "archive.sqlite3"

    summary = _route(state, source).run()

    assert summary.errors == 0
    hit = search_archive_state(state, "diferencial interno")[0]
    assert hit.content_kind == "pdf"
    assert hit.member_chain == "manuales/proteccion.pdf"


def test_nested_zip_indexes_image_ocr_with_explicit_virtual_path(tmp_path: Path) -> None:
    pytest.importorskip("pytesseract")
    if not Path("/usr/bin/tesseract").is_file():
        pytest.skip("Tesseract is unavailable")
    inner = _zip_bytes({"imagenes/tablero.png": _ocr_png_bytes("RELEVADOR ARCO ELECTRICO NORTE")})
    source = tmp_path / "evidencia.zip"
    source.write_bytes(_zip_bytes({"anidado/inspeccion.zip": inner}))
    state = tmp_path / "archive.sqlite3"

    summary = _route(state, source).run()

    assert summary.errors == 0
    assert summary.nested_archives == 1
    hit = search_archive_state(state, "relevador electrico", 10)[0]
    assert hit.content_kind == "image"
    assert hit.archive_depth == 2
    assert hit.virtual_path.endswith("evidencia.zip!/anidado/inspeccion.zip!/imagenes/tablero.png")
    assert hit.container_path == os.fspath(source)


def test_zip_indexes_scanned_pdf_through_bounded_ocr(tmp_path: Path) -> None:
    fitz = pytest.importorskip("fitz")
    pytest.importorskip("pytesseract")
    if not Path("/usr/bin/tesseract").is_file():
        pytest.skip("Tesseract is unavailable")
    image = _ocr_png_bytes("TRANSFORMADOR POTENCIA DELTA")
    document = fitz.open()
    page = document.new_page(width=1800, height=260)
    page.insert_image(page.rect, stream=image)
    payload = document.tobytes()
    document.close()
    source = tmp_path / "escaneos.zip"
    source.write_bytes(_zip_bytes({"reportes/placa.pdf": payload}))
    state = tmp_path / "archive.sqlite3"

    summary = _route(state, source).run()

    assert summary.errors == 0
    hit = search_archive_state(state, "transformador potencia", 10)[0]
    assert hit.content_kind == "pdf"
    assert hit.virtual_path.endswith("escaneos.zip!/reportes/placa.pdf")
    assert hit.archive_depth == 1
