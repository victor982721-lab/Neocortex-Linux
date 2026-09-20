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


def _import_edges(module: str, path: Path) -> tuple[tuple[str, str, tuple[str, ...]], ...]:
    """Return explicit targets plus eager/deferred/type-checking context.

    ImportFrom is relative to __package__, not the source module. Candidate
    submodules remain in the result and are intersected with _modules by graph
    consumers; symbols do not become fictional module nodes.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package = module if path.name == "__init__.py" else module.rpartition(".")[0]
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    typing_modules = {alias.asname or alias.name for node in ast.walk(tree)
                      if isinstance(node, ast.Import) for alias in node.names if alias.name == "typing"}
    typing_checks = {alias.asname or alias.name for node in ast.walk(tree)
                     if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module == "typing"
                     for alias in node.names if alias.name == "TYPE_CHECKING"}
    values: set[tuple[str, str, tuple[str, ...]]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            targets = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                try:
                    base = importlib.util.resolve_name("." * node.level + (node.module or ""), package)
                except ImportError as exc:
                    raise AssertionError(f"invalid relative import: {module}:{node.lineno}") from exc
            else:
                base = node.module or ""
            targets = [base] if base else []
            targets.extend(f"{base}.{alias.name}" for alias in node.names if alias.name != "*")
        else:
            continue
        functions: list[str] = []
        type_checking = False
        child = node
        while child in parents:
            ancestor = parents[child]
            if isinstance(ancestor, (ast.FunctionDef, ast.AsyncFunctionDef)):
                functions.append(ancestor.name)
            if isinstance(ancestor, ast.If):
                condition = ancestor.test
                inverted = isinstance(condition, ast.UnaryOp) and isinstance(condition.op, ast.Not)
                tested = condition.operand if inverted else condition
                named = (isinstance(tested, ast.Name) and tested.id in typing_checks) or (
                    isinstance(tested, ast.Attribute) and tested.attr == "TYPE_CHECKING"
                    and isinstance(tested.value, ast.Name) and tested.value.id in typing_modules
                )
                if named and child in (ancestor.orelse if inverted else ancestor.body):
                    type_checking = True
            child = ancestor
        kind = "type_checking" if type_checking else "deferred" if functions else "eager"
        values.update((target, kind, tuple(functions)) for target in targets)
    return tuple(sorted(values))


def _imports(module: str, path: Path) -> tuple[str, ...]:
    return tuple(sorted({target for target, _kind, _context in _import_edges(module, path)}))


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






def test_code_product_tree_has_been_removed() -> None:
    assert not CODE.exists()


def test_api_core_does_not_import_interface_ui() -> None:
    for module, path in _modules():
        if not (module == "neocortex.api" or module.startswith("neocortex.api.")):
            continue
        assert all(not imported.startswith("neocortex.interface") for imported in _imports(module, path)), module


def _cyclic_components(graph: dict[str, set[str]]) -> tuple[tuple[str, ...], ...]:
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
    return tuple(sorted(components))

def test_production_import_graph_has_no_cycles() -> None:
    """Reject every eager cycle; deferred cycles have a separate exact contract."""
    modules = dict(_modules())
    graph = {
        module: {target for target, kind, _context in _import_edges(module, path)
                 if target in modules and target != module and kind == "eager"}
        for module, path in modules.items()
    }
    assert _cyclic_components(graph) == ()


def test_shared_read_contract_does_not_import_cli() -> None:
    for module in ("neocortex.api.read_api_port", "neocortex.api.status_codes"):
        path = ROOT / (module.replace(".", "/") + ".py")
        assert not any(target.startswith("neocortex.api.cli") for target in _imports(module, path))


# Exact, reviewable contracts for retained domain-level cycles. These are not
# import failures and not a generic permission for new cycles. Every edge and
# its calling function/type-checking context must still match this set.
_RETAINED_DOMAIN_CYCLES = {
    # Materialization delegates to its durable registry and no-follow primitives; rebuild does not re-enter materialization.
    frozenset({'neocortex.capabilities.formats.archive.materialization', 'neocortex.capabilities.formats.archive.rebuild'}): {
        ('neocortex.capabilities.formats.archive.materialization', 'neocortex.capabilities.formats.archive.rebuild', 'deferred', ('_apply_manifest',)),
        ('neocortex.capabilities.formats.archive.materialization', 'neocortex.capabilities.formats.archive.rebuild', 'deferred', ('_hash_file',)),
        ('neocortex.capabilities.formats.archive.materialization', 'neocortex.capabilities.formats.archive.rebuild', 'deferred', ('_publish_no_replace',)),
        ('neocortex.capabilities.formats.archive.materialization', 'neocortex.capabilities.formats.archive.rebuild', 'deferred', ('materialize_archive',)),
        ('neocortex.capabilities.formats.archive.rebuild', 'neocortex.capabilities.formats.archive.materialization', 'eager', ()),
    },
    # Five public HistoricalAuditManager methods delegate exact selected adoption to HistoricalAdoption(self). The implementation eagerly imports only the shared HistoricalAuditError and identity helper from the established owner module. Its manager reference supplies configuration/limits/root; implementation does not call back into the five public delegation methods. Private approval and effect receipts remain with the same owner.
    frozenset({'neocortex.runtime.historical_adoption', 'neocortex.runtime.historical_audit'}): {
        ('neocortex.runtime.historical_adoption', 'neocortex.runtime.historical_audit', 'eager', ()),
        ('neocortex.runtime.historical_audit', 'neocortex.runtime.historical_adoption', 'deferred', ('adoption_plan',)),
        ('neocortex.runtime.historical_audit', 'neocortex.runtime.historical_adoption', 'deferred', ('apply_selected',)),
        ('neocortex.runtime.historical_audit', 'neocortex.runtime.historical_adoption', 'deferred', ('approve_adoption',)),
        ('neocortex.runtime.historical_audit', 'neocortex.runtime.historical_adoption', 'deferred', ('plan_selected',)),
        ('neocortex.runtime.historical_audit', 'neocortex.runtime.historical_adoption', 'deferred', ('prepare_adoption',)),
    },
    frozenset({"neocortex.capabilities.formats.audio.models", "neocortex.capabilities.formats.audio.whisper"}): {
        ("neocortex.capabilities.formats.audio.models", "neocortex.capabilities.formats.audio.whisper", "deferred", ("processing_provenance",)),
        ("neocortex.capabilities.formats.audio.whisper", "neocortex.capabilities.formats.audio.models", "eager", ()),
    },  # Config asks the local model resolver for its bytes; backend consumes config contracts.
    frozenset({"neocortex.knowledge.knowledge_asset_diagnosis", "neocortex.knowledge.knowledge_asset_health_contracts"}): {
        ("neocortex.knowledge.knowledge_asset_diagnosis", "neocortex.knowledge.knowledge_asset_health_contracts", "eager", ()),
        ("neocortex.knowledge.knowledge_asset_health_contracts", "neocortex.knowledge.knowledge_asset_diagnosis", "deferred", ("to_dict",)),
    },  # Report serialization projects diagnosis through a pure builder; builder does not serialize reports.
    frozenset({"neocortex.semantic.image_retrieval_calibration", "neocortex.semantic.semantic_search_service"}): {
        ("neocortex.semantic.image_retrieval_calibration", "neocortex.semantic.semantic_search_service", "deferred", ("measure_image_retrieval_calibration",)),
        ("neocortex.semantic.semantic_search_service", "neocortex.semantic.image_retrieval_calibration", "deferred", ("image_search_ranking",)),
    },  # Measurement reuses search; normal query reads persisted calibration, never measurement.
    frozenset({"neocortex.semantic.semantic_exact_index", "neocortex.semantic.semantic_search_repository"}): {
        ("neocortex.semantic.semantic_exact_index", "neocortex.semantic.semantic_search_repository", "deferred", ("_native_rows",)),
        ("neocortex.semantic.semantic_search_repository", "neocortex.semantic.semantic_exact_index", "type_checking", ()),
        ("neocortex.semantic.semantic_search_repository", "neocortex.semantic.semantic_exact_index", "deferred", ("_search_exact_page",)),
    },  # Verified index preparation reads native rows; optional query dispatch never prepares an index.
    frozenset({"neocortex.workflow.review.archive_review_tasks", "neocortex.workflow.review.value_review_tasks"}): {
        ("neocortex.workflow.review.archive_review_tasks", "neocortex.workflow.review.value_review_tasks", "deferred", ("_refresh_archive_review_tasks",)),
        ("neocortex.workflow.review.value_review_tasks", "neocortex.workflow.review.archive_review_tasks", "type_checking", ()),
        ("neocortex.workflow.review.value_review_tasks", "neocortex.workflow.review.archive_review_tasks", "deferred", ("refresh_value_review_tasks",)),
    },  # Public refresh delegates to a shared review writer; archive reuses digest/invalidators, not refresh.
}


def test_deferred_dependency_cycles_match_explicit_contract() -> None:
    modules = dict(_modules())
    edges = { (module, target, kind, context)
              for module, path in modules.items()
              for target, kind, context in _import_edges(module, path)
              if target in modules and target != module }
    graph = {module: {target for source, target, _kind, _context in edges if source == module}
             for module in modules}
    components = {frozenset(component) for component in _cyclic_components(graph)}
    assert components == set(_RETAINED_DOMAIN_CYCLES)
    for component in components:
        assert {edge for edge in edges if edge[0] in component and edge[1] in component} == _RETAINED_DOMAIN_CYCLES[component]
