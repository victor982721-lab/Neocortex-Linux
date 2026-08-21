"""Stable packaged implementation identity for the Text extraction stage."""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import sqlite3
from importlib import metadata
from pathlib import Path

import pytest

import _04_Nucleo_Operativo.text_route as text_route_module
import neocortex
from _02_Deduplicacion import FileSnapshot, snapshot_path
from _04_Nucleo_Operativo.cancellation import CancellationToken
from _04_Nucleo_Operativo.route_filters import CandidateSelection
from _04_Nucleo_Operativo.text_route import TextRoute, TextRouteConfig


PROJECT_ROOT = Path(__file__).resolve().parents[1]
_EXTRACTOR_ROOTS = {
    "_04_Nucleo_Operativo/text_route.py": ("_extract", "_extractor_selector"),
    "_04_Nucleo_Operativo/capabilities/formats/office/legacy_worker.py": ("main",),
}


class _FrameworkState:
    def __init__(self, snapshot: FileSnapshot) -> None:
        self.snapshot = snapshot

    def selected_route_candidate_counts(
        self,
        _run_id: int,
        mime: str,
        _max_file_bytes: int | None,
        _route_name: str,
        _selection: CandidateSelection,
    ) -> tuple[int, int]:
        return (1, 1) if mime == "text/plain" else (0, 0)

    def iter_selected_route_candidates(
        self,
        _run_id: int,
        mime: str,
        _route_name: str,
        _selection: CandidateSelection,
    ):
        if mime == "text/plain":
            yield self.snapshot


class _ContractNormalizer(ast.NodeTransformer):
    """Discard formatting, comments, docstrings and function type hints."""

    @staticmethod
    def _without_docstring(body: list[ast.stmt]) -> list[ast.stmt]:
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            return body[1:]
        return body

    def visit_arg(self, node: ast.arg) -> ast.arg:
        node = copy.deepcopy(node)
        node.annotation = None
        node.type_comment = None
        return node

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.FunctionDef:
        node = copy.deepcopy(node)
        self.generic_visit(node)
        node.returns = None
        node.type_comment = None
        node.body = self._without_docstring(node.body)
        return node

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AsyncFunctionDef:
        node = copy.deepcopy(node)
        self.generic_visit(node)
        node.returns = None
        node.type_comment = None
        node.body = self._without_docstring(node.body)
        return node

    def visit_ClassDef(self, node: ast.ClassDef) -> ast.ClassDef:
        node = copy.deepcopy(node)
        self.generic_visit(node)
        node.body = self._without_docstring(node.body)
        return node


def _top_level_definitions(tree: ast.Module) -> dict[str, ast.AST]:
    definitions: dict[str, ast.AST] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            definitions[node.name] = node
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    definitions[target.id] = node
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            definitions[node.target.id] = node
    return definitions


def _normalized_extractor_contract(sources: dict[str, str]) -> str:
    manifest: dict[str, dict[str, str]] = {}
    normalizer = _ContractNormalizer()
    for relative_path, roots in sorted(_EXTRACTOR_ROOTS.items()):
        tree = ast.parse(sources[relative_path], filename=relative_path)
        definitions = _top_level_definitions(tree)
        pending = list(roots)
        selected: set[str] = set()
        while pending:
            name = pending.pop()
            if name in selected:
                continue
            node = definitions[name]
            selected.add(name)
            normalized = normalizer.visit(copy.deepcopy(node))
            local_dependencies = {
                item.id
                for item in ast.walk(normalized)
                if isinstance(item, ast.Name)
                and isinstance(item.ctx, ast.Load)
                and item.id in definitions
            }
            pending.extend(sorted(local_dependencies - selected))
        manifest[relative_path] = {
            name: ast.dump(
                normalizer.visit(copy.deepcopy(definitions[name])),
                annotate_fields=True,
                include_attributes=False,
            )
            for name in sorted(selected)
        }
    encoded = json.dumps(
        manifest,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _sources_at(root: Path) -> dict[str, str]:
    return {
        relative_path: (root / relative_path).read_text(encoding="utf-8")
        for relative_path in _EXTRACTOR_ROOTS
    }


def _installed_distribution_sources() -> tuple[metadata.Distribution, dict[str, str]]:
    distribution = metadata.distribution("neocortex-framework")
    files = {str(item).replace("\\", "/"): item for item in distribution.files or ()}
    sources = {
        relative_path: Path(distribution.locate_file(files[relative_path])).read_text(
            encoding="utf-8"
        )
        for relative_path in _EXTRACTOR_ROOTS
    }
    return distribution, sources


def test_text_receipt_links_stage_to_effective_packaged_implementation(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.txt"
    source.write_text("stable Text implementation identity", encoding="utf-8")
    state = tmp_path / "text.sqlite3"
    route = TextRoute(
        TextRouteConfig(state_path=state),
        _FrameworkState(snapshot_path(source)),
        1,
        cancellation=CancellationToken(),
    )

    summary = route.run()

    assert summary.extracted == 1
    with sqlite3.connect(state) as connection:
        receipt = json.loads(
            connection.execute("SELECT receipt_json FROM text_work_receipts").fetchone()[0]
        )
    configuration = receipt["effective_configuration"]
    assert receipt["stage"]["implementation_digest"] == configuration["implementation_digest"]
    assert configuration["implementation_distribution"] == "neocortex-framework"
    assert configuration["implementation_distribution_version"] == neocortex.__version__
    assert configuration["implementation_digest"].startswith("sha256:")


@pytest.mark.parametrize(
    ("attribute", "replacement"),
    (
        ("_NEOCORTEX_DISTRIBUTION_VERSION", "0.9.0+build-two"),
        ("_TEXT_EXTRACTOR_CONTRACT_SHA256", f"sha256:{'f' * 64}"),
    ),
)
def test_text_processing_signature_changes_with_packaged_implementation_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    attribute: str,
    replacement: str,
) -> None:
    config = TextRouteConfig(state_path=tmp_path / "text.sqlite3")
    baseline = config.processing_provenance
    baseline_stage = text_route_module._stage_descriptor(baseline)

    monkeypatch.setattr(text_route_module, attribute, replacement)
    changed = config.processing_provenance
    changed_stage = text_route_module._stage_descriptor(changed)

    assert changed.signature != baseline.signature
    assert changed_stage.implementation_digest != baseline_stage.implementation_digest
    assert (
        baseline_stage.implementation_digest
        == baseline.manifest["configuration"]["implementation_digest"]
    )
    assert (
        changed_stage.implementation_digest
        == changed.manifest["configuration"]["implementation_digest"]
    )


def test_text_extractor_contract_and_identity_are_stable_in_source_and_distribution() -> None:
    source_digest = _normalized_extractor_contract(_sources_at(PROJECT_ROOT))
    assert source_digest == text_route_module._TEXT_EXTRACTOR_CONTRACT_SHA256

    try:
        distribution, installed_sources = _installed_distribution_sources()
    except KeyError:
        pytest.skip("installed release predates the candidate extractor layout")
    installed_digest = _normalized_extractor_contract(installed_sources)
    assert installed_digest == source_digest
    assert distribution.version == neocortex.__version__
    assert (
        text_route_module._text_implementation_configuration(
            distribution_version=distribution.version,
            extractor_contract_sha256=installed_digest,
        )
        == text_route_module._text_implementation_configuration()
    )


def test_text_extractor_contract_ignores_formatting_but_detects_behavior_changes() -> None:
    sources = _sources_at(PROJECT_ROOT)
    baseline = _normalized_extractor_contract(sources)
    reformatted = {
        path: f"# irrelevant comment\n{ast.unparse(ast.parse(source, filename=path))}\n"
        for path, source in sources.items()
    }
    changed = dict(sources)
    changed["_04_Nucleo_Operativo/text_route.py"] = changed[
        "_04_Nucleo_Operativo/text_route.py"
    ].replace('("utf-8-sig", "cp1252")', '("utf-8-sig", "latin-1")', 1)

    assert _normalized_extractor_contract(reformatted) == baseline
    assert _normalized_extractor_contract(changed) != baseline
