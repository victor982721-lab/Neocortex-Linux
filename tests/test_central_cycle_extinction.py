"""Characterization gates for the final production import-cycle cut."""
# region [00] Contexto del módulo
# Módulo: tests/test_central_cycle_extinction.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

import ast
import json
import os
import pickle
import subprocess
import sys
from dataclasses import fields
from pathlib import Path

import pytest

import _04_Nucleo_Operativo as public_api
from _04_Nucleo_Operativo import (
    framework_state_writer,
    models,
    pdf_route,
    pdf_route_models,
    self_analysis,
)
# endregion [01]

# region [02] Implementación

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "neocortex"
SELF_ANALYSIS_MODULE = "neocortex.workflow.self_analysis.self_analysis"
STATE_WRITER_MODULE = "neocortex.persistence.framework_state_writer"
MODELS_MODULE = "neocortex.runtime.models"
PDF_ROUTE_MODULE = "neocortex.capabilities.formats.pdf.pdf_route"
PDF_MODELS_MODULE = "neocortex.capabilities.formats.pdf.pdf_route_models"
LEGACY_MODELS_MODULE = "_04_Nucleo_Operativo.models"
LEGACY_PDF_MODELS_MODULE = "_04_Nucleo_Operativo.pdf_route_models"
PDF_MODELS_PRODUCT_MODULE = "neocortex.capabilities.formats.pdf.pdf_route_models"

SELF_ANALYSIS_CONFIG_FIELDS = {
    "analysis_profile",
    "code_cache_validation",
    "code_chunk_chars",
    "code_complexity_warning",
    "code_function_lines_warning",
    "code_max_documents",
    "code_max_file_bytes",
    "code_max_text_chars",
    "code_retry_errors",
    "deep_max_tests",
    "deep_mutation_max_mutants",
    "deep_mutation_symbol",
    "deep_mutation_target",
    "deep_mutation_time_budget_seconds",
    "deep_mutation_timeout_seconds",
    "deep_shard_size",
    "deep_test_selectors",
    "deep_time_budget_seconds",
}
ACTION_SUMMARY_FIELDS = (
    "apply_actions",
    "duplicate_candidates",
    "duplicates_trashed",
    "duplicate_skips",
    "files_checked",
    "types_detected",
    "extensions_matching",
    "unknown_types",
    "type_cache_hits",
    "type_cache_misses",
    "type_cache_pruned",
    "stale_inventory",
    "rename_candidates",
    "files_renamed",
    "rename_skips",
    "empty_directory_candidates",
    "empty_directories_trashed",
    "empty_directory_skips",
    "errors",
)


def _source_tree(module_name: str) -> ast.Module:
    path = PROJECT_ROOT.joinpath(*module_name.split(".")).with_suffix(".py")
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _module_imports(module_name: str) -> set[str]:
    package = module_name.rpartition(".")[0]
    imported: set[str] = set()
    for node in ast.walk(_source_tree(module_name)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.module is None:
                imported.update(f"{package}.{alias.name}" for alias in node.names)
            elif node.module is not None:
                imported.add(f"{package}.{node.module}" if node.level else node.module)
    return imported


def _type_checking_protocol(module_name: str, class_name: str) -> ast.ClassDef:
    block = next(
        node
        for node in _source_tree(module_name).body
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Name)
        and node.test.id == "TYPE_CHECKING"
    )
    contract = next(
        node for node in block.body if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    assert any(isinstance(base, ast.Name) and base.id == "Protocol" for base in contract.bases)
    return contract


def _protocol_properties(contract: ast.ClassDef) -> set[str]:
    return {
        node.name
        for node in contract.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(
            isinstance(decorator, ast.Name) and decorator.id == "property"
            for decorator in node.decorator_list
        )
    }


def test_public_contract_owners_reexports_and_signatures_are_unchanged() -> None:
    assert models.FrameworkConfig.__module__ == LEGACY_MODELS_MODULE
    assert models.ActionSummary.__module__ == LEGACY_MODELS_MODULE
    assert pdf_route_models.PdfRouteSummary.__module__ == LEGACY_PDF_MODELS_MODULE
    assert pdf_route.PdfRouteSummary is pdf_route_models.PdfRouteSummary
    assert public_api.FrameworkConfig is models.FrameworkConfig
    assert public_api.ActionSummary is models.ActionSummary
    assert public_api.PdfRouteSummary is pdf_route_models.PdfRouteSummary

    assert self_analysis.self_analysis_commands.__annotations__ == {
        "config": "FrameworkConfig",
        "root": "Path",
        "state_directory": "Path",
        "return": "dict[str, list[str]]",
    }
    assert framework_state_writer.FrameworkState.store_action_summary.__annotations__ == {
        "run_id": "int",
        "summary": "ActionSummary",
        "return": "None",
    }
    assert tuple(field.name for field in fields(models.ActionSummary)) == ACTION_SUMMARY_FIELDS
    assert str(
        next(field for field in fields(models.InitialRunResult) if field.name == "pdf").type
    ) == ("PdfRouteSummary | None")

    values = (
        models.FrameworkConfig(),
        models.ActionSummary(False),
        pdf_route_models.PdfRouteSummary(),
    )
    for value in values:
        restored = pickle.loads(pickle.dumps(value))
        assert type(restored) is type(value)
        assert restored == value


def test_static_contract_ports_replace_only_the_three_feedback_edges() -> None:
    self_imports = _module_imports(SELF_ANALYSIS_MODULE)
    writer_imports = _module_imports(STATE_WRITER_MODULE)
    model_imports = _module_imports(MODELS_MODULE)

    assert MODELS_MODULE not in self_imports
    assert MODELS_MODULE not in writer_imports
    assert PDF_ROUTE_MODULE not in model_imports
    assert PDF_MODELS_PRODUCT_MODULE in model_imports

    config_port = _type_checking_protocol(SELF_ANALYSIS_MODULE, "FrameworkConfig")
    summary_port = _type_checking_protocol(STATE_WRITER_MODULE, "ActionSummary")
    assert _protocol_properties(config_port) == SELF_ANALYSIS_CONFIG_FIELDS
    assert _protocol_properties(summary_port) == set(ACTION_SUMMARY_FIELDS)


@pytest.mark.parametrize(
    "module_order",
    (
        (SELF_ANALYSIS_MODULE, STATE_WRITER_MODULE, MODELS_MODULE, PDF_ROUTE_MODULE),
        (STATE_WRITER_MODULE, SELF_ANALYSIS_MODULE, PDF_ROUTE_MODULE, MODELS_MODULE),
        (MODELS_MODULE, PDF_ROUTE_MODULE, SELF_ANALYSIS_MODULE, STATE_WRITER_MODULE),
    ),
)
def test_central_modules_support_cold_import_orders(
    module_order: tuple[str, ...],
) -> None:
    script = f"""
import importlib
import sys

sys.path.insert(0, {os.fspath(PROJECT_ROOT)!r})
for module_name in {module_order!r}:
    importlib.import_module(module_name)
models = importlib.import_module({MODELS_MODULE!r})
pdf_route = importlib.import_module({PDF_ROUTE_MODULE!r})
pdf_models = importlib.import_module({PDF_MODELS_MODULE!r})
assert models.FrameworkConfig.__module__ == {LEGACY_MODELS_MODULE!r}
assert models.ActionSummary.__module__ == {LEGACY_MODELS_MODULE!r}
assert pdf_route.PdfRouteSummary is pdf_models.PdfRouteSummary
print("ok")
"""
    completed = subprocess.run(
        [sys.executable, "-I", "-B", "-c", script],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "ok"


def test_live_grimp_graph_has_no_production_cycles() -> None:
    worker = PROJECT_ROOT / "neocortex" / "code" / "external_architecture_worker.py"
    executable = os.environ.get("NEOCORTEX_ARCHITECTURE_TEST_PYTHON", sys.executable)
    completed = subprocess.run(
        [
            executable,
            "-I",
            os.fspath(worker),
            "grimp",
            "--root",
            os.fspath(PROJECT_ROOT),
        ],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    payload = json.loads(completed.stdout)
    cycle_evaluation = next(
        item
        for item in payload["contract_evaluations"]
        if item["contract"]["contract_id"] == "no-new-production-import-cycles-v1"
    )
    assert payload["status"] == "ready"
    assert payload["counters"]["contract_violations"] == 0
    assert payload["counters"]["cyclic_components"] == 0
    assert payload["cycles"] == []
    assert cycle_evaluation["observed_count"] == 0
    assert cycle_evaluation["status"] == "passed"


# endregion [02]
