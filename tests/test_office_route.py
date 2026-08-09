from __future__ import annotations

import os
import sqlite3
import zipfile
import zlib
from pathlib import Path
from typing import Any

import pytest

import _04_Nucleo_Operativo.office_route as office_route_module
from _02_Deduplicacion import FileSnapshot, snapshot_path
from _04_Nucleo_Operativo.cancellation import CancellationToken
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
from _04_Nucleo_Operativo.office_route import (
    OFFICE_MIME_FORMATS,
    ODT_MIME,
    PPTX_MIME,
    XLSX_MIME,
    OfficeRoute,
    OfficeRouteConfig,
)
from _04_Nucleo_Operativo.office_state import office_database
from _04_Nucleo_Operativo.route_filters import CandidateSelection
from tests.internal_paths_test_support import disjoint_internal_paths_policy


# region [01] Minimal safe office fixtures and framework-state double


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


def _core(title: str, author: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<cp:coreProperties
 xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
 xmlns:dc="http://purl.org/dc/elements/1.1/">
 <dc:title>{title}</dc:title><dc:creator>{author}</dc:creator>
</cp:coreProperties>"""


def _write_xlsx(path: Path) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("docProps/core.xml", _core("Orden de compra CHINT", "CHINT Electric"))
        archive.writestr(
            "xl/workbook.xml",
            """<workbook xmlns="urn:test"><sheets>
            <sheet name="Materiales transformador"/></sheets></workbook>""",
        )
        archive.writestr(
            "xl/sharedStrings.xml",
            """<sst xmlns="urn:test"><si><t>Requisición de materiales</t></si>
            <si><t>Interruptor de potencia y transformador</t></si></sst>""",
        )


def _write_pptx(path: Path) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr(
            "docProps/core.xml",
            _core("Curso CFE de transformadores", "Comisión Federal de Electricidad"),
        )
        archive.writestr("ppt/presentation.xml", "<presentation/>")
        archive.writestr(
            "ppt/slides/slide1.xml",
            """<p:sld xmlns:p="urn:p" xmlns:a="urn:a"><a:t>Material didáctico
            de capacitación en pruebas eléctricas</a:t></p:sld>""",
        )


def _write_odt(path: Path) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "mimetype",
            "application/vnd.oasis.opendocument.text",
            compress_type=zipfile.ZIP_STORED,
        )
        archive.writestr(
            "content.xml",
            """<office:document xmlns:office="urn:office" xmlns:text="urn:text">
            <text:h>Procedimiento SERINTRA</text:h>
            <text:p>Instructivo de mantenimiento de subestaciones</text:p>
            </office:document>""",
        )


def _route_for(state_path: Path, source_paths: dict[str, Path]):
    candidates: dict[str, tuple[FileSnapshot, ...]] = {
        mime: (snapshot_path(source_paths[mime]),) for mime in source_paths
    }
    framework = FakeFrameworkRouteState(candidates)
    route = OfficeRoute(
        OfficeRouteConfig(
            state_path=state_path,
            min_free_memory_bytes=0,
            min_free_commit_bytes=0,
        ),
        framework,  # type: ignore[arg-type]
        1,
        cancellation=CancellationToken(),
    )
    return route, framework


# endregion [01]


# region [02] Incremental extraction, classification and corrupt review


def test_office_route_extracts_caches_and_classifies_all_supported_formats(
    tmp_path: Path,
) -> None:
    xlsx = tmp_path / "Compra CHINT.xlsx"
    pptx = tmp_path / "Curso CFE.pptx"
    odt = tmp_path / "Procedimiento SERINTRA.odt"
    _write_xlsx(xlsx)
    _write_pptx(pptx)
    _write_odt(odt)
    office_path = tmp_path / "office.sqlite3"
    route, framework = _route_for(
        office_path,
        {XLSX_MIME: xlsx, PPTX_MIME: pptx, ODT_MIME: odt},
    )

    first = route.run()
    second = route.run()

    assert first.extracted == 3
    assert first.errors == 0
    assert second.cache_hits == 3
    assert len(framework.resolutions) == 6
    assert all("office_corrupt_container" in item[4] for item in framework.resolutions)
    assert all(item[5] == frozenset() for item in framework.resolutions)
    with office_database(office_path, readonly=True) as connection:
        rows = connection.execute(
            "SELECT format,status,text_chars FROM documents ORDER BY format"
        ).fetchall()
    assert [(row["format"], row["status"]) for row in rows] == [
        ("odt", "complete"),
        ("pptx", "complete"),
        ("xlsx", "complete"),
    ]
    assert all(int(row["text_chars"]) > 0 for row in rows)

    summaries = update_document_catalog(tmp_path)

    assert {summary.source_kind for summary in summaries} == {
        "pdf",
        "docx",
        "xlsx",
        "pptx",
        "odt",
        "audio",
        "text",
    }
    with document_catalog_database(tmp_path / "document_catalog.sqlite3", readonly=True) as catalog:
        classified = catalog.execute(
            """SELECT source_kind,primary_kind,primary_organization
            FROM documents ORDER BY source_kind"""
        ).fetchall()
    assert [(row["source_kind"], row["primary_kind"]) for row in classified] == [
        ("odt", "procedimiento"),
        ("pptx", "curso_capacitacion"),
        ("xlsx", "compra_requisicion"),
    ]
    assert {row["primary_organization"] for row in classified} == {
        "SERINTRA",
        "CFE",
        "CHINT",
    }


def test_corrupt_office_container_is_cached_as_deletion_candidate(
    tmp_path: Path,
) -> None:
    source = tmp_path / "dañado.xlsx"
    source.write_bytes(b"not a zip container")
    route, framework = _route_for(
        tmp_path / "office.sqlite3",
        {XLSX_MIME: source},
    )

    summary = route.run()
    cached = route.run()

    assert summary.errors == 1
    assert summary.deletion_candidates == 1
    assert cached.processed == 1
    assert cached.cache_hits == 1
    assert cached.cached_errors == 1
    assert cached.errors == 0
    assert len(framework.reviews) == 2
    assert framework.reviews[0].recommendation == "deletion_candidate"
    connection = sqlite3.connect(tmp_path / "office.sqlite3")
    try:
        row = connection.execute(
            "SELECT status,error_type,review_disposition FROM documents"
        ).fetchone()
    finally:
        connection.close()
    assert row == (
        "error",
        "office_corrupt_container",
        "deletion_candidate",
    )


def test_corrupt_deflate_stream_isolated_to_one_office_candidate(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "flujo-deflate-corrupto.xlsx"
    _write_xlsx(source)
    route, framework = _route_for(
        tmp_path / "office.sqlite3",
        {XLSX_MIME: source},
    )

    def raise_zlib_error(*_args, **_kwargs):
        raise zlib.error("invalid distance too far back")

    monkeypatch.setattr(
        office_route_module,
        "_extract_part_text",
        raise_zlib_error,
    )

    summary = route.run()

    assert summary.processed == 1
    assert summary.errors == 1
    assert summary.deletion_candidates == 1
    assert framework.reviews[0].reason_code == "office_corrupt_container"
    assert framework.reviews[0].recommendation == "deletion_candidate"


# endregion [02]


# region [03] Organization move and office-cache synchronization


@pytest.mark.skipif(os.name != "nt", reason="document mutation backend is Windows-only")
def test_organized_xlsx_move_updates_office_cache_and_fts(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    state = tmp_path / "state"
    corpus.mkdir()
    state.mkdir()
    source = corpus / "Compra CHINT.xlsx"
    _write_xlsx(source)
    office_path = state / "office.sqlite3"
    route, _framework = _route_for(office_path, {XLSX_MIME: source})
    assert route.run().extracted == 1
    update_document_catalog(state)
    catalog_path = state / "document_catalog.sqlite3"
    destination_root = tmp_path / "organizados"
    assert plan_document_organization(catalog_path, destination_root).planned == 1

    summary = apply_document_organization(
        catalog_path,
        destination_root,
        mutation_guard=CorpusMutationGuard(
            CorpusAccessPolicy.capture("normal", corpus),
            disjoint_internal_paths_policy(tmp_path),
        ),
        max_actions=1,
    )
    destination = (
        destination_root
        / "Empresas"
        / "CHINT"
        / "Gestion_y_administracion"
        / "Comercial_y_contratos"
        / source.name
    )

    assert summary.applied == 1
    assert summary.cache_synced == 1
    assert destination.is_file()
    with office_database(office_path, readonly=True) as connection:
        assert connection.execute("SELECT path FROM documents").fetchone()[0] == str(destination)
        assert connection.execute("SELECT path FROM office_inventory").fetchone()[0] == str(
            destination
        )
        assert connection.execute("SELECT path FROM document_fts").fetchone()[0] == str(destination)


def test_office_mime_contract_covers_indexed_formats() -> None:
    assert set(OFFICE_MIME_FORMATS) == {XLSX_MIME, PPTX_MIME, ODT_MIME}


def test_office_cli_configuration_is_explicit_and_validated() -> None:
    args = build_parser().parse_args(
        [
            "--route",
            "office",
            "--office-max-count",
            "25",
            "--office-max-text-chars",
            "500000",
            "--office-memory-budget-mb",
            "384",
            "--office-min-free-memory-mb",
            "0",
            "--office-min-free-commit-mb",
            "0",
        ]
    )
    validate_arguments(args)

    config = framework_config_from_args(args)

    assert config.office_max_documents == 25
    assert config.office_max_text_chars == 500_000
    assert config.office_memory_budget_bytes == 384 * 1024 * 1024


# endregion [03]
