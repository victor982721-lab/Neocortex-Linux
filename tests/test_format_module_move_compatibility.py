"""Cross-cutting compatibility contracts for format module relocations."""

from __future__ import annotations

import importlib
import os
import pickle
import subprocess
import sys
import textwrap
from pathlib import Path
from types import CodeType, ModuleType
from typing import Any

import pytest

from _04_Nucleo_Operativo.code.contracts.target_registry import (
    COMPATIBILITY_CONTRACTS,
    COMPATIBILITY_MODULE_PAIRS,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

MODULE_MOVES = COMPATIBILITY_MODULE_PAIRS

PARENT_PACKAGES = (
    "_04_Nucleo_Operativo",
    "_04_Nucleo_Operativo.platform",
    "_04_Nucleo_Operativo.platform.shared",
    "_04_Nucleo_Operativo.capabilities",
    "_04_Nucleo_Operativo.capabilities.formats",
    "_04_Nucleo_Operativo.capabilities.formats.archive",
    "_04_Nucleo_Operativo.capabilities.formats.audio",
    "_04_Nucleo_Operativo.capabilities.formats.docx",
    "_04_Nucleo_Operativo.capabilities.formats.image",
    "_04_Nucleo_Operativo.capabilities.formats.office",
    "_04_Nucleo_Operativo.capabilities.formats.video",
)

# One locally-defined, globally addressable object for every moved module that
# owns one. ``image_policy`` intentionally owns constants only.
HISTORICAL_PICKLE_SYMBOLS = {
    "_04_Nucleo_Operativo.content_types": "DetectedType",
    "_04_Nucleo_Operativo.zip_safety": "ZipStructure",
    "_04_Nucleo_Operativo.archive_models": "ArchiveRouteSummary",
    "_04_Nucleo_Operativo.archive_route": "ArchiveRouteConfig",
    "_04_Nucleo_Operativo.archive_state": "ArchiveStatus",
    "_04_Nucleo_Operativo.archive_text_worker": "main",
    "_04_Nucleo_Operativo.audio_models": "AudioRouteSummary",
    "_04_Nucleo_Operativo.audio_probe": "probe_media",
    "_04_Nucleo_Operativo.audio_route": "AudioRoute",
    "_04_Nucleo_Operativo.audio_state": "audio_database",
    "_04_Nucleo_Operativo.audio_whisper": "WhisperTranscriber",
    "_04_Nucleo_Operativo.docx_integrity": "recover_raw_deflate_member",
    "_04_Nucleo_Operativo.docx_layout": "TextBudget",
    "_04_Nucleo_Operativo.docx_models": "DocxDiagnostic",
    "_04_Nucleo_Operativo.docx_route": "DocxRoute",
    "_04_Nucleo_Operativo.docx_schema": "validate_docx_schema",
    "_04_Nucleo_Operativo.docx_state": "connect_docx_state",
    "_04_Nucleo_Operativo.image_adult": "decide_adult_classification",
    "_04_Nucleo_Operativo.image_analysis": "classify",
    "_04_Nucleo_Operativo.image_decision": "classify",
    "_04_Nucleo_Operativo.image_decode": "RecoveredImageContentError",
    "_04_Nucleo_Operativo.image_document": "DocumentVerifierConfig",
    "_04_Nucleo_Operativo.image_errors": "ImageFailure",
    "_04_Nucleo_Operativo.image_features": "extract_features",
    "_04_Nucleo_Operativo.image_isolation": "ImageWorkerError",
    "_04_Nucleo_Operativo.image_models": "Features",
    "_04_Nucleo_Operativo.image_png": "PngProbeResult",
    "_04_Nucleo_Operativo.image_route": "ImageRouteConfig",
    "_04_Nucleo_Operativo.image_semantics": "normalize_text",
    "_04_Nucleo_Operativo.image_state": "EncodedOcrText",
    "_04_Nucleo_Operativo.image_visual": "FeatureVisualClassifier",
    "_04_Nucleo_Operativo.legacy_office_worker": "main",
    "_04_Nucleo_Operativo.office_route": "OfficeRoute",
    "_04_Nucleo_Operativo.office_state": "office_database",
    "_04_Nucleo_Operativo.video_frames": "build_frame_plan",
    "_04_Nucleo_Operativo.video_models": "VideoRouteSummary",
    "_04_Nucleo_Operativo.video_probe": "decode_video_probe",
    "_04_Nucleo_Operativo.video_route": "VideoRoute",
    "_04_Nucleo_Operativo.video_state": "video_database",
}

MONKEYPATCH_SEAMS = (
    (
        "_04_Nucleo_Operativo.content_types",
        "detect_content_type",
        "_detect_archive",
    ),
    (
        "_04_Nucleo_Operativo.zip_safety",
        "read_raw_deflate_member",
        "_decompress_raw_member",
    ),
    (
        "_04_Nucleo_Operativo.archive_route",
        "ArchiveRoute._process_container",
        "_walk_zip",
    ),
    (
        "_04_Nucleo_Operativo.archive_state",
        "initialize_archive_state",
        "archive_database",
    ),
    (
        "_04_Nucleo_Operativo.audio_probe",
        "_run_ffprobe",
        "run_bounded_capture",
    ),
    (
        "_04_Nucleo_Operativo.audio_state",
        "_initialize_locked_audio_state",
        "_migrate_audio_v1_path_collation",
    ),
    (
        "_04_Nucleo_Operativo.audio_whisper",
        "_whisper_worker",
        "resolve_whisper_runtime",
    ),
    (
        "_04_Nucleo_Operativo.docx_integrity",
        "recover_raw_deflate_member",
        "read_raw_deflate_member",
    ),
    (
        "_04_Nucleo_Operativo.docx_route",
        "DocxRoute._extract_candidate",
        "extract_docx",
    ),
    (
        "_04_Nucleo_Operativo.docx_route",
        "DocxRoute._pair_pdfs",
        "snapshot_path",
    ),
    (
        "_04_Nucleo_Operativo.docx_state",
        "initialize_docx_state",
        "connect_docx_state",
    ),
    (
        "_04_Nucleo_Operativo.image_analysis",
        "classify",
        "verify_document_text",
    ),
    (
        "_04_Nucleo_Operativo.image_document",
        "_run_document_ocr",
        "run_bounded_capture",
    ),
    (
        "_04_Nucleo_Operativo.image_features",
        "extract_features",
        "_extract_features_once",
    ),
    (
        "_04_Nucleo_Operativo.image_route",
        "ImageRoute._image_worker",
        "ImageWorkerSupervisor",
    ),
    (
        "_04_Nucleo_Operativo.legacy_office_worker",
        "_backend_command",
        "os",
    ),
    (
        "_04_Nucleo_Operativo.office_route",
        "extract_office_document",
        "_extract_xlsx_shared_strings",
    ),
    (
        "_04_Nucleo_Operativo.office_state",
        "_initialize_locked_office_state",
        "_migrate_office_v2_path_collation",
    ),
    (
        "_04_Nucleo_Operativo.video_frames",
        "sampled_video_frames.__wrapped__",
        "resolve_video_ffmpeg",
    ),
    (
        "_04_Nucleo_Operativo.video_probe",
        "_run_video_probe",
        "run_bounded_capture",
    ),
    (
        "_04_Nucleo_Operativo.video_route",
        "VideoRoute.run",
        "resolve_video_ffmpeg",
    ),
    (
        "_04_Nucleo_Operativo.video_state",
        "_initialize_locked_video_state",
        "_migrate_video_v1",
    ),
)

INSTANCE_PICKLE_CASES = (
    (
        "_04_Nucleo_Operativo.content_types",
        "DetectedType",
        ("application/x-test", ".test", frozenset({".test"}), "test"),
        {},
    ),
    (
        "_04_Nucleo_Operativo.archive_models",
        "ArchiveRouteSummary",
        (),
        {},
    ),
    (
        "_04_Nucleo_Operativo.audio_models",
        "AudioRouteSummary",
        (),
        {},
    ),
    (
        "_04_Nucleo_Operativo.docx_models",
        "DocxDiagnostic",
        (),
        {"code": "test", "message": "test", "stage": "test"},
    ),
    (
        "_04_Nucleo_Operativo.image_models",
        "SemanticLabel",
        ("test", 0.5, ("evidence",), "test"),
        {},
    ),
    (
        "_04_Nucleo_Operativo.video_models",
        "VideoRouteSummary",
        (),
        {},
    ),
    (
        "_04_Nucleo_Operativo.office_route",
        "OfficeRouteSummary",
        (),
        {},
    ),
)


def _isolated_python(script: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", "-B", "-c", script],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _resolve_attribute(module: ModuleType, dotted_name: str) -> Any:
    value: Any = module
    for component in dotted_name.split("."):
        value = getattr(value, component)
    return value


def _recursive_code_names(code: CodeType) -> frozenset[str]:
    names = set(code.co_names)
    for constant in code.co_consts:
        if isinstance(constant, CodeType):
            names.update(_recursive_code_names(constant))
    return frozenset(names)


def test_module_move_manifest_contains_all_40_pairs() -> None:
    assert len(MODULE_MOVES) == 40
    assert len(set(MODULE_MOVES)) == 40
    assert {legacy for legacy, _canonical in MODULE_MOVES} == {
        "_04_Nucleo_Operativo.content_types",
        "_04_Nucleo_Operativo.zip_safety",
        "_04_Nucleo_Operativo.archive_models",
        "_04_Nucleo_Operativo.archive_route",
        "_04_Nucleo_Operativo.archive_state",
        "_04_Nucleo_Operativo.archive_text_worker",
        "_04_Nucleo_Operativo.audio_models",
        "_04_Nucleo_Operativo.audio_probe",
        "_04_Nucleo_Operativo.audio_route",
        "_04_Nucleo_Operativo.audio_state",
        "_04_Nucleo_Operativo.audio_whisper",
        "_04_Nucleo_Operativo.docx_integrity",
        "_04_Nucleo_Operativo.docx_layout",
        "_04_Nucleo_Operativo.docx_models",
        "_04_Nucleo_Operativo.docx_route",
        "_04_Nucleo_Operativo.docx_schema",
        "_04_Nucleo_Operativo.docx_state",
        "_04_Nucleo_Operativo.legacy_office_worker",
        "_04_Nucleo_Operativo.office_route",
        "_04_Nucleo_Operativo.office_state",
        "_04_Nucleo_Operativo.video_frames",
        "_04_Nucleo_Operativo.video_models",
        "_04_Nucleo_Operativo.video_probe",
        "_04_Nucleo_Operativo.video_route",
        "_04_Nucleo_Operativo.video_state",
        *{
            f"_04_Nucleo_Operativo.image_{leaf}"
            for leaf in (
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
        },
    }


def test_compatibility_matrix_covers_pickle_patch_and_worker_contracts() -> None:
    contracts = {item.legacy_module_id: item for item in COMPATIBILITY_CONTRACTS}

    assert set(contracts) == {legacy for legacy, _canonical in MODULE_MOVES}
    assert {
        module
        for module, contract in contracts.items()
        if "historical_pickle_global" in contract.requirement_ids
    } == set(HISTORICAL_PICKLE_SYMBOLS)
    assert {
        module
        for module, contract in contracts.items()
        if "pickle_instance_roundtrip" in contract.requirement_ids
    } == {item[0] for item in INSTANCE_PICKLE_CASES}
    assert {
        module
        for module, contract in contracts.items()
        if "monkeypatch_seam" in contract.requirement_ids
    } == {item[0] for item in MONKEYPATCH_SEAMS}
    assert {
        module
        for module, contract in contracts.items()
        if "worker_module_execution" in contract.requirement_ids
    } == {
        "_04_Nucleo_Operativo.archive_text_worker",
        "_04_Nucleo_Operativo.legacy_office_worker",
    }


@pytest.mark.parametrize(
    ("legacy_name", "canonical_name"),
    MODULE_MOVES,
    ids=[legacy.rpartition(".")[2] for legacy, _canonical in MODULE_MOVES],
)
@pytest.mark.parametrize(
    "legacy_first",
    (True, False),
    ids=("legacy-first", "canonical-first"),
)
def test_cold_import_orders_preserve_exact_module_and_pickle_identity(
    legacy_name: str,
    canonical_name: str,
    legacy_first: bool,
    tmp_path: Path,
) -> None:
    first_name, second_name = (
        (legacy_name, canonical_name) if legacy_first else (canonical_name, legacy_name)
    )
    pickle_symbol = HISTORICAL_PICKLE_SYMBOLS.get(legacy_name)
    script = textwrap.dedent(
        f"""
        import importlib
        import pickle
        import sys

        sys.path.insert(0, {str(REPOSITORY_ROOT)!r})
        first = importlib.import_module({first_name!r})
        second = importlib.import_module({second_name!r})
        legacy = importlib.import_module({legacy_name!r})
        canonical = importlib.import_module({canonical_name!r})
        assert first is canonical
        assert second is canonical
        assert legacy is canonical
        assert sys.modules[{legacy_name!r}] is canonical
        assert sys.modules[{canonical_name!r}] is canonical
        symbol_name = {pickle_symbol!r}
        if symbol_name is not None:
            symbol = getattr(legacy, symbol_name)
            assert symbol is getattr(canonical, symbol_name)
            assert symbol.__module__ == {legacy_name!r}
            assert pickle.loads(pickle.dumps(symbol, protocol=5)) is symbol
        print("ok")
        """
    )

    completed = _isolated_python(script, tmp_path)

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "ok"


@pytest.mark.parametrize("package_name", PARENT_PACKAGES)
def test_parent_packages_are_import_light(package_name: str, tmp_path: Path) -> None:
    leaves = frozenset(name for pair in MODULE_MOVES for name in pair)
    script = textwrap.dedent(
        f"""
        import importlib
        import sys

        sys.path.insert(0, {str(REPOSITORY_ROOT)!r})
        leaves = {leaves!r}
        importlib.import_module({package_name!r})
        loaded = sorted(leaves.intersection(sys.modules))
        assert not loaded, loaded
        print("ok")
        """
    )

    completed = _isolated_python(script, tmp_path)

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "ok"


@pytest.mark.parametrize(
    ("legacy_name", "consumer_name", "dependency_name"),
    MONKEYPATCH_SEAMS,
    ids=[
        f"{legacy.rpartition('.')[2]}:{dependency}"
        for legacy, _consumer, dependency in MONKEYPATCH_SEAMS
    ],
)
def test_legacy_monkeypatch_updates_canonical_runtime_global(
    legacy_name: str,
    consumer_name: str,
    dependency_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = importlib.import_module(legacy_name)
    canonical_name = dict(MODULE_MOVES)[legacy_name]
    canonical = importlib.import_module(canonical_name)
    consumer = _resolve_attribute(canonical, consumer_name)
    marker = object()

    assert legacy is canonical
    assert dependency_name in _recursive_code_names(consumer.__code__)
    monkeypatch.setattr(legacy, dependency_name, marker)

    assert getattr(canonical, dependency_name) is marker
    assert consumer.__globals__[dependency_name] is marker


@pytest.mark.parametrize(
    ("legacy_name", "symbol_name", "args", "kwargs"),
    INSTANCE_PICKLE_CASES,
    ids=[
        f"{legacy.rpartition('.')[2]}:{symbol}"
        for legacy, symbol, _args, _kwargs in INSTANCE_PICKLE_CASES
    ],
)
def test_representative_instances_round_trip_through_historical_pickle_path(
    legacy_name: str,
    symbol_name: str,
    args: tuple[object, ...],
    kwargs: dict[str, object],
) -> None:
    legacy = importlib.import_module(legacy_name)
    canonical = importlib.import_module(dict(MODULE_MOVES)[legacy_name])
    instance_type = getattr(legacy, symbol_name)
    original = instance_type(*args, **kwargs)

    assert legacy is canonical
    assert instance_type is getattr(canonical, symbol_name)
    assert instance_type.__module__ == legacy_name

    restored = pickle.loads(pickle.dumps(original, protocol=5))

    assert type(restored) is instance_type
    assert restored == original


@pytest.mark.parametrize(
    ("arguments", "payload", "expected_stdout"),
    (
        (
            ("--max-input-bytes", "0", "--max-pages", "1", "--max-chars", "1"),
            b"",
            b'{"ok":false,"reason":"invalid_worker_limits"}',
        ),
        (
            ("--max-input-bytes", "1", "--max-pages", "1", "--max-chars", "1"),
            b"xx",
            b'{"ok":false,"reason":"input_limit"}',
        ),
    ),
    ids=("invalid-limits", "input-limit"),
)
def test_archive_text_worker_legacy_and_canonical_module_entrypoints_match(
    arguments: tuple[str, ...],
    payload: bytes,
    expected_stdout: bytes,
    tmp_path: Path,
) -> None:
    modules = (
        "_04_Nucleo_Operativo.archive_text_worker",
        "_04_Nucleo_Operativo.capabilities.formats.archive.text_worker",
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(REPOSITORY_ROOT)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = [
        subprocess.run(
            [sys.executable, "-B", "-m", module_name, *arguments],
            cwd=tmp_path,
            env=environment,
            check=False,
            input=payload,
            capture_output=True,
            timeout=30,
        )
        for module_name in modules
    ]

    outcomes = [(result.returncode, result.stdout, result.stderr) for result in completed]
    assert outcomes[0] == outcomes[1]
    assert outcomes[0] == (2, expected_stdout, b"")
