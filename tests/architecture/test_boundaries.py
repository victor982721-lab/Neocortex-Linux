"""Small, direct architectural checks independent of the product runtime."""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PRODUCT = ROOT / "neocortex"
CODE = PRODUCT / "code"


def _modules() -> tuple[tuple[str, Path], ...]:
    rows: list[tuple[str, Path]] = []
    for path in PRODUCT.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        relative = path.relative_to(ROOT).with_suffix("")
        parts = list(relative.parts)
        if parts[-1] == "__init__":
            parts.pop()
        rows.append((".".join(parts), path))
    return tuple(sorted(rows))


def _imports(module: str, path: Path) -> tuple[str, ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    values: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            values.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            try:
                values.add(importlib.util.resolve_name("." * node.level + node.module, module))
            except ImportError:
                continue
    return tuple(sorted(values))


def test_product_modules_do_not_import_development_namespaces() -> None:
    forbidden_roots = {"tests", "tools", "benchmarks"}
    for module, path in _modules():
        for imported in _imports(module, path):
            assert imported.partition(".")[0] not in forbidden_roots, (module, imported)


def test_subprocess_calls_do_not_enable_shell_execution() -> None:
    for module, path in _modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            shell = next(
                (keyword.value for keyword in node.keywords if keyword.arg == "shell"),
                None,
            )
            if isinstance(shell, ast.Constant) and shell.value is True:
                raise AssertionError(f"{module} enables shell=True")


def test_readonly_sqlite_opens_are_centralized_in_the_read_kernel() -> None:
    """Prevent a new product reader from bypassing the fenced SQLite API."""

    allowed_compatibility_seams = {
        "neocortex.persistence.sqlite_connection",  # injected factory seam
        "neocortex.knowledge.knowledge_search_inventory",  # injected legacy seam
        "neocortex.workflow.retention.planner",  # injected test module seam
        "neocortex.semantic.semantic_plan_owners",  # injected planner seam
    }
    for module, path in _modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            if not (
                isinstance(function, ast.Attribute)
                and function.attr == "connect"
                and isinstance(function.value, ast.Name)
                and function.value.id == "sqlite3"
            ):
                continue
            source = ast.get_source_segment(path.read_text(encoding="utf-8"), node) or ""
            if "mode=ro" not in source and "readonly_sqlite_uri" not in source:
                continue
            if module == "neocortex.persistence.sqlite_immutable" or module in allowed_compatibility_seams:
                continue
            raise AssertionError(
                f"{module} opens a read-only SQLite owner outside the fenced kernel"
            )


def test_code_tree_contains_only_product_capabilities() -> None:
    forbidden_fragments = (
        "analysis",
        "review",
        "experiment",
        "external_",
        "validation",
        "supply_chain",
        "unused",
        "epistemic",
        "engineering",
        "publication_diff",
    )
    paths = tuple(path.relative_to(CODE).as_posix() for path in CODE.rglob("*.py"))
    for relative in paths:
        if relative in {"code_contracts.py", "code_route.py", "code_schema.py", "code_state.py", "code_retention.py"}:
            continue
        if relative.startswith(("ingestion/", "search/", "contracts/")):
            continue
        assert not any(fragment in relative for fragment in forbidden_fragments), relative


def test_code_tree_has_no_legacy_wrappers_or_orphaned_modules() -> None:
    expected = {
        "__init__.py",
        "code_contracts.py",
        "code_retention.py",
        "code_route.py",
        "code_schema.py",
        "code_state.py",
        "code_graph_generations.py",
        "ingestion/__init__.py",
        "ingestion/code_analyzer_common.py",
        "ingestion/code_analyzers.py",
        "ingestion/code_candidate_scope.py",
        "ingestion/code_detection.py",
        "ingestion/code_generic.py",
        "ingestion/code_projects.py",
        "ingestion/code_python.py",
        "ingestion/code_rust.py",
        "search/__init__.py",
        "search/code_search.py",
        "search/code_semantic_links.py",
    }
    actual = {
        path.relative_to(CODE).as_posix()
        for path in CODE.rglob("*.py")
        if "__pycache__" not in path.parts
    }
    assert actual == expected


def test_api_core_does_not_import_interface_ui() -> None:
    for module, path in _modules():
        if not (module == "neocortex.api" or module.startswith("neocortex.api.")):
            continue
        assert all(not imported.startswith("neocortex.interface") for imported in _imports(module, path)), module


def test_production_import_graph_has_no_cycles() -> None:
    modules = dict(_modules())
    graph = {
        module: {
            imported
            for imported in _imports(module, path)
            if imported in modules
        }
        for module, path in modules.items()
    }
    index = 0
    stack: list[str] = []
    on_stack: set[str] = set()
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    components: list[tuple[str, ...]] = []

    def visit(module: str) -> None:
        nonlocal index
        indices[module] = index
        lowlinks[module] = index
        index += 1
        stack.append(module)
        on_stack.add(module)
        for imported in graph[module]:
            if imported not in indices:
                visit(imported)
                lowlinks[module] = min(lowlinks[module], lowlinks[imported])
            elif imported in on_stack:
                lowlinks[module] = min(lowlinks[module], indices[imported])
        if lowlinks[module] == indices[module]:
            component: list[str] = []
            while True:
                item = stack.pop()
                on_stack.remove(item)
                component.append(item)
                if item == module:
                    break
            if len(component) > 1:
                components.append(tuple(sorted(component)))

    for module in sorted(graph):
        if module not in indices:
            visit(module)
    assert components == []
