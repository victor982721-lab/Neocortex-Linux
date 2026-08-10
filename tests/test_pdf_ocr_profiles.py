from __future__ import annotations

import json
import sqlite3
import tempfile
from contextlib import closing, nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from _02_Deduplicacion import snapshot_path
from _04_Nucleo_Operativo import pdf_schema
from _04_Nucleo_Operativo.pdf_admin import doctor_pdf_runtime
from _04_Nucleo_Operativo.pdf_isolation import (
    IsolatedExtractionConfig,
    PdfOcrPageResult,
    _ChildExtractionSession,
    _ocr_page_result,
)
from _04_Nucleo_Operativo.pdf_route import PdfRoute
from _04_Nucleo_Operativo.pdf_route_models import PdfRouteConfig
from _04_Nucleo_Operativo.pdf_route_cache import file_key
from _04_Nucleo_Operativo.pdf_route_storage import PdfRouteStorageMixin
from _04_Nucleo_Operativo.pdf_state import (
    SCHEMA_VERSION,
    initialize_pdf_state,
    pdf_database,
)
from _04_Nucleo_Operativo.processing_provenance import (
    TesseractRuntimeProvenance,
)


def _config(**overrides) -> IsolatedExtractionConfig:
    values = {
        "ocr_mode": "auto",
        "ocr_lang": "spa+eng",
        "dpi": 200,
        "min_page_chars": 40,
        "max_page_text_chars": 10_000,
        "max_render_pixels": 40_000_000,
        "max_ocr_pages": None,
        "ocr_timeout_seconds": 30,
        "pdfminer_fallback": True,
        "max_pages": None,
        "page_start": None,
        "page_end": None,
        "fail_fast_pages": False,
        "skip_before": 0,
        "only_pages": frozenset(),
        "prior_ocr_pages": 0,
        "tesseract_cmd": None,
        "tessdata_dir": None,
        "ocr_profile": "configured",
        "ocr_processing_signature": "test-ocr-signature",
        "ocr_traineddata_hashes": (("spa", "spa-hash"), ("eng", "eng-hash")),
    }
    values.update(overrides)
    return IsolatedExtractionConfig(**values)


class _Page:
    def __init__(self, text: str) -> None:
        self.text = text

    def get_text(self, _kind: str) -> str:
        return self.text


def test_native_pdf_quality_gate_ocr_replaces_long_mojibake() -> None:
    config = _config()
    session = _ChildExtractionSession(None, config, object(), object())
    session.fitz = object()
    result = PdfOcrPageResult(
        "Informe técnico recuperado",
        {"schema": "neocortex.ocr-page/v1", "effective_languages": ["spa", "eng"]},
    )
    with patch(
        "_04_Nucleo_Operativo.pdf_isolation._ocr_page_result",
        return_value=result,
    ) as ocr:
        source, text, provenance = session._page_text(_Page("Ã" * 100))

    assert ocr.call_count == 1
    assert source == "ocr"
    assert text == "Informe técnico recuperado"
    assert provenance["ocr_attempted"] is True
    assert provenance["ocr_selected"] is True
    quality = provenance["native_text_quality"]
    assert isinstance(quality, dict)
    assert quality["usable"] is False
    assert quality["reason"] == "suspicious_unicode_or_mojibake"


@pytest.mark.parametrize(
    "native_text",
    (
        "Prüfbericht für den Transformator ohne festgestellte Abweichungen",
        "变压器绝缘油检测报告已经完成审核",
        "變壓器絕緣油檢測報告已經完成審核",
    ),
)
def test_clean_german_and_han_native_text_skip_ocr(native_text: str) -> None:
    session = _ChildExtractionSession(None, _config(), object(), object())
    with patch(
        "_04_Nucleo_Operativo.pdf_isolation._ocr_page_result"
    ) as ocr:
        source, text, provenance = session._page_text(_Page(native_text))

    ocr.assert_not_called()
    assert source == "native"
    assert text == native_text
    assert provenance["ocr_attempted"] is False
    assert provenance["ocr_skipped_reason"] == "native_text_usable"


def test_pdf_auto_han_uses_300dpi_and_at_most_one_traditional_fallback() -> None:
    scales: list[float] = []

    class FakeFitz:
        csGRAY = object()

        @staticmethod
        def Matrix(x: float, _y: float):
            scales.append(x)
            return SimpleNamespace(scale=x)

    class FakePage:
        rect = SimpleNamespace(width=612.0, height=792.0)

        @staticmethod
        def get_pixmap(**_kwargs):
            return SimpleNamespace(
                width=120,
                height=160,
                samples=b"\xff" * (120 * 160),
            )

    config = _config(
        ocr_profile="auto-multilingual",
        ocr_traineddata_hashes=(
            ("spa", "spa-hash"),
            ("eng", "eng-hash"),
            ("deu", "deu-hash"),
            ("chi_sim", "sim-hash"),
            ("chi_tra", "tra-hash"),
            ("osd", "osd-hash"),
        ),
    )
    primary = {"text": ["變壓器", "檢查"], "conf": ["88", "88"]}
    fallback = {"text": ["變壓器", "檢查"], "conf": ["96", "96"]}
    osd = (
        "Orientation in degrees: 0\nRotate: 0\n"
        "Orientation confidence: 10.5\nScript: Han\nScript confidence: 7.25\n"
    )
    with (
        patch("pytesseract.image_to_osd", return_value=osd) as osd_call,
        patch(
            "pytesseract.image_to_data",
            side_effect=(primary, fallback),
        ) as recognition,
    ):
        result = _ocr_page_result(FakePage(), FakeFitz, config, nullcontext())

    assert osd_call.call_count == 1
    assert recognition.call_count == 2
    assert scales[0] == pytest.approx(300 / 72)
    assert result.text == "變壓器 檢查"
    assert result.provenance["effective_languages"] == ["chi_tra", "eng"]
    assert result.provenance["fallback_attempted"] is True
    assert result.provenance["fallback_reason"] == "traditional_han_signal"
    assert result.provenance["recognition_attempts"] == 2
    assert result.provenance["render_dpi"] == pytest.approx(300.0)
    assert result.provenance["traineddata"][-2:] == [
        {"language": "chi_tra", "xxh3_128": "tra-hash"},
        {"language": "osd", "xxh3_128": "osd-hash"},
    ]


class _Storage(PdfRouteStorageMixin):
    def __init__(self, config: PdfRouteConfig) -> None:
        self.config = config
        self.run_id = 1

    def _check_disk(self) -> None:
        return None

    def _delete_document_cache(
        self,
        _connection: sqlite3.Connection,
        _cache_key: str,
    ) -> int:
        return 0


def test_schema_12_promotes_bounded_page_ocr_provenance() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        state = root / "pdf.sqlite3"
        source = root / "sample.pdf"
        source.write_bytes(b"%PDF-1.7\nfixture")
        snapshot = snapshot_path(source)
        initialize_pdf_state(state)
        config = PdfRouteConfig(state, ocr_mode="never", min_free_bytes=0)
        storage = _Storage(config)
        key = file_key(snapshot)
        provenance = {
            "schema": "neocortex.ocr-page/v1",
            "requested_languages": ["spa", "eng"],
            "effective_languages": ["spa", "eng"],
            "traineddata": [{"language": "spa", "xxh3_128": "hash"}],
            "rotation_degrees": 0,
            "orientation_confidence": 0.0,
            "fallback_attempted": False,
        }
        with pdf_database(state) as connection:
            storage._prepare_document(connection, snapshot, 1, {})
            storage._store_staging_page(
                connection,
                key,
                config.processing_signature,
                0,
                "ocr",
                "texto",
                provenance,
            )
            storage._promote_document(
                connection,
                snapshot,
                1,
                1,
                {},
                None,
                status="done",
                page_start=1,
                page_end=1,
                is_partial=False,
                page_errors=0,
            )
        with closing(sqlite3.connect(state)) as connection:
            version = connection.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone()[0]
            stored = connection.execute(
                "SELECT ocr_provenance_json FROM pages"
            ).fetchone()[0]

    assert version == str(SCHEMA_VERSION) == "12"
    assert json.loads(stored) == provenance


def test_schema_11_migration_preserves_populated_pages_and_staging() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        state = Path(temporary) / "pdf.sqlite3"
        initialize_pdf_state(state)
        with closing(sqlite3.connect(state)) as connection:
            connection.execute("ALTER TABLE pages DROP COLUMN ocr_provenance_json")
            connection.execute(
                "ALTER TABLE page_staging DROP COLUMN ocr_provenance_json"
            )
            connection.execute(
                """INSERT INTO documents(
                file_key,path,size,mtime_ns,birthtime_ns,processing_signature,
                status,updated_ns) VALUES('key','legacy.pdf',1,2,3,'sig','done',4)"""
            )
            connection.execute(
                """INSERT INTO pages(
                file_key,page_number,source,text_zlib,text_chars,profile_json)
                VALUES('key',0,'native',X'78',1,NULL)"""
            )
            connection.execute(
                """INSERT INTO page_staging(
                file_key,processing_signature,page_number,source,text_zlib,text_chars)
                VALUES('key','sig',1,'ocr',X'78',1)"""
            )
            connection.execute(
                "UPDATE metadata SET value='11' WHERE key='schema_version'"
            )
            connection.commit()

        initialize_pdf_state(state)

        with closing(sqlite3.connect(state)) as connection:
            version = connection.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone()[0]
            page = connection.execute(
                "SELECT file_key,ocr_provenance_json FROM pages"
            ).fetchone()
            staged = connection.execute(
                "SELECT file_key,ocr_provenance_json FROM page_staging"
            ).fetchone()

    assert version == "12"
    assert page == ("key", None)
    assert staged == ("key", None)


def test_schema_11_ocr_provenance_migration_rolls_back_ddl_on_failure() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        state = Path(temporary) / "pdf.sqlite3"
        initialize_pdf_state(state)
        with closing(sqlite3.connect(state)) as connection:
            connection.execute("ALTER TABLE pages DROP COLUMN ocr_provenance_json")
            connection.execute(
                "ALTER TABLE page_staging DROP COLUMN ocr_provenance_json"
            )
            connection.execute(
                "UPDATE metadata SET value='11' WHERE key='schema_version'"
            )
            connection.execute("INSERT INTO metadata VALUES('preserved','yes')")
            connection.commit()

        def fail_after_structure(connection: sqlite3.Connection) -> None:
            connection.execute("CREATE TABLE rollback_probe(value TEXT)")
            raise sqlite3.OperationalError("injected OCR migration failure")

        with patch.dict(pdf_schema._PDF_MIGRATIONS, {11: fail_after_structure}):
            with pytest.raises(RuntimeError, match="initialization from version 11"):
                initialize_pdf_state(state)

        with closing(sqlite3.connect(state)) as connection:
            page_columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(pages)")
            }
            staging_columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(page_staging)")
            }
            objects = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE name='rollback_probe'"
                )
            }
            version = connection.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone()
            preserved = connection.execute(
                "SELECT value FROM metadata WHERE key='preserved'"
            ).fetchone()

    assert "ocr_provenance_json" not in page_columns
    assert "ocr_provenance_json" not in staging_columns
    assert objects == set()
    assert version == ("11",)
    assert preserved == ("yes",)


def test_multilingual_missing_packs_fail_doctor_and_pdf_route_preflight() -> None:
    component = json.dumps(
        {
            "name": "tesseract",
            "kind": "native-executable",
            "status": "missing-languages",
            "requested_languages": [
                "spa",
                "eng",
                "deu",
                "chi_sim",
                "chi_tra",
                "osd",
            ],
            "traineddata": [],
        }
    )
    unavailable = TesseractRuntimeProvenance(
        False,
        "tesseract",
        None,
        "5.5.0",
        ("spa", "eng", "osd"),
        component,
        "missing OCR languages: deu, chi_sim, chi_tra",
    )
    with patch(
        "_04_Nucleo_Operativo.pdf_admin.resolve_tesseract_runtime",
        return_value=unavailable,
    ):
        report = doctor_pdf_runtime(ocr_profile="auto-multilingual")
    tesseract = next(check for check in report.checks if check.name == "tesseract")
    assert not tesseract.ok
    assert "missing OCR languages" in tesseract.detail

    config = PdfRouteConfig(
        Path("unused.sqlite3"),
        ocr_profile="auto-multilingual",
    )
    with patch(
        "_04_Nucleo_Operativo.pdf_route.resolve_pdf_tesseract_runtime",
        return_value=unavailable,
    ):
        with pytest.raises(RuntimeError, match="profile preflight failed"):
            PdfRoute(config, object(), object(), 1, 1)


def test_configured_pdf_profile_fails_closed_when_requested_pack_is_missing() -> None:
    component = json.dumps(
        {
            "name": "tesseract",
            "kind": "native-executable",
            "status": "missing-languages",
            "requested_languages": ["spa", "eng"],
            "missing_languages": ["spa"],
            "traineddata": [],
        }
    )
    unavailable = TesseractRuntimeProvenance(
        False,
        "tesseract",
        None,
        "5.5.0",
        ("eng", "osd"),
        component,
        "missing OCR languages: spa",
    )
    config = PdfRouteConfig(Path("unused.sqlite3"), ocr_profile="configured")
    with patch(
        "_04_Nucleo_Operativo.pdf_route.resolve_pdf_tesseract_runtime",
        return_value=unavailable,
    ):
        with pytest.raises(RuntimeError, match="missing OCR languages: spa"):
            PdfRoute(config, object(), object(), 1, 1)
