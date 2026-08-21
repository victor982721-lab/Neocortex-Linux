"""Compatibility contracts for the capability-owned DOCX namespace."""

from __future__ import annotations

import importlib
import os
import pickle
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CANONICAL_ROOT = "_04_Nucleo_Operativo.capabilities.formats.docx"
LEGACY_ROOT = "_04_Nucleo_Operativo"
MODULE_NAMES = ("integrity", "layout", "models", "route", "schema", "state")

HISTORICAL_SYMBOLS = {
    "integrity": (
        "member_upper_bounds",
        "recover_raw_deflate_member",
        "recovered_member_diagnostic",
        "_is_policy_limit",
        "diagnostic_for_member",
        "fatal_member_error",
        "classify_docx_exception",
    ),
    "layout": (
        "TextBudget",
        "normalized_text_digest",
        "_collect_element_text",
        "_record_paragraph_layout",
        "_section_layout",
        "_record_element_layout",
        "_flush_text_pieces",
        "xml_text_and_layout",
        "_page_class",
        "layout_result",
    ),
    "models": (
        "DocxRouteConfig",
        "_docx_processing_provenance",
        "DocxRouteSummary",
        "DocxDiagnostic",
        "DocxFailure",
        "DocxProcessingError",
        "DocxPart",
        "ExtractedDocx",
    ),
    "route": (
        "_LiveDocxCachePathConflict",
        "_review_candidates",
        "_review_reason_codes",
        "_read_member",
        "_compress_text",
        "_part_kind",
        "_estimated_docx_memory_bytes",
        "_processing_error",
        "_effective_recovery_error",
        "_read_xml_root",
        "_parse_word_member",
        "_DocxExtractionAccumulator",
        "_validate_docx_archive",
        "_required_docx_members",
        "_validate_docx_contract",
        "_validate_docx_relationship",
        "_extract_docx_parts",
        "_extract_optional_docx_part",
        "_docx_metadata",
        "_build_extracted_docx",
        "extract_docx",
        "_DocxCandidateOutcome",
        "DocxRoute",
        "search_docx_state",
        "list_docx_layout_groups",
        "list_missing_pdf_counterparts",
    ),
    "schema": (
        "_docx_table_ddl",
        "_docx_path_index_ddl",
        "_quoted_identifier",
        "_column_names",
        "_add_columns",
        "_create_tables_from",
        "_create_indexes_from",
        "_ensure_structure",
        "_ensure_current_structure",
        "_ensure_v5_structure",
        "_no_data_migration",
        "_migrate_birthtime",
        "_migrate_explicit_path_collations",
        "_ordered_columns",
        "_copy_table_exact",
        "_validate_docx_v5_schema",
        "_migrate_platform_path_collation",
        "create_fresh_docx_schema",
        "migrate_docx_schema",
        "_build_canonical_schema",
        "_build_docx_v5_canonical_schema",
        "_build_docx_v5_legacy_v1_schema",
        "_build_docx_v5_legacy_v2_schema",
        "_build_metadata_schema",
        "_metadata_contract",
        "_docx_schema_contracts",
        "_docx_v5_schema_contracts",
        "validate_docx_metadata",
        "validate_docx_schema",
    ),
    "state": (
        "connect_docx_state",
        "docx_database",
        "initialize_docx_state",
    ),
}


def _canonical_module(name: str):
    return importlib.import_module(f"{CANONICAL_ROOT}.{name}")


def _legacy_module(name: str):
    return importlib.import_module(f"{LEGACY_ROOT}.docx_{name}")


@pytest.mark.parametrize("name", MODULE_NAMES)
def test_legacy_modules_are_real_aliases_of_canonical_modules(name: str) -> None:
    canonical = _canonical_module(name)
    legacy = _legacy_module(name)

    assert legacy is canonical
    assert sys.modules[f"{LEGACY_ROOT}.docx_{name}"] is canonical


@pytest.mark.parametrize("name", MODULE_NAMES)
def test_module_symbols_keep_historical_serializable_fqns(name: str) -> None:
    module = _canonical_module(name)
    historical_module = f"{LEGACY_ROOT}.docx_{name}"

    for symbol_name in HISTORICAL_SYMBOLS[name]:
        symbol = getattr(module, symbol_name)
        assert symbol.__module__ == historical_module
        assert pickle.loads(pickle.dumps(symbol, protocol=5)) is symbol


def test_docx_config_remains_pickle_compatible_through_legacy_fqn() -> None:
    models = _canonical_module("models")
    config = models.DocxRouteConfig(Path("state/docx.sqlite3"))

    restored = pickle.loads(pickle.dumps(config, protocol=5))

    assert restored == config
    assert type(restored) is models.DocxRouteConfig
    assert type(restored).__module__ == "_04_Nucleo_Operativo.docx_models"


def test_legacy_monkeypatch_seams_reach_canonical_globals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cases = (
        ("integrity", "read_raw_deflate_member"),
        ("route", "extract_docx"),
        ("state", "connect_docx_state"),
    )
    sentinel = object()

    for module_name, attribute_name in cases:
        legacy = _legacy_module(module_name)
        canonical = _canonical_module(module_name)
        monkeypatch.setattr(legacy, attribute_name, sentinel)
        assert getattr(canonical, attribute_name) is sentinel


def test_docx_uses_the_shared_zip_safety_primitives() -> None:
    shared = importlib.import_module("_04_Nucleo_Operativo.platform.shared.zip_safety")
    integrity = _canonical_module("integrity")
    route = _canonical_module("route")

    assert integrity.RawDeflateMember is shared.RawDeflateMember
    assert integrity.ZipStructureError is shared.ZipStructureError
    assert integrity.read_raw_deflate_member is shared.read_raw_deflate_member
    assert route.ZipStructureError is shared.ZipStructureError
    assert route.inspect_zip_structure is shared.inspect_zip_structure


def test_docx_schema_state_and_algorithm_contracts_remain_stable() -> None:
    models = _canonical_module("models")
    schema = _canonical_module("schema")
    state = _canonical_module("state")

    assert models.ALGORITHM_VERSION == "docx-route-v3"
    assert schema.DOCX_SCHEMA_VERSION == state.SCHEMA_VERSION == 6
    assert schema.UNKNOWN_BIRTHTIME_NS == state.UNKNOWN_BIRTHTIME_NS == -1
    assert tuple(schema._DOCX_MIGRATIONS) == (1, 2, 3, 4, 5)
    assert state._DOCX_SQLITE_POLICY.label == "DOCX state"
    writer_pragmas = state._DOCX_SQLITE_POLICY.writer_pragmas
    assert writer_pragmas is not None
    assert writer_pragmas.journal_mode == "WAL"


def test_docx_package_import_is_light_in_a_fresh_process() -> None:
    script = textwrap.dedent(
        f"""
        import importlib
        import sys

        importlib.import_module({CANONICAL_ROOT!r})
        forbidden = {{
            {", ".join(repr(f"{CANONICAL_ROOT}.{name}") for name in MODULE_NAMES)},
            "_04_Nucleo_Operativo.platform.shared.zip_safety",
            "neocortex.deduplication",
        }}
        loaded = forbidden.intersection(sys.modules)
        if loaded:
            raise SystemExit("DOCX package eagerly loaded: " + ",".join(sorted(loaded)))
        """
    )
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"

    completed = subprocess.run(
        [sys.executable, "-B", "-c", script],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
