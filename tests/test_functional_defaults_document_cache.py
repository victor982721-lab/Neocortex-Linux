"""Functional cache replay for the DOCX and Office default routes."""

from __future__ import annotations

import sqlite3
import zlib
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import neocortex.capabilities.formats.docx.route as docx_route_module
import neocortex.capabilities.formats.office.route as office_route_module
import neocortex.capabilities.formats.office.state as office_state_module
from neocortex.capabilities.formats.docx.route import (
    DOCX_MIME,
    PDF_MIME,
    DocxRoute,
    DocxRouteConfig,
    _decode_cached_text as decode_docx_cached_text,
    extract_docx,
    search_docx_state,
)
from neocortex.capabilities.formats.office.route import (
    OfficeRoute,
    OfficeRouteConfig,
    XLSX_MIME,
    search_office_state,
)
from neocortex.capabilities.formats.office.state import office_database
from neocortex.capabilities.formats.office.state import (
    _decode_cached_text as decode_office_cached_text,
)
from neocortex.runtime.control.cancellation import CancellationToken
from neocortex.runtime.orchestration.replay_metrics import route_replay_metrics
from neocortex.deduplication import snapshot_path

from tests.test_docx_route import _State, _make_docx
from tests.test_office_route import _route_for, _write_typed_xlsx


TEST_CAPABILITIES = ("base",)


class _FlushForbiddenDecoder:
    def __init__(self, delegate: Any) -> None:
        self._delegate = delegate
        self.flushed = False

    def decompress(self, payload: bytes, max_length: int) -> bytes:
        return self._delegate.decompress(payload, max_length)

    @property
    def eof(self) -> bool:
        return bool(self._delegate.eof)

    @property
    def unconsumed_tail(self) -> bytes:
        return self._delegate.unconsumed_tail

    @property
    def unused_data(self) -> bytes:
        return self._delegate.unused_data

    def flush(self) -> bytes:
        self.flushed = True
        raise AssertionError("cache decoder must not call unbounded flush")


@pytest.mark.parametrize(
    ("decoder", "module"),
    (
        (decode_docx_cached_text, docx_route_module),
        (decode_office_cached_text, office_state_module),
    ),
)
def test_cached_text_decoder_bounds_expansion_without_flush(
    decoder,
    module,
) -> None:
    original_decompressobj = module.zlib.decompressobj
    spies: list[_FlushForbiddenDecoder] = []

    def factory() -> _FlushForbiddenDecoder:
        spy = _FlushForbiddenDecoder(original_decompressobj())
        spies.append(spy)
        return spy

    oversized = zlib.compress(b"x" * 100_000)
    with patch.object(module.zlib, "decompressobj", side_effect=factory):
        assert decoder(oversized, 3, max_chars=3) is None
    assert len(spies) == 1
    assert not spies[0].flushed

    valid_text = "áé"
    valid = zlib.compress(valid_text.encode("utf-8"))
    with patch.object(module.zlib, "decompressobj", side_effect=factory):
        assert decoder(valid, len(valid_text), max_chars=8) == valid_text
    assert len(spies) == 2
    assert not spies[1].flushed


def test_docx_cache_repairs_fts_from_valid_durable_representation(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Informe.docx"
    _make_docx(source)
    state = _State({DOCX_MIME: [snapshot_path(source)], PDF_MIME: []})
    config = DocxRouteConfig(
        tmp_path / "docx.sqlite3",
        min_free_memory_bytes=0,
        min_free_commit_bytes=0,
    )

    first = DocxRoute(config, state, 1).run()
    assert (first.extracted, first.cache_hits, first.new_documents) == (1, 0, 1)
    with sqlite3.connect(config.state_path) as connection:
        connection.execute("DELETE FROM document_fts")

    with patch(
        "neocortex.capabilities.formats.docx.route.extract_docx",
        side_effect=AssertionError("a valid cache repair must not parse the DOCX"),
    ) as extractor:
        repaired = DocxRoute(config, state, 2).run()
    extractor.assert_not_called()

    assert (repaired.extracted, repaired.cache_hits, repaired.fts_documents_indexed) == (0, 1, 1)
    assert search_docx_state(config.state_path, "transformador")
    intact = DocxRoute(config, state, 3).run()
    assert route_replay_metrics("docx", intact)["new_work"] == 0
    assert intact.fts_documents_indexed == 0


def test_docx_invalid_structural_derivative_reprocesses_instead_of_serving_stale(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Estructura.docx"
    _make_docx(source)
    state = _State({DOCX_MIME: [snapshot_path(source)], PDF_MIME: []})
    config = DocxRouteConfig(
        tmp_path / "docx.sqlite3",
        min_free_memory_bytes=0,
        min_free_commit_bytes=0,
    )
    DocxRoute(config, state, 1).run()
    with sqlite3.connect(config.state_path) as connection:
        connection.execute("DELETE FROM document_parts")

    with patch(
        "neocortex.capabilities.formats.docx.route.extract_docx",
        wraps=extract_docx,
    ) as extractor:
        result = DocxRoute(config, state, 2).run()

    assert extractor.call_count == 1
    assert (result.cache_hits, result.extracted) == (0, 1)
    assert search_docx_state(config.state_path, "transformador")


def test_office_cache_repairs_fts_from_valid_durable_representation(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Mediciones.xlsx"
    _write_typed_xlsx(source)
    office_path = tmp_path / "office.sqlite3"
    route, framework = _route_for(office_path, {XLSX_MIME: source})

    first = route.run()
    with office_database(office_path) as connection:
        connection.execute("DELETE FROM document_fts")
        connection.commit()

    with patch(
        "neocortex.capabilities.formats.office.route.extract_office_document",
        side_effect=AssertionError("a valid cache repair must not parse the Office file"),
    ) as extractor:
        repaired = OfficeRoute(
            route.config,
            framework,
            2,
            cancellation=CancellationToken(),
        ).run()
    extractor.assert_not_called()

    assert (first.extracted, repaired.extracted, repaired.cache_hits) == (1, 0, 1)
    assert search_office_state(office_path, "Interruptor")
    intact = OfficeRoute(
        route.config,
        framework,
        3,
        cancellation=CancellationToken(),
    ).run()
    assert route_replay_metrics("office", intact)["new_work"] == 0


def test_office_missing_typed_cells_invalidates_cache_and_reextracts(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Tipos.xlsx"
    _write_typed_xlsx(source)
    office_path = tmp_path / "office.sqlite3"
    route, framework = _route_for(office_path, {XLSX_MIME: source})
    route.run()
    with office_database(office_path) as connection:
        connection.execute("DELETE FROM xlsx_cells")
        connection.commit()

    with patch(
        "neocortex.capabilities.formats.office.route.extract_office_document",
        wraps=office_route_module.extract_office_document,
    ) as extractor:
        result = OfficeRoute(
            route.config,
            framework,
            2,
            cancellation=CancellationToken(),
        ).run()

    assert extractor.call_count == 1
    assert (result.cache_hits, result.extracted) == (0, 1)
    with office_database(office_path, readonly=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM xlsx_cells").fetchone()[0] == 10
    assert search_office_state(office_path, "Interruptor")


def test_docx_recoverable_error_retry_is_opt_in_and_does_not_repeat_success(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Temporal.docx"
    _make_docx(source)
    state = _State({DOCX_MIME: [snapshot_path(source)], PDF_MIME: []})
    database = tmp_path / "docx.sqlite3"

    with patch(
        "neocortex.capabilities.formats.docx.route.extract_docx",
        side_effect=OSError("transient source unavailable"),
    ):
        failed = DocxRoute(DocxRouteConfig(database), state, 1).run()
    assert failed.retryable_errors == 1

    with patch(
        "neocortex.capabilities.formats.docx.route.extract_docx",
        side_effect=AssertionError("default must retain a retryable error"),
    ) as default_extractor:
        retained = DocxRoute(DocxRouteConfig(database), state, 2).run()
    default_extractor.assert_not_called()
    assert (retained.cache_hits, retained.cached_errors, retained.extracted) == (0, 1, 0)

    retry_config = DocxRouteConfig(database, retry_recoverable_errors=True)
    with patch(
        "neocortex.capabilities.formats.docx.route.extract_docx",
        wraps=extract_docx,
    ) as retry_extractor:
        recovered = DocxRoute(retry_config, state, 3).run()
    assert retry_extractor.call_count == 1
    assert (recovered.retried_documents, recovered.extracted) == (1, 1)

    with patch(
        "neocortex.capabilities.formats.docx.route.extract_docx",
        side_effect=AssertionError("a valid success must never be re-extracted"),
    ) as intact_extractor:
        intact = DocxRoute(retry_config, state, 4).run()
    intact_extractor.assert_not_called()
    assert route_replay_metrics("docx", intact)["new_work"] == 0


def test_office_recoverable_retry_accepts_typed_retry_only(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Temporal.xlsx"
    _write_typed_xlsx(source)
    office_path = tmp_path / "office.sqlite3"
    route, framework = _route_for(office_path, {XLSX_MIME: source})

    with patch(
        "neocortex.capabilities.formats.office.route.extract_office_document",
        side_effect=OSError("transient source unavailable"),
    ):
        failed = route.run()
    assert failed.errors == 1

    with office_database(office_path) as connection:
        connection.execute(
            "UPDATE documents SET error_message=?,retryable=0,review_disposition=?",
            ("retry this only because a message says so", "manual_review"),
        )
        connection.commit()

    retry_config = OfficeRouteConfig(
        state_path=office_path,
        retry_recoverable_errors=True,
        min_free_memory_bytes=0,
        min_free_commit_bytes=0,
    )
    with patch(
        "neocortex.capabilities.formats.office.route.extract_office_document",
        side_effect=AssertionError("manual error text must not trigger retry"),
    ) as extractor:
        retained = OfficeRoute(
            retry_config,
            framework,
            2,
            cancellation=CancellationToken(),
        ).run()
    extractor.assert_not_called()
    assert (retained.cache_hits, retained.cached_errors, retained.extracted) == (1, 1, 0)

    with office_database(office_path) as connection:
        connection.execute(
            "UPDATE documents SET retryable=1,review_disposition='retry'"
        )
        connection.commit()
    with patch(
        "neocortex.capabilities.formats.office.route.extract_office_document",
        wraps=office_route_module.extract_office_document,
    ) as retry_extractor:
        recovered = OfficeRoute(
            retry_config,
            framework,
            3,
            cancellation=CancellationToken(),
        ).run()
    assert retry_extractor.call_count == 1
    assert (recovered.cache_hits, recovered.extracted) == (0, 1)
