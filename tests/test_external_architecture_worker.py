"""Subprocess contracts for the static architecture worker."""

from __future__ import annotations

import builtins
import hashlib
import importlib.metadata
import importlib.util
import io
import json
import os
import runpy
import stat
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
import _04_Nucleo_Operativo.external_architecture_worker as worker


def _limits(
    *,
    max_files: int = worker.DEFAULT_MAX_FILES,
    max_input_bytes: int = worker.DEFAULT_MAX_INPUT_BYTES,
    max_output_bytes: int = worker.DEFAULT_MAX_OUTPUT_BYTES,
) -> worker.WorkerLimits:
    return worker.WorkerLimits(max_files, max_input_bytes, max_output_bytes)


def _staged_file(tmp_path: Path, module: str, content: bytes = b"VALUE = 1\n") -> Any:
    path = tmp_path / f"{module.rpartition('.')[2]}.py"
    path.write_bytes(content)
    return worker.StagedPythonFile(
        path=path,
        relative_path=path.name,
        module=module,
        size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )


def _production_tree(root: Path, sentinel: Path) -> None:
    for package in worker._contracts.PRODUCTION_ROOT_PACKAGES:
        package_root = root / package
        package_root.mkdir(parents=True)
        source = ""
        if package == "neocortex":
            source = (
                "from pathlib import Path\n"
                f"Path({os.fspath(sentinel)!r}).write_text('executed', encoding='utf-8')\n"
            )
        (package_root / "__init__.py").write_text(source, encoding="utf-8")
    (root / "_04_Nucleo_Operativo" / "service.py").write_text(
        "from _05_Interfaz import view\n", encoding="utf-8"
    )
    (root / "_05_Interfaz" / "view.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "_04_Nucleo_Operativo" / "logic.py").write_text(
        """# complexipy: ignore
def tangled(values):
    if values:
        for value in values:
            if value:
                return value
    return None
""",
        encoding="utf-8",
    )


def _run_worker(root: Path, mode: str, *extra: str) -> subprocess.CompletedProcess[str]:
    assert worker.__file__ is not None
    executable = os.environ.get("NEOCORTEX_ARCHITECTURE_TEST_PYTHON", sys.executable)
    return subprocess.run(
        [
            executable,
            "-I",
            worker.__file__,
            mode,
            "--root",
            os.fspath(root),
            *extra,
        ],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )


def test_grimp_worker_is_deterministic_static_and_cacheless(tmp_path: Path) -> None:
    root = tmp_path / "project"
    sentinel = tmp_path / "content-executed.txt"
    _production_tree(root, sentinel)

    first = _run_worker(root, "grimp")
    second = _run_worker(root, "grimp")

    assert first.returncode == second.returncode == 0, first.stderr or first.stdout
    assert first.stdout == second.stdout
    assert not sentinel.exists()
    assert not (root / ".grimp_cache").exists()
    payload = json.loads(first.stdout)
    assert payload["schema"] == worker.GRIMP_WORKER_SCHEMA
    assert payload["analysis_contract"]["cache"] == "disabled"
    assert payload["counters"]["production_relations"] == 1
    assert payload["relations"][0]["details"] == [
        {"line_contents": "from _05_Interfaz import view", "line_number": 1}
    ]
    contracts = {item["contract"]["contract_id"]: item for item in payload["contract_evaluations"]}
    assert contracts["core-does-not-depend-on-ui-v1"]["status"] == "failed"
    assert contracts["core-does-not-depend-on-ui-v1"]["violations"][0]["import_chain"] == [
        "_04_Nucleo_Operativo.service",
        "_05_Interfaz.view",
    ]


def test_complexipy_worker_reports_module_function_and_line_metrics(tmp_path: Path) -> None:
    root = tmp_path / "project"
    _production_tree(root, tmp_path / "content-executed.txt")

    completed = _run_worker(root, "complexipy")

    assert completed.returncode == 0, completed.stderr or completed.stdout
    payload = json.loads(completed.stdout)
    assert payload["schema"] == worker.COMPLEXIPY_WORKER_SCHEMA
    assert payload["analysis_contract"]["check_script"] is True
    assert payload["analysis_contract"]["no_ignore"] is True
    metric = next(
        item
        for item in payload["function_metrics"]
        if item["relative_path"] == "_04_Nucleo_Operativo/logic.py" and item["symbol"] == "tangled"
    )
    assert metric["value"] > 0
    assert sum(item["complexity"] for item in metric["lines"]) == metric["value"]
    module = next(
        item for item in payload["module_metrics"] if item["module"] == "_04_Nucleo_Operativo.logic"
    )
    assert module["total"] >= module["maximum"] == metric["value"]


def test_worker_fails_with_bounded_json_when_domain_is_incomplete(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "neocortex").mkdir()
    (root / "neocortex" / "__init__.py").write_text("", encoding="utf-8")

    completed = _run_worker(root, "grimp")

    assert completed.returncode == 2
    payload = json.loads(completed.stdout)
    assert payload == {
        "error": {
            "code": "missing_production_package",
            "message": "exact production package is unavailable: _01_Enumeracion",
        },
        "schema": worker.WORKER_ERROR_SCHEMA,
        "status": "error",
    }


def test_direct_module_loader_isolated_success_and_fail_closed(tmp_path: Path) -> None:
    assert worker.__file__ is not None
    module_name = "_external_architecture_worker_direct_test"
    control_aliases = (
        "_neocortex_code_architecture_contracts",
        "_neocortex_architecture_projection",
        "_neocortex_capability_registry",
    )
    saved_modules = {name: sys.modules.get(name) for name in (module_name, *control_aliases)}
    spec = importlib.util.spec_from_file_location(module_name, worker.__file__)
    assert spec is not None and spec.loader is not None
    direct = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = direct
    try:
        spec.loader.exec_module(direct)
        assert direct.GRIMP_WORKER_SCHEMA == worker.GRIMP_WORKER_SCHEMA

        with pytest.raises(RuntimeError, match="control-plane module is unavailable"):
            direct._load_control_plane_module(
                "_unsupported_control_plane_test",
                tmp_path / "unsupported.extension",
            )
        with pytest.raises(FileNotFoundError):
            direct._load_control_plane_module(
                "_missing_control_plane_test",
                tmp_path / "missing.py",
            )
        assert "_missing_control_plane_test" not in sys.modules
    finally:
        for name, previous in saved_modules.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous
        sys.modules.pop("_unsupported_control_plane_test", None)
        sys.modules.pop("_missing_control_plane_test", None)


def test_direct_entrypoint_emits_bounded_error_in_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    assert worker.__file__ is not None
    monkeypatch.setattr(
        sys,
        "argv",
        [worker.__file__, "grimp", "--root", os.fspath(tmp_path / "missing")],
    )

    with pytest.raises(SystemExit) as captured:
        runpy.run_path(worker.__file__, run_name="__main__")

    assert captured.value.code == 2
    payload = json.loads(capfd.readouterr().out)
    assert payload["error"]["code"] == "invalid_root"


@pytest.mark.parametrize(
    ("values", "message"),
    (
        ((0, 1, 1024), "max-files"),
        ((1, 0, 1024), "max-input-bytes"),
        ((1, 1, 1023), "max-output-bytes"),
    ),
)
def test_worker_limits_reject_each_hard_boundary(
    values: tuple[int, int, int], message: str
) -> None:
    with pytest.raises(worker.WorkerContractError, match=message):
        worker.WorkerLimits(*values)


def test_worker_limits_accept_hard_maxima_and_serialize() -> None:
    limits = worker.WorkerLimits(
        worker.HARD_MAX_FILES,
        worker.HARD_MAX_INPUT_BYTES,
        worker.HARD_MAX_OUTPUT_BYTES,
    )

    assert limits.as_payload() == {
        "max_files": worker.HARD_MAX_FILES,
        "max_input_bytes": worker.HARD_MAX_INPUT_BYTES,
        "max_output_bytes": worker.HARD_MAX_OUTPUT_BYTES,
    }


def test_module_and_stable_file_guards_cover_every_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    source = root / "module.py"
    source.write_bytes(b"abc")
    link = root / "link.py"
    link.symlink_to(source)

    assert worker._inside_root(source, root)
    assert not worker._inside_root(tmp_path / "outside.py", root)
    assert worker._module_from_relative_path("package/__init__.py") == "package"
    assert worker._module_from_relative_path("package/module.py") == "package.module"
    with pytest.raises(worker.WorkerContractError, match="not Python"):
        worker._module_from_relative_path("package/data.json")
    assert worker._read_stable_file(source, expected_size=3) == b"abc"
    with pytest.raises(worker.WorkerContractError, match="changed during analysis"):
        worker._read_stable_file(source, expected_size=2)
    with pytest.raises(worker.WorkerContractError, match="not a regular file"):
        worker._read_stable_file(link)

    fake_metadata = SimpleNamespace(
        st_mode=stat.S_IFREG,
        st_size=4,
        st_mtime_ns=source.stat().st_mtime_ns,
        st_file_attributes=0,
    )
    monkeypatch.setattr(worker.os, "lstat", lambda _path: fake_metadata)
    with pytest.raises(worker.WorkerContractError, match="changed during analysis"):
        worker._read_stable_file(source)


def test_root_validation_accepts_exact_domain_and_rejects_missing_parts(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    _production_tree(root, tmp_path / "sentinel")

    assert worker._validate_root(root) == root.resolve()
    with pytest.raises(worker.WorkerContractError, match="regular directory"):
        worker._validate_root(tmp_path / "absent")

    initializer = root / worker._contracts.PRODUCTION_ROOT_PACKAGES[0] / "__init__.py"
    initializer.unlink()
    with pytest.raises(worker.WorkerContractError, match="production package"):
        worker._validate_root(root)


def test_input_collection_manifest_revalidation_and_path_cleanup(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    _production_tree(root, tmp_path / "sentinel")
    excluded = root / "neocortex" / "tests"
    excluded.mkdir()
    (excluded / "ignored.py").write_text("raise AssertionError\n", encoding="utf-8")

    inputs = worker._collect_inputs(root, _limits())

    assert inputs == tuple(sorted(inputs, key=lambda item: item.relative_path))
    assert all("/tests/" not in item.relative_path for item in inputs)
    manifest = worker._input_manifest(inputs)
    assert manifest["file_count"] == len(inputs)
    assert manifest["total_bytes"] == sum(item.size for item in inputs)
    worker._validate_inputs_unchanged(inputs)

    value = os.fspath(root)
    with worker._staged_import_path(root):
        assert sys.path[0] == value
        sys.path.remove(value)
    assert value not in sys.path

    mutable = next(item for item in inputs if item.size > 0)
    original = mutable.path.read_bytes()
    replacement = bytes(byte ^ 1 for byte in original)
    mutable.path.write_bytes(replacement)
    with pytest.raises(worker.WorkerContractError, match="changed during analysis"):
        worker._validate_inputs_unchanged((mutable,))


def test_input_collection_enforces_file_byte_escape_and_empty_bounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    _production_tree(root, tmp_path / "sentinel")

    with pytest.raises(worker.WorkerContractError, match="file bound"):
        worker._collect_inputs(root, _limits(max_files=1))
    with pytest.raises(worker.WorkerContractError, match="byte bound"):
        worker._collect_inputs(root, _limits(max_input_bytes=1))

    outside = tmp_path / "outside.py"
    outside.write_text("VALUE = 1\n", encoding="utf-8")
    (root / "_01_Enumeracion" / "escape.py").symlink_to(outside)
    with pytest.raises(worker.WorkerContractError, match="escapes staged root"):
        worker._collect_inputs(root, _limits())

    monkeypatch.setattr(worker._contracts, "PRODUCTION_ROOT_PACKAGES", ())
    with pytest.raises(worker.WorkerContractError, match="domain is empty"):
        worker._collect_inputs(root, _limits())


def test_input_collection_rejects_reparse_parent_and_negative_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    _production_tree(root, tmp_path / "sentinel")
    package = root / worker._contracts.PRODUCTION_ROOT_PACKAGES[0]

    monkeypatch.setattr(worker, "_is_reparse_point", lambda path: path == package)
    with pytest.raises(worker.WorkerContractError, match="reparse point"):
        worker._collect_inputs(root, _limits())
    monkeypatch.undo()

    target = package / "__init__.py"
    real_lstat = worker.os.lstat

    def negative_lstat(path: os.PathLike[str] | str) -> Any:
        metadata = real_lstat(path)
        if Path(path) != target:
            return metadata
        return SimpleNamespace(
            st_mode=metadata.st_mode,
            st_size=-1,
            st_mtime_ns=metadata.st_mtime_ns,
            st_file_attributes=0,
        )

    monkeypatch.setattr(worker.os, "lstat", negative_lstat)
    with pytest.raises(worker.WorkerContractError, match="invalid size"):
        worker._collect_inputs(root, _limits())


def test_tool_version_and_import_detail_normalization_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(worker.importlib.metadata, "version", lambda _name: "1.2.3")
    assert worker._tool_version("static-tool") == "1.2.3"

    monkeypatch.setattr(worker.importlib.metadata, "version", lambda _name: "")
    with pytest.raises(worker.WorkerContractError, match="version is invalid"):
        worker._tool_version("static-tool")

    def missing_distribution(_name: str) -> str:
        raise importlib.metadata.PackageNotFoundError("static-tool")

    monkeypatch.setattr(worker.importlib.metadata, "version", missing_distribution)
    with pytest.raises(worker.WorkerContractError, match="tool is unavailable"):
        worker._tool_version("static-tool")

    details = worker._normalize_import_details(
        (
            {"line_number": 0, "line_contents": "ignored"},
            {"line_number": "1", "line_contents": "ignored"},
            {"line_number": 2, "line_contents": " from  package import   value "},
            {"line_number": 2, "line_contents": "from package import value"},
        )
    )
    assert details == (worker._contracts.ImportLineDetail(2, "from package import value"),)
    assert worker._normalize_import_details(()) == ()


def test_grimp_import_queries_filter_normalize_and_preserve_external_policy() -> None:
    first = "_01_Enumeracion.first"
    second = "_01_Enumeracion.second"

    class Graph:
        @staticmethod
        def find_modules_directly_imported_by(importer: str) -> set[str]:
            if importer == first:
                return {second, "tests.helper", "external.package"}
            return set()

        @staticmethod
        def get_import_details(*, importer: str, imported: str) -> list[dict[str, object]]:
            return [
                {"line_number": -1, "line_contents": "ignored"},
                {
                    "line_number": 3,
                    "line_contents": f"from {imported} import value",
                },
            ]

    imports = worker._grimp_imports(Graph(), (first, second))

    assert [(item.importer, item.imported) for item in imports] == [
        (first, second),
        (first, "tests.helper"),
    ]
    assert all(item.details[0].line_number == 3 for item in imports)


def test_grimp_import_queries_wrap_module_and_detail_failures() -> None:
    class ModuleFailure:
        @staticmethod
        def find_modules_directly_imported_by(_importer: str) -> set[str]:
            raise RuntimeError("module query failed")

    with pytest.raises(worker.WorkerContractError, match="import query failed"):
        worker._grimp_imports(ModuleFailure(), ("_01_Enumeracion.first",))

    class DetailFailure:
        @staticmethod
        def find_modules_directly_imported_by(_importer: str) -> set[str]:
            return {"_01_Enumeracion.second"}

        @staticmethod
        def get_import_details(**_kwargs: object) -> list[dict[str, object]]:
            raise RuntimeError("detail query failed")

    with pytest.raises(worker.WorkerContractError, match="detail query failed"):
        worker._grimp_imports(DetailFailure(), ("_01_Enumeracion.first",))


def test_cycle_payloads_distinguish_real_cycles_from_acyclic_graphs() -> None:
    first = "_01_Enumeracion.first"
    second = "_01_Enumeracion.second"
    imports = (
        worker._contracts.ModuleImport(first, second),
        worker._contracts.ModuleImport(second, first),
    )

    payloads = worker._cycle_payloads((second, first), imports)

    assert len(payloads) == 1
    assert payloads[0]["modules"] == [first, second]
    assert payloads[0]["shortest_cycle_chain"] == [first, second, first]
    assert worker._cycle_payloads((first, second), imports[:1]) == ()


def test_registry_projection_guards_family_drift_none_and_overlap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    drift = SimpleNamespace(
        capabilities=(
            SimpleNamespace(
                architecture_family_id="unexpected",
                compatibility_family_id=worker.CAPABILITY_COMPATIBILITY_FAMILY,
                logical_owner_id="owner",
                modules=(),
            ),
        )
    )
    monkeypatch.setattr(worker._capability_registry, "CAPABILITY_REGISTRY", drift)
    with pytest.raises(worker.WorkerContractError, match="families drifted"):
        worker._capability_family_dag()

    no_legacy = SimpleNamespace(
        capabilities=(
            SimpleNamespace(
                architecture_family_id=worker.CAPABILITY_CANONICAL_FAMILY,
                compatibility_family_id=worker.CAPABILITY_COMPATIBILITY_FAMILY,
                logical_owner_id="owner",
                modules=(
                    SimpleNamespace(
                        canonical_module_id="canonical.module",
                        legacy_module_id=None,
                    ),
                ),
            ),
        )
    )
    monkeypatch.setattr(worker._capability_registry, "CAPABILITY_REGISTRY", no_legacy)
    canonical, legacy, owners, families = worker._registered_capability_labels()
    assert canonical == ("canonical.module",)
    assert legacy == ()
    assert owners == {"canonical.module": ("owner",)}
    assert families == {
        "canonical.module": (worker.CAPABILITY_CANONICAL_FAMILY,),
    }

    overlap = SimpleNamespace(
        capabilities=(
            SimpleNamespace(
                architecture_family_id=worker.CAPABILITY_CANONICAL_FAMILY,
                compatibility_family_id=worker.CAPABILITY_COMPATIBILITY_FAMILY,
                logical_owner_id="owner",
                modules=(
                    SimpleNamespace(
                        canonical_module_id="same.module",
                        legacy_module_id="same.module",
                    ),
                ),
            ),
        )
    )
    monkeypatch.setattr(worker._capability_registry, "CAPABILITY_REGISTRY", overlap)
    with pytest.raises(worker.WorkerContractError, match="module scopes overlap"):
        worker._registered_capability_labels()


def test_projection_payload_rejects_non_mapping_family_counters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(worker, "_projection_payload", lambda *_args, **_kwargs: {"counters": ()})

    with pytest.raises(worker.WorkerContractError, match="counters are invalid"):
        worker._capability_projection_payload((), ())


def test_projected_edge_payload_binds_canonical_witnesses() -> None:
    relation = SimpleNamespace(
        source_module="source.module",
        target_module="target.module",
        witness_ids=("module-import-v1:unit",),
    )
    edge = SimpleNamespace(
        source_label="source-family",
        target_label="target-family",
        module_relations=(relation,),
        witness_ids=relation.witness_ids,
    )

    payload = worker._projected_edge_payload("unit-family", edge)

    assert payload["edge_id"] == worker._projected_edge_id("unit-family", edge)
    assert payload["module_relations"] == [
        {
            "source_module": "source.module",
            "target_module": "target.module",
            "witness_ids": ["module-import-v1:unit"],
        }
    ]


def test_analyze_grimp_in_process_preserves_cycles_metrics_and_projections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = "worker_unit_stage"
    modules = (f"{package}.first", f"{package}.second")
    inputs = tuple(_staged_file(tmp_path, module) for module in modules)
    imports = (
        worker._contracts.ModuleImport(modules[0], modules[1]),
        worker._contracts.ModuleImport(modules[1], modules[0]),
    )
    graph = SimpleNamespace(modules=set(modules))
    grimp_module = ModuleType("grimp")
    grimp_module.build_graph = lambda *_args, **_kwargs: graph

    monkeypatch.setattr(worker._contracts, "PRODUCTION_ROOT_PACKAGES", (package,))
    monkeypatch.setattr(worker, "_collect_inputs", lambda _root, _limits: inputs)
    monkeypatch.setattr(worker, "_tool_version", lambda _name: "unit-1")
    monkeypatch.setattr(worker, "_grimp_imports", lambda _graph, _modules: imports)
    monkeypatch.setattr(
        worker._contracts,
        "evaluate_architecture_contracts",
        lambda _modules, _imports: (),
    )
    monkeypatch.setattr(worker, "_validate_inputs_unchanged", lambda _inputs: None)
    monkeypatch.setitem(sys.modules, "grimp", grimp_module)

    payload = worker.analyze_grimp(tmp_path, _limits())

    assert payload["counters"] == {
        "modules": 2,
        "production_relations": 2,
        "policy_only_external_relations": 0,
        "cyclic_components": 1,
        "contract_violations": 0,
    }
    assert payload["cycles"][0]["modules"] == list(modules)
    metrics = {item["module"]: item for item in payload["module_metrics"]}
    assert metrics[modules[0]]["fan_in"] == metrics[modules[0]]["fan_out"] == 1
    assert metrics[modules[1]]["cycle_ids"] == [payload["cycles"][0]["cycle_id"]]
    assert payload["projections"]["module_graph"]["cyclic_sccs"]


def test_analyze_grimp_rejects_loaded_build_import_and_cycle_contract_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    staged = _staged_file(tmp_path, "worker_failure_stage.module")
    monkeypatch.setattr(worker, "_collect_inputs", lambda _root, _limits: (staged,))
    monkeypatch.setattr(worker, "_tool_version", lambda _name: "unit-1")
    grimp_module = ModuleType("grimp")
    monkeypatch.setitem(sys.modules, "grimp", grimp_module)

    loaded_package = "worker_already_loaded_stage"
    monkeypatch.setattr(
        worker._contracts,
        "PRODUCTION_ROOT_PACKAGES",
        (loaded_package,),
    )
    monkeypatch.setitem(sys.modules, loaded_package, ModuleType(loaded_package))
    with pytest.raises(worker.WorkerContractError, match="execute directly"):
        worker.analyze_grimp(tmp_path, _limits())
    monkeypatch.delitem(sys.modules, loaded_package)

    build_package = "worker_build_failure_stage"
    monkeypatch.setattr(worker._contracts, "PRODUCTION_ROOT_PACKAGES", (build_package,))

    def fail_build(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("build failed")

    grimp_module.build_graph = fail_build
    with pytest.raises(worker.WorkerContractError, match="graph build failed"):
        worker.analyze_grimp(tmp_path, _limits())

    imported_package = "worker_imported_stage"
    monkeypatch.setattr(worker._contracts, "PRODUCTION_ROOT_PACKAGES", (imported_package,))

    def importing_build(*_args: object, **_kwargs: object) -> object:
        monkeypatch.setitem(sys.modules, imported_package, ModuleType(imported_package))
        return SimpleNamespace(modules={f"{imported_package}.module"})

    grimp_module.build_graph = importing_build
    with pytest.raises(worker.WorkerContractError, match="imported staged project"):
        worker.analyze_grimp(tmp_path, _limits())
    monkeypatch.delitem(sys.modules, imported_package)

    cycle_package = "worker_cycle_contract_stage"
    module = f"{cycle_package}.module"
    monkeypatch.setattr(worker._contracts, "PRODUCTION_ROOT_PACKAGES", (cycle_package,))
    grimp_module.build_graph = lambda *_args, **_kwargs: SimpleNamespace(modules={module})
    monkeypatch.setattr(worker, "_grimp_imports", lambda _graph, _modules: ())
    monkeypatch.setattr(
        worker,
        "_cycle_payloads",
        lambda _modules, _imports: ({"cycle_id": "invalid", "modules": ()},),
    )
    with pytest.raises(worker.WorkerContractError, match="cycle modules are invalid"):
        worker.analyze_grimp(tmp_path, _limits())


def test_analyze_grimp_wraps_import_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    staged = _staged_file(tmp_path, "worker_import_error.module")
    monkeypatch.setattr(worker, "_collect_inputs", lambda _root, _limits: (staged,))
    monkeypatch.setattr(worker, "_tool_version", lambda _name: "unit-1")
    real_import = builtins.__import__

    def guarded_import(
        name: str,
        globals_: Mapping[str, object] | None = None,
        locals_: Mapping[str, object] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> Any:
        if name == "grimp":
            raise ImportError("missing grimp")
        return real_import(name, globals_, locals_, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    with pytest.raises(worker.WorkerContractError, match="cannot be imported"):
        worker.analyze_grimp(tmp_path, _limits())


def test_complexity_helpers_and_analyzer_cover_success_and_contract_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    staged = _staged_file(tmp_path, "complexity_stage.module")
    function = SimpleNamespace(
        name="<module>",
        line_start=1,
        line_end=3,
        complexity=2,
        line_complexities=(
            SimpleNamespace(line=1, complexity=0),
            SimpleNamespace(line=2, complexity=2),
            SimpleNamespace(line=2, complexity=2),
        ),
    )
    result = SimpleNamespace(functions=(function,), complexity=2)
    complexipy_module = ModuleType("complexipy")
    complexipy_module.file_complexity = lambda *_args, **_kwargs: result

    monkeypatch.setattr(worker, "_collect_inputs", lambda _root, _limits: (staged,))
    monkeypatch.setattr(worker, "_tool_version", lambda _name: "unit-1")
    monkeypatch.setattr(worker, "_validate_inputs_unchanged", lambda _inputs: None)
    monkeypatch.setitem(sys.modules, "complexipy", complexipy_module)

    payload = worker.analyze_complexipy(tmp_path, _limits())

    assert payload["counters"]["cognitive_complexity_total"] == 2
    assert payload["module_metrics"][0]["maximum"] == 2
    assert payload["function_metrics"][0]["scope"] == "module_script"
    assert payload["function_metrics"][0]["lines"] == [{"line": 2, "complexity": 2}]
    assert worker._required_metric_int({"value": 3}, "value") == 3
    with pytest.raises(worker.WorkerContractError, match="metric is invalid"):
        worker._required_metric_int({"value": True}, "value")

    complexipy_module.file_complexity = lambda *_args, **_kwargs: SimpleNamespace(
        functions=(function,), complexity=3
    )
    with pytest.raises(worker.WorkerContractError, match="total does not equal"):
        worker.analyze_complexipy(tmp_path, _limits())

    def fail_complexity(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("analysis failed")

    complexipy_module.file_complexity = fail_complexity
    with pytest.raises(worker.WorkerContractError, match="could not analyze"):
        worker.analyze_complexipy(tmp_path, _limits())


def test_complexipy_analyzer_wraps_import_error_and_handles_empty_domain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(worker, "_collect_inputs", lambda _root, _limits: ())
    monkeypatch.setattr(worker, "_tool_version", lambda _name: "unit-1")
    monkeypatch.setattr(worker, "_validate_inputs_unchanged", lambda _inputs: None)
    complexipy_module = ModuleType("complexipy")
    complexipy_module.file_complexity = lambda *_args, **_kwargs: None
    monkeypatch.setitem(sys.modules, "complexipy", complexipy_module)

    payload = worker.analyze_complexipy(tmp_path, _limits())
    assert payload["module_metrics"] == []
    monkeypatch.delitem(sys.modules, "complexipy")

    real_import = builtins.__import__

    def guarded_import(
        name: str,
        globals_: Mapping[str, object] | None = None,
        locals_: Mapping[str, object] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> Any:
        if name == "complexipy":
            raise ImportError("missing complexipy")
        return real_import(name, globals_, locals_, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    with pytest.raises(worker.WorkerContractError, match="cannot be imported"):
        worker.analyze_complexipy(tmp_path, _limits())


def test_output_and_error_emission_enforce_declared_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout = SimpleNamespace(buffer=io.BytesIO())
    monkeypatch.setattr(worker.sys, "stdout", stdout)

    worker._emit({"status": "ready"}, 1024)
    assert json.loads(stdout.buffer.getvalue()) == {"status": "ready"}

    stdout.buffer = io.BytesIO()
    with pytest.raises(SystemExit) as oversized:
        worker._emit({"payload": "x" * 2048}, 1)
    assert oversized.value.code == 2
    assert json.loads(stdout.buffer.getvalue())["error"]["code"] == ("output_byte_bound_exceeded")

    stdout.buffer = io.BytesIO()
    error = worker.WorkerContractError("unit_failure", "bounded failure")
    with pytest.raises(SystemExit) as emitted:
        worker._fail(error, 1024)
    assert emitted.value.code == 2
    assert json.loads(stdout.buffer.getvalue())["error"]["code"] == "unit_failure"

    stdout.buffer = io.BytesIO()
    with pytest.raises(SystemExit) as suppressed:
        worker._fail(error, 1)
    assert suppressed.value.code == 2
    assert stdout.buffer.getvalue() == b""


def test_main_dispatches_both_in_process_modes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: list[dict[str, object]] = []
    monkeypatch.setattr(worker, "_validate_root", lambda path: path)
    monkeypatch.setattr(
        worker,
        "analyze_grimp",
        lambda _root, _limits: {"mode": "grimp"},
    )
    monkeypatch.setattr(
        worker,
        "analyze_complexipy",
        lambda _root, _limits: {"mode": "complexipy"},
    )
    monkeypatch.setattr(worker, "_emit", lambda payload, _bound: observed.append(dict(payload)))

    assert worker.main(("grimp", "--root", os.fspath(tmp_path))) == 0
    assert worker.main(("complexipy", "--root", os.fspath(tmp_path))) == 0
    assert observed == [{"mode": "grimp"}, {"mode": "complexipy"}]
