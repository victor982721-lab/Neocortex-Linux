from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from _04_Nucleo_Operativo.platform.shared.capability_registry import (
    CAPABILITY_REGISTRY,
    CAPABILITY_REGISTRY_SCHEMA,
    CapabilityLogicalOwnerBinding,
    CapabilityModuleBinding,
    CapabilityRegistry,
    CapabilityRouteContract,
    CapabilitySpec,
    CapabilityStateContract,
    PythonSymbolRef,
    canonical_target_architecture_families,
    capability_canonical_logical_owner_bindings,
    capability_logical_owner_bindings,
    capability_registry_canonical_json,
    capability_registry_fingerprint,
    capability_registry_payload,
    capability_source_logical_owner_bindings,
    parse_capability_registry_payload,
    resolve_canonical_capabilities,
    resolve_source_capabilities,
    source_compatibility_architecture_families,
    source_target_architecture_families,
)


_FORMATS_ROOT = "neocortex.capabilities.formats"
_EXPECTED_TEST_ROOTS = {
    "archive": (
        "tests/test_archive_cli.py",
        "tests/test_archive_namespace_migration.py",
        "tests/test_archive_route.py",
        "tests/test_archive_text_worker_unit.py",
        "tests/test_capability_registry.py",
        "tests/test_format_module_move_compatibility.py",
        "tests/test_route_schema_contracts.py",
    ),
    "audio": (
        "tests/test_audio_probe_bounded.py",
        "tests/test_audio_route.py",
        "tests/test_capability_registry.py",
        "tests/test_cli_audio_surface.py",
        "tests/test_format_module_move_compatibility.py",
        "tests/test_route_schema_contracts.py",
    ),
    "docx": (
        "tests/test_capability_registry.py",
        "tests/test_docx_namespace_migration.py",
        "tests/test_docx_route.py",
        "tests/test_format_module_move_compatibility.py",
        "tests/test_pdf_docx_schema_contracts.py",
    ),
    "image": (
        "tests/test_capability_registry.py",
        "tests/test_format_module_move_compatibility.py",
        "tests/test_image_adult.py",
        "tests/test_image_analysis.py",
        "tests/test_image_classifier_memory.py",
        "tests/test_image_document.py",
        "tests/test_image_features.py",
        "tests/test_image_isolation.py",
        "tests/test_image_namespace_migration.py",
        "tests/test_image_ocr_profiles.py",
        "tests/test_image_png.py",
        "tests/test_image_route.py",
        "tests/test_image_schema_contract.py",
        "tests/test_image_semantics.py",
    ),
    "office": (
        "tests/test_application_config_media_projections.py",
        "tests/test_capability_registry.py",
        "tests/test_cli_review_office.py",
        "tests/test_format_module_move_compatibility.py",
        "tests/test_office_namespace_migration.py",
        "tests/test_office_route.py",
        "tests/test_route_schema_contracts.py",
        "tests/test_text_derivation_route.py",
        "tests/test_text_implementation_identity.py",
    ),
    "video": (
        "tests/test_capability_registry.py",
        "tests/test_cli_video_surface.py",
        "tests/test_format_module_move_compatibility.py",
        "tests/test_route_schema_contracts.py",
        "tests/test_video_content_types.py",
        "tests/test_video_frames.py",
        "tests/test_video_knowledge_integration.py",
        "tests/test_video_namespace_migration.py",
        "tests/test_video_probe.py",
        "tests/test_video_route.py",
        "tests/test_video_state.py",
    ),
}


def _ids(values: tuple[CapabilitySpec, ...]) -> tuple[str, ...]:
    return tuple(item.capability_id for item in values)


def _module_map(capability_id: str) -> dict[str, tuple[str | None, str]]:
    capability = CAPABILITY_REGISTRY.by_id(capability_id)
    return {
        item.role: (item.legacy_module_id, item.canonical_module_id) for item in capability.modules
    }


def test_registry_test_root_matrix_is_independent_complete_and_live() -> None:
    repository = Path(__file__).resolve().parents[1]

    assert sum(len(roots) for roots in _EXPECTED_TEST_ROOTS.values()) == 52
    assert {
        capability.capability_id: capability.test_roots
        for capability in CAPABILITY_REGISTRY.capabilities
    } == _EXPECTED_TEST_ROOTS
    for roots in _EXPECTED_TEST_ROOTS.values():
        for relative in roots:
            target = repository / relative
            assert target.is_file(), relative
            assert not target.is_symlink(), relative


def test_registry_declares_exact_format_module_moves() -> None:
    assert CAPABILITY_REGISTRY.schema == CAPABILITY_REGISTRY_SCHEMA
    assert _ids(CAPABILITY_REGISTRY.capabilities) == (
        "archive",
        "audio",
        "docx",
        "image",
        "office",
        "video",
    )
    assert {
        (item.architecture_family_id, item.compatibility_family_id)
        for item in CAPABILITY_REGISTRY.capabilities
    } == {("_04.capabilities.formats", "_04.compat.formats")}

    assert _module_map("archive") == {
        "models": (
            "_04_Nucleo_Operativo.archive_models",
            f"{_FORMATS_ROOT}.archive.models",
        ),
        "route": (
            "_04_Nucleo_Operativo.archive_route",
            f"{_FORMATS_ROOT}.archive.route",
        ),
        "state": (
            "_04_Nucleo_Operativo.archive_state",
            f"{_FORMATS_ROOT}.archive.state",
        ),
        "text_worker": (
            "_04_Nucleo_Operativo.archive_text_worker",
            f"{_FORMATS_ROOT}.archive.text_worker",
        ),
    }
    assert _module_map("audio") == {
        role: (
            f"_04_Nucleo_Operativo.audio_{role}",
            f"{_FORMATS_ROOT}.audio.{role}",
        )
        for role in ("models", "probe", "route", "state", "whisper")
    }
    assert _module_map("docx") == {
        role: (f"_04_Nucleo_Operativo.docx_{role}", f"{_FORMATS_ROOT}.docx.{role}")
        for role in ("integrity", "layout", "models", "route", "schema", "state")
    }
    assert _module_map("image") == {
        role: (f"_04_Nucleo_Operativo.image_{role}", f"{_FORMATS_ROOT}.image.{role}")
        for role in (
            "adult",
            "analysis",
            "decision",
            "decode",
            "document",
            "errors",
            "features",
            "isolation",
            "models",
            "png",
            "policy",
            "route",
            "semantics",
            "state",
            "visual",
        )
    }
    assert _module_map("office") == {
        "extraction": (None, f"{_FORMATS_ROOT}.office.extraction"),
        "extraction_support": (None, f"{_FORMATS_ROOT}.office.extraction_support"),
        "legacy_worker": (
            "_04_Nucleo_Operativo.legacy_office_worker",
            f"{_FORMATS_ROOT}.office.legacy_worker",
        ),
        "models": (None, f"{_FORMATS_ROOT}.office.models"),
        "route": (
            "_04_Nucleo_Operativo.office_route",
            f"{_FORMATS_ROOT}.office.route",
        ),
        "state": (
            "_04_Nucleo_Operativo.office_state",
            f"{_FORMATS_ROOT}.office.state",
        ),
        "xlsx": (None, f"{_FORMATS_ROOT}.office.xlsx"),
    }
    assert _module_map("video") == {
        role: (
            f"_04_Nucleo_Operativo.video_{role}",
            f"{_FORMATS_ROOT}.video.{role}",
        )
        for role in ("frames", "models", "probe", "route", "state")
    }

    serialized = capability_registry_canonical_json()
    assert '"legacy_module_id":"_04_Nucleo_Operativo.content_types"' not in serialized
    assert '"legacy_module_id":"_04_Nucleo_Operativo.zip_safety"' not in serialized
    assert "cli_archive" not in serialized
    assert "cli_docx" not in serialized


def test_routes_preserve_exact_subjects_and_public_fqns() -> None:
    archive = CAPABILITY_REGISTRY.by_route("archive")
    audio = CAPABILITY_REGISTRY.by_route("audio")
    docx = CAPABILITY_REGISTRY.by_route("docx")
    image = CAPABILITY_REGISTRY.by_route("image")
    office = CAPABILITY_REGISTRY.by_route("office")
    video = CAPABILITY_REGISTRY.by_route("video")

    assert archive.route is not None
    assert archive.route.input_source == "route_candidates"
    assert (archive.route.subject_match_kind, archive.route.subject_value) == (
        "exact_mime",
        "application/zip",
    )
    assert archive.route.route_class.qualified_name == (
        f"{_FORMATS_ROOT}.archive.route.ArchiveRoute"
    )
    assert archive.route.config_class.qualified_name.endswith(".archive.route.ArchiveRouteConfig")
    assert archive.route.summary_class.qualified_name.endswith(".archive.route.ArchiveRouteSummary")
    assert archive.executable_module_ids == (f"{_FORMATS_ROOT}.archive.text_worker",)

    assert audio.route is not None
    assert (audio.route.subject_match_kind, audio.route.subject_value) == (
        "mime_prefix",
        "audio/",
    )
    assert audio.route.route_class.qualified_name.endswith(".audio.route.AudioRoute")
    assert audio.route.config_class.qualified_name.endswith(".audio.models.AudioRouteConfig")
    assert audio.route.summary_class.qualified_name.endswith(".audio.models.AudioRouteSummary")
    assert audio.route.version_symbol.qualified_name.endswith(".audio.models.AUDIO_ROUTE_VERSION")

    assert docx.route is not None
    assert (docx.route.subject_match_kind, docx.route.subject_value) == (
        "exact_mime",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    assert docx.route.config_class.qualified_name.endswith(".docx.route.DocxRouteConfig")
    assert docx.route.summary_class.qualified_name.endswith(".docx.route.DocxRouteSummary")
    assert docx.route.version_symbol.qualified_name.endswith(".docx.models.ALGORITHM_VERSION")

    assert image.route is not None
    assert (image.route.subject_match_kind, image.route.subject_value) == (
        "mime_prefix",
        "image/",
    )
    assert image.route.route_class.qualified_name.endswith(".image.route.ImageRoute")
    assert image.route.version_symbol.qualified_name.endswith(".image.route.IMAGE_ROUTE_VERSION")

    assert office.route is not None
    assert (office.route.subject_match_kind, office.route.subject_value) == (
        "exact_mime",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    assert office.route.route_class.qualified_name.endswith(".office.route.OfficeRoute")
    assert office.route.config_class.qualified_name.endswith(".office.models.OfficeRouteConfig")
    assert office.route.summary_class.qualified_name.endswith(".office.models.OfficeRouteSummary")
    assert office.executable_module_ids == (f"{_FORMATS_ROOT}.office.legacy_worker",)

    assert video.route is not None
    assert (video.route.subject_match_kind, video.route.subject_value) == (
        "mime_prefix",
        "video/",
    )
    assert video.route.route_class.qualified_name.endswith(".video.route.VideoRoute")
    assert video.route.summary_class.qualified_name.endswith(".video.models.VideoRouteSummary")


def test_state_foreign_keys_preserve_live_store_contracts() -> None:
    expected = {
        "archive": (
            "sqlite:archive.sqlite3",
            "archive.sqlite3",
            1,
            "documents",
            "if_present",
            "ARCHIVE_SCHEMA_VERSION",
        ),
        "audio": (
            "sqlite:audio.sqlite3",
            "audio.sqlite3",
            2,
            "documents",
            "configured",
            "AUDIO_SCHEMA_VERSION",
        ),
        "docx": (
            "sqlite:docx.sqlite3",
            "docx.sqlite3",
            6,
            "documents",
            "configured",
            "DOCX_SCHEMA_VERSION",
        ),
        "image": (
            "sqlite:image.sqlite3",
            "image.sqlite3",
            5,
            "images",
            "configured",
            "SCHEMA_VERSION",
        ),
        "office": (
            "sqlite:office.sqlite3",
            "office.sqlite3",
            3,
            "documents",
            "configured",
            "OFFICE_SCHEMA_VERSION",
        ),
        "video": (
            "sqlite:video.sqlite3",
            "video.sqlite3",
            2,
            "videos",
            "configured",
            "VIDEO_SCHEMA_VERSION",
        ),
    }
    for capability_id, values in expected.items():
        capability = CAPABILITY_REGISTRY.by_id(capability_id)
        assert capability.logical_owner_id == capability_id
        assert capability.state is not None
        state = capability.state
        assert state.state_owner_id == capability_id
        assert (
            state.state_store_id,
            state.database_name,
            state.expected_schema_version,
            state.knowledge_read_kind,
            state.knowledge_capture_mode,
            state.schema_version_symbol.symbol_name,
        ) == values
        assert state.knowledge_path_attribute == capability_id
        assert state.storage_engine == "sqlite"


def test_source_and_canonical_resolvers_are_disjoint_and_never_fallback() -> None:
    source = "_04_Nucleo_Operativo.archive_route"
    canonical = f"{_FORMATS_ROOT}.archive.route"
    nested_future = f"{_FORMATS_ROOT}.archive.future.reader"

    assert _ids(resolve_source_capabilities(source)) == ("archive",)
    assert resolve_source_capabilities(canonical) == ()
    assert resolve_source_capabilities(source + ".nested") == ()

    assert _ids(resolve_canonical_capabilities(canonical)) == ("archive",)
    assert _ids(resolve_canonical_capabilities(nested_future)) == ("archive",)
    assert resolve_canonical_capabilities(source) == ()

    assert source_target_architecture_families(source) == ("_04.capabilities.formats",)
    assert source_compatibility_architecture_families(source) == ("_04.compat.formats",)
    assert source_compatibility_architecture_families(canonical) == ()
    assert source_target_architecture_families(canonical) == ()
    assert canonical_target_architecture_families(canonical) == ("_04.capabilities.formats",)
    assert canonical_target_architecture_families(source) == ()

    for capability in CAPABILITY_REGISTRY.capabilities:
        for binding in capability.modules:
            assert _ids(resolve_canonical_capabilities(binding.canonical_module_id)) == (
                capability.capability_id,
            )
            assert resolve_source_capabilities(binding.canonical_module_id) == ()
            legacy_module_id = binding.legacy_module_id
            if legacy_module_id is None:
                continue
            assert _ids(resolve_source_capabilities(legacy_module_id)) == (
                capability.capability_id,
            )
            assert resolve_canonical_capabilities(legacy_module_id) == ()


def test_logical_owner_projection_exposes_disjoint_canonical_and_source_bindings() -> None:
    bindings = capability_canonical_logical_owner_bindings()
    assert tuple(item.owner_id for item in bindings) == (
        "archive",
        "audio",
        "docx",
        "image",
        "office",
        "video",
    )
    assert tuple(item.selector_id for item in bindings) == (
        "archive-core-modules",
        "audio-core-modules",
        "docx-core-modules",
        "image-core-modules",
        "office-core-modules",
        "video-core-modules",
    )
    assert all(item.match_kind == "module_tree" for item in bindings)
    assert tuple(item.value for item in bindings) == tuple(
        f"{_FORMATS_ROOT}.{owner}"
        for owner in ("archive", "audio", "docx", "image", "office", "video")
    )
    assert tuple(item.state_owner_ids for item in bindings) == (
        ("archive",),
        ("audio",),
        ("docx",),
        ("image",),
        ("office",),
        ("video",),
    )
    assert all("_04_Nucleo_Operativo.archive_" not in item.value for item in bindings)

    source_bindings = capability_source_logical_owner_bindings()
    assert all(item.match_kind == "exact_module" for item in source_bindings)
    assert len(source_bindings) == 38
    archive_source = tuple(item for item in source_bindings if item.owner_id == "archive")
    assert tuple(item.selector_id for item in archive_source) == (
        "archive-legacy-models",
        "archive-legacy-route",
        "archive-legacy-state",
        "archive-legacy-text-worker",
    )
    assert tuple(item.value for item in archive_source) == (
        "_04_Nucleo_Operativo.archive_models",
        "_04_Nucleo_Operativo.archive_route",
        "_04_Nucleo_Operativo.archive_state",
        "_04_Nucleo_Operativo.archive_text_worker",
    )
    audio_source = tuple(item for item in source_bindings if item.owner_id == "audio")
    assert tuple(item.selector_id for item in audio_source) == (
        "audio-legacy-models",
        "audio-legacy-probe",
        "audio-legacy-route",
        "audio-legacy-state",
        "audio-legacy-whisper",
    )
    office_source = tuple(item for item in source_bindings if item.owner_id == "office")
    assert tuple(item.selector_id for item in office_source) == (
        "office-legacy-legacy-worker",
        "office-legacy-route",
        "office-legacy-state",
    )
    assert tuple(item.value for item in office_source) == (
        "_04_Nucleo_Operativo.legacy_office_worker",
        "_04_Nucleo_Operativo.office_route",
        "_04_Nucleo_Operativo.office_state",
    )
    video_source = tuple(item for item in source_bindings if item.owner_id == "video")
    assert tuple(item.selector_id for item in video_source) == (
        "video-legacy-frames",
        "video-legacy-models",
        "video-legacy-probe",
        "video-legacy-route",
        "video-legacy-state",
    )
    assert capability_logical_owner_bindings() == tuple(
        binding
        for capability in CAPABILITY_REGISTRY.capabilities
        for binding in capability.logical_owner_bindings()
    )


def test_payload_round_trip_is_strict_canonical_and_fingerprinted() -> None:
    payload = capability_registry_payload()
    encoded = capability_registry_canonical_json()
    raw_capabilities = payload["capabilities"]
    assert isinstance(raw_capabilities, list)

    assert parse_capability_registry_payload(json.loads(encoded)) == CAPABILITY_REGISTRY
    assert encoded == json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert capability_registry_fingerprint() == capability_registry_fingerprint()
    assert capability_registry_fingerprint().startswith("capability-registry-v1:sha256:")
    digest = capability_registry_fingerprint().removeprefix("capability-registry-v1:sha256:")
    assert len(digest) == 64

    with pytest.raises(ValueError, match="fields are invalid"):
        parse_capability_registry_payload({**payload, "invented": False})
    with pytest.raises(ValueError, match="JSON array"):
        parse_capability_registry_payload(
            {
                "schema": CAPABILITY_REGISTRY_SCHEMA,
                "capabilities": tuple(raw_capabilities),
            }
        )
    with pytest.raises(ValueError, match="schema"):
        parse_capability_registry_payload({"schema": "future", "capabilities": []})


def test_registry_rejects_duplicate_overlap_and_inconsistent_state() -> None:
    archive = CAPABILITY_REGISTRY.by_id("archive")
    docx = CAPABILITY_REGISTRY.by_id("docx")
    archive_state = archive.state
    image_route = CAPABILITY_REGISTRY.by_id("image").route
    assert archive_state is not None
    assert image_route is not None

    with pytest.raises(ValueError, match="canonically ordered"):
        CapabilityRegistry(CAPABILITY_REGISTRY_SCHEMA, (docx, archive))
    with pytest.raises(ValueError, match="module role"):
        replace(archive, modules=(archive.modules[0], archive.modules[0]))
    with pytest.raises(ValueError, match="store id and database name disagree"):
        replace(
            archive,
            state=replace(archive_state, database_name="different.sqlite3"),
        )
    with pytest.raises(ValueError, match="families must differ"):
        replace(
            archive,
            compatibility_family_id=archive.architecture_family_id,
        )
    with pytest.raises(ValueError, match="MIME-prefix"):
        replace(
            image_route,
            subject_value="image/png",
        )
    with pytest.raises(ValueError, match="inside their module tree"):
        replace(
            archive,
            modules=(
                replace(archive.modules[0], canonical_module_id=f"{_FORMATS_ROOT}.docx.models"),
                *archive.modules[1:],
            ),
        )
    cross_tree_archive = replace(
        archive,
        modules=(
            replace(
                archive.modules[0],
                    legacy_module_id="_04_Nucleo_Operativo.capabilities.formats.docx.compat_models",
            ),
            *archive.modules[1:],
        ),
    )
    with pytest.raises(ValueError, match="match any canonical capability tree"):
        CapabilityRegistry(
            CAPABILITY_REGISTRY_SCHEMA,
            (cross_tree_archive, docx, CAPABILITY_REGISTRY.by_id("image")),
        )


def test_registry_contract_types_are_frozen_and_slotted() -> None:
    contract_types = (
        PythonSymbolRef,
        CapabilityModuleBinding,
        CapabilityRouteContract,
        CapabilityStateContract,
        CapabilityLogicalOwnerBinding,
        CapabilitySpec,
        CapabilityRegistry,
    )
    assert all(getattr(item, "__slots__", None) for item in contract_types)

    archive = CAPABILITY_REGISTRY.by_id("archive")
    with pytest.raises(FrozenInstanceError):
        archive.capability_id = "changed"  # type: ignore[misc]


def test_registry_value_contracts_fail_closed_for_invalid_runtime_values() -> None:
    archive = CAPABILITY_REGISTRY.by_id("archive")
    archive_route = archive.route
    archive_state = archive.state
    assert archive_route is not None
    assert archive_state is not None

    with pytest.raises(ValueError, match="non-empty trimmed text"):
        PythonSymbolRef("", "Symbol")
    with pytest.raises(ValueError, match="exceeds its bound"):
        PythonSymbolRef("a" * 513, "Symbol")
    with pytest.raises(ValueError, match="is invalid"):
        PythonSymbolRef("valid.module", "not-a-symbol")
    with pytest.raises(ValueError, match="immutable tuple"):
        replace(archive.modules[0], public_symbols=["Symbol"])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="must differ"):
        replace(
            archive.modules[0],
            legacy_module_id=archive.modules[0].canonical_module_id,
        )
    with pytest.raises(ValueError, match="must remain silent"):
        replace(archive.modules[0], warning_policy="warn")  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="input source is invalid"):
        replace(archive_route, input_source="unknown")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="match kind is invalid"):
        replace(archive_route, subject_match_kind="unknown")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="lowercase MIME"):
        replace(archive_route, subject_value="Application/ZIP")
    with pytest.raises(ValueError, match="cannot end with a slash"):
        replace(archive_route, subject_value="application/")

    with pytest.raises(ValueError, match="identify a SQLite file"):
        replace(archive_state, state_store_id="sqlite:archive.db", database_name="archive.db")
    with pytest.raises(ValueError, match="positive integer"):
        replace(archive_state, expected_schema_version=True)
    with pytest.raises(ValueError, match="capture mode is invalid"):
        replace(archive_state, knowledge_capture_mode="always")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="belong to its schema module"):
        replace(
            archive_state,
            schema_version_symbol=PythonSymbolRef("different.module", "SCHEMA_VERSION"),
        )
    with pytest.raises(ValueError, match="must remain SQLite"):
        replace(archive_state, storage_engine="other")  # type: ignore[arg-type]

    canonical_binding = archive.canonical_logical_owner_binding()
    with pytest.raises(ValueError, match="match kind is invalid"):
        replace(canonical_binding, match_kind="prefix")  # type: ignore[arg-type]


def test_capability_and_registry_reject_incomplete_cross_references() -> None:
    archive = CAPABILITY_REGISTRY.by_id("archive")
    route = archive.route
    assert route is not None

    with pytest.raises(ValueError, match="typed module bindings"):
        replace(archive, modules=())
    with pytest.raises(ValueError, match="inside the canonical tree"):
        replace(
            archive,
            modules=(
                replace(
                    archive.modules[0],
                    legacy_module_id=f"{archive.canonical_module_tree}.legacy",
                ),
                *archive.modules[1:],
            ),
        )
    with pytest.raises(ValueError, match="route FQNs"):
        replace(
            archive,
            route=replace(
                route,
                route_class=PythonSymbolRef("outside.module", "ArchiveRoute"),
            ),
        )
    with pytest.raises(ValueError, match=r"normalized tests/\*\.py path"):
        replace(archive, test_roots=("../test_archive.py",))
    with pytest.raises(ValueError, match="executable modules"):
        replace(archive, executable_module_ids=("outside.worker",))
    with pytest.raises(ValueError, match="unknown module role"):
        archive.module("missing")

    with pytest.raises(ValueError, match="schema is invalid"):
        CapabilityRegistry("future", (archive,))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="requires typed capabilities"):
        CapabilityRegistry(CAPABILITY_REGISTRY_SCHEMA, ())
    with pytest.raises(ValueError, match="unknown capability"):
        CAPABILITY_REGISTRY.by_id("missing")
    with pytest.raises(ValueError, match="unknown capability route"):
        CAPABILITY_REGISTRY.by_route("missing")
    with pytest.raises(ValueError, match="resolver mode is invalid"):
        CAPABILITY_REGISTRY.target_families(
            "_04_Nucleo_Operativo.archive_route",
            resolver_mode="invalid",  # type: ignore[arg-type]
        )


def test_registry_cold_import_does_not_load_implementations_or_sqlite() -> None:
    repository = Path(__file__).resolve().parents[1]
    script = """
import sys
import _04_Nucleo_Operativo.platform.shared.capability_registry  # noqa: F401
forbidden = (
    'sqlite3',
    'neocortex.capabilities',
    '_04_Nucleo_Operativo.archive_route',
    '_04_Nucleo_Operativo.capabilities.formats.archive.route',
    '_04_Nucleo_Operativo.audio_route',
    '_04_Nucleo_Operativo.capabilities.formats.audio.route',
    '_04_Nucleo_Operativo.docx_route',
    '_04_Nucleo_Operativo.capabilities.formats.docx.route',
    '_04_Nucleo_Operativo.image_route',
    '_04_Nucleo_Operativo.capabilities.formats.image.route',
    '_04_Nucleo_Operativo.office_route',
    '_04_Nucleo_Operativo.capabilities.formats.office.route',
    '_04_Nucleo_Operativo.video_route',
    '_04_Nucleo_Operativo.capabilities.formats.video.route',
    '_04_Nucleo_Operativo.state_topology_contracts',
)
loaded = [name for name in forbidden if name in sys.modules]
if loaded:
    raise SystemExit('unexpected imports: ' + ','.join(loaded))
"""
    completed = subprocess.run(
        [sys.executable, "-B", "-c", script],
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
