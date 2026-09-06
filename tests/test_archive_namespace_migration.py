"""Compatibility contracts for the physical Archive namespace migration."""

from __future__ import annotations

import importlib
import json
import os
import pickle
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]

HISTORICAL_SYMBOLS = {
    "neocortex.capabilities.formats.archive.models": (
        "ArchiveRouteSummary",
    ),
    "neocortex.capabilities.formats.archive.route": (
        "ArchiveRouteConfig",
        "_archive_processing_provenance",
        "ArchiveExtractionError",
        "_normalized_member_name",
        "_member_is_special",
        "_compression_ratio",
        "_VisibleHTML",
        "_decode_text",
        "_bounded_text",
        "_xml_text",
        "_html_text",
        "_zip_document_kind",
        "_WalkBudget",
        "_read_zip_member",
        "_embedded_part_selected",
        "_extract_embedded_zip_document",
        "_extract_media_text",
        "_image_media_type",
        "_ExtractedContent",
        "_extract_member_content",
        "_ContainerCounters",
        "_member_key",
        "_virtual_path",
        "_delete_container",
        "_prepare_container",
        "_record_issue",
        "_store_member",
        "_metadata_content",
        "_walk_zip",
        "_publish_container",
        "_store_container_error",
        "_cached_container",
        "_refresh_cached_container",
        "_prune_stale_containers",
        "_ContainerOutcome",
        "_require_current_source",
        "ArchiveRoute",
    ),
    "neocortex.capabilities.formats.archive.state": (
        "_create_archive_schema",
        "archive_schema_contract",
        "archive_database",
        "initialize_archive_state",
        "_validate_reader",
        "ArchiveStatus",
        "ArchiveSearchHit",
        "read_archive_status",
        "_validate_result_limit",
        "_escaped_like_fragment",
        "_row_to_hit",
        "search_archive_state",
        "list_archive_members",
    ),
    "neocortex.capabilities.formats.archive.text_worker": (
        "_parser",
        "_emit",
        "_ocr_config",
        "_prepare_ocr",
        "_bounded_ocr_image",
        "_extract_image",
        "_ocr_pdf_page",
        "_extract_pdf",
        "main",
    ),
    "neocortex.platform.content_types": (
        "DetectedType",
        "_type",
        "_detect_zip",
        "_detect_iso_bmff",
        "_detect_ebml_video",
        "_detect_pe",
        "_detect_document_or_image",
        "_detect_media",
        "_detect_archive",
        "_detect_database_or_executable",
        "_text_decoding",
        "_detect_text",
        "detect_content_type",
    ),
    "neocortex.platform.zip_safety": (
        "ZipStructureError",
        "ZipStructure",
        "RawDeflateMember",
        "_read_exact",
        "_find_eocd",
        "_zip64_values",
        "_inspect_zip_source",
        "inspect_zip_stream",
        "inspect_zip_structure",
        "inspect_zip_bytes",
        "_validate_raw_member_bounds",
        "_raw_member_payload_offset",
        "_extend_raw_deflate_output",
        "_decompress_raw_member",
        "read_raw_deflate_member",
    ),
}


def test_archive_implementation_lives_under_the_product_namespace() -> None:
    canonical_root = PROJECT_ROOT / "neocortex" / "capabilities" / "formats" / "archive"
    for module_name in (
        "neocortex.capabilities.formats.archive.models",
        "neocortex.capabilities.formats.archive.route",
        "neocortex.capabilities.formats.archive.state",
        "neocortex.capabilities.formats.archive.text_worker",
    ):
        module = importlib.import_module(module_name)
        assert Path(module.__file__).resolve().is_relative_to(canonical_root)

    for relative_path in (
        "neocortex/runtime/config/application_config_projections.py",
        "neocortex/api/cli/cli_archive.py",
        "neocortex/knowledge/knowledge_snapshot.py",
        "neocortex/runtime/models.py",
        "neocortex/runtime/orchestration/orchestrator.py",
        "neocortex/runtime/orchestration/route_registry.py",
        "neocortex/semantic/semantic_plan_owners.py",
        "neocortex/safety/state_topology_contracts.py",
    ):
        source = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
        assert "neocortex.capabilities.formats.archive" in source


def test_archive_parent_packages_remain_import_light() -> None:
    script = textwrap.dedent(
        """
        import sys

        import neocortex.capabilities
        import neocortex.capabilities.formats
        import neocortex.capabilities.formats.archive

        forbidden = {
            "neocortex.capabilities.formats.archive.models",
            "neocortex.capabilities.formats.archive.route",
            "neocortex.capabilities.formats.archive.state",
            "neocortex.capabilities.formats.archive.text_worker",
            "neocortex.platform.content_types",
            "neocortex.platform.zip_safety",
        }
        loaded = sorted(forbidden.intersection(sys.modules))
        if loaded:
            raise SystemExit("eager Archive imports: " + ",".join(loaded))
        print("ARCHIVE_PARENTS_IMPORT_LIGHT")
        """
    )
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        (sys.executable, "-B", "-c", script),
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert completed.stdout.strip() == "ARCHIVE_PARENTS_IMPORT_LIGHT"


def test_archive_defined_symbols_are_owned_by_canonical_modules() -> None:
    models = importlib.import_module(
        "neocortex.capabilities.formats.archive.models"
    )
    route = importlib.import_module(
        "neocortex.capabilities.formats.archive.route"
    )
    state = importlib.import_module(
        "neocortex.capabilities.formats.archive.state"
    )
    content_types = importlib.import_module(
        "neocortex.platform.content_types"
    )
    zip_safety = importlib.import_module(
        "neocortex.platform.zip_safety"
    )
    for canonical_name, symbol_names in HISTORICAL_SYMBOLS.items():
        canonical = importlib.import_module(canonical_name)
        for symbol_name in symbol_names:
            symbol = getattr(canonical, symbol_name)
            assert symbol.__module__ == canonical.__name__
            assert pickle.loads(pickle.dumps(symbol, protocol=5)) is symbol

    instances = (
        models.ArchiveRouteSummary(),
        route.ArchiveRouteConfig(Path("archive-state.sqlite3")),
        state.ArchiveStatus(False),
        state.ArchiveSearchHit(
            "member-key",
            "container.zip!/member.txt",
            "container.zip",
            "member.txt",
            "member.txt",
            1,
            "txt",
            "text/plain",
            "indexed",
            7,
        ),
        content_types.DetectedType("text/plain", ".txt", frozenset({".txt"}), "fixture"),
        zip_safety.ZipStructure(1, 2, 3, False),
        zip_safety.RawDeflateMember(b"payload", 7, 123),
    )
    for instance in instances:
        restored = pickle.loads(pickle.dumps(instance, protocol=5))
        assert type(restored) is type(instance)
        assert restored == instance


def test_archive_route_invokes_the_canonical_worker_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route = importlib.import_module(
        "neocortex.capabilities.formats.archive.route"
    )
    observed: list[tuple[str, ...]] = []

    def fake_capture(command, **_kwargs):
        observed.append(tuple(command))
        return subprocess.CompletedProcess(
            command,
            0,
            b'{"ok":true,"text":"worker text","truncated":false}',
            b"",
        )

    monkeypatch.setattr(route, "run_bounded_capture", fake_capture)
    extracted = route._extract_media_text(
        b"fixture",
        kind="pdf",
        char_limit=100,
        config=route.ArchiveRouteConfig(
            Path("archive-state.sqlite3"),
            ocr_mode="never",
        ),
    )

    assert extracted == ("worker text", False, None, "native")
    assert observed[0][:3] == (
        sys.executable,
        "-m",
        "neocortex.capabilities.formats.archive.text_worker",
    )


@pytest.mark.parametrize(
    "module_name",
    (
        "neocortex.capabilities.formats.archive.text_worker",
        "neocortex.capabilities.formats.archive.text_worker",
        "neocortex.capabilities.formats.archive.text_worker",
    ),
)
def test_archive_worker_dash_m_rejects_invalid_limits(
    module_name: str,
) -> None:
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        (
            sys.executable,
            "-B",
            "-m",
            module_name,
            "--max-input-bytes",
            "0",
            "--max-pages",
            "1",
            "--max-chars",
            "1",
        ),
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 2
    assert json.loads(completed.stdout) == {
        "ok": False,
        "reason": "invalid_worker_limits",
    }
    assert completed.stderr == ""


def test_archive_versions_schema_and_store_contract_remain_stable() -> None:
    route = importlib.import_module(
        "neocortex.capabilities.formats.archive.route"
    )
    state = importlib.import_module(
        "neocortex.capabilities.formats.archive.state"
    )
    content_types = importlib.import_module(
        "neocortex.platform.content_types"
    )

    assert route.ARCHIVE_MIME == "application/zip"
    assert route.ARCHIVE_ROUTE_VERSION == "archive-route-v3"
    assert state.ARCHIVE_SCHEMA_VERSION == 2
    assert content_types.DETECTOR_VERSION == "content-types-v3"
    assert route.ArchiveRouteConfig(Path("state") / "archive.sqlite3").state_path.name == (
        "archive.sqlite3"
    )
