from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType

import pytest

import neocortex.api.cli.cli_app as cli_app
from neocortex.code.code_experiment_planner import experiment_template
from neocortex.code.code_invariant_contracts import (
    CALIBRATION_SCENARIO_IDS,
    EXPERIMENT_SCENARIO_IDS,
    runtime_scenario,
)
from neocortex import cli, human_cli


SCENARIO_ID = "interfaces.public_cli_and_static_surface"


def test_public_entrypoint_dispatch_precedence_covers_special_human_canonical_and_flat(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[tuple[str, tuple[str, ...]]] = []
    app_module = ModuleType("neocortex.interface.application.app")
    worker_module = ModuleType("neocortex.interface.protocol.worker")
    app_module.__dict__["main"] = lambda arguments: events.append(("ui", tuple(arguments))) or 11
    worker_module.__dict__["main"] = lambda arguments: (
        events.append(("worker", tuple(arguments))) or 12
    )
    monkeypatch.setitem(sys.modules, "neocortex.interface.application.app", app_module)
    monkeypatch.setitem(sys.modules, "neocortex.interface.protocol.worker", worker_module)
    monkeypatch.setattr(
        human_cli,
        "run_human_command",
        lambda arguments: events.append(("human", tuple(arguments))) or 13,
    )
    monkeypatch.setattr(
        cli_app,
        "main",
        lambda arguments: events.append(("flat", tuple(arguments))) or 14,
    )
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))

    assert cli.entrypoint(("--ui", "fixture-ui")) == 11
    assert cli.entrypoint(("--gui-worker", "fixture-worker")) == 12
    assert cli.entrypoint(("status", "--scope", "personal")) == 13
    assert cli.entrypoint(("doctor", "capabilities", "--json")) == 14
    assert cli.entrypoint(("--status",)) == 14
    assert events == [
        ("ui", ("fixture-ui",)),
        ("worker", ("fixture-worker",)),
        ("human", ("status", "--scope", "personal")),
        ("flat", ("--doctor-capabilities", "--doctor-capabilities-json")),
        ("flat", ("--status",)),
    ]


def _entrypoint_result(arguments: tuple[str, ...]) -> int:
    try:
        return cli.entrypoint(arguments)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2


def test_public_entrypoint_invalid_help_and_incomplete_commands_are_bounded_and_state_free(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_home = tmp_path / "state"
    data_home = tmp_path / "data"
    config_home = tmp_path / "config"
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))

    assert _entrypoint_result(("code", "validate", "--help")) == 0
    help_output = capsys.readouterr()
    assert help_output.err == ""
    assert "usage: Neocortex code validate" in help_output.out
    assert "--code-validate-change" not in help_output.out
    assert len(help_output.out.encode("utf-8")) < 32_768

    assert _entrypoint_result(("--rou", "pdf")) == 2
    abbreviated = capsys.readouterr()
    assert abbreviated.out == ""
    assert "unrecognized arguments" in abbreviated.err
    assert len(abbreviated.err.encode("utf-8")) < 65_536

    assert _entrypoint_result(("review",)) == 2
    incomplete = capsys.readouterr()
    assert incomplete.out == ""
    assert "falta una acción concreta" in incomplete.err
    assert len(incomplete.err.encode("utf-8")) < 16_384
    assert not state_home.exists()
    assert not data_home.exists()
    assert not config_home.exists()


def test_public_cli_experiment_registry_is_exact_and_non_mutating() -> None:
    scenario = runtime_scenario(SCENARIO_ID)
    template = experiment_template("interfaces.public_cli_contract_acceptance")
    manual = experiment_template("interfaces.public_contract_acceptance")

    assert scenario.version == "v4"
    assert scenario.scenario_kind == "state_fixture"
    assert scenario.isolation == "pytest_tmp_path"
    assert len(scenario.test_nodeids) == 26
    assert tuple(
        nodeid
        for nodeid in scenario.test_nodeids
        if "test_flat_observability_arguments_fail_closed" in nodeid
    ) == tuple(
        "tests/test_code_observability_cli.py::"
        f"test_flat_observability_arguments_fail_closed[{parameter_id}]"
        for parameter_id in (
            "arguments0-requires --code-question",
            "arguments1-non-empty trimmed text",
            "arguments2-between 1 and 50",
            "arguments3-require --code-storage",
            "arguments4-between 1 and 1000000",
            "arguments5-between 1 and --code-storage-run-limit",
        )
    )
    assert SCENARIO_ID in EXPERIMENT_SCENARIO_IDS
    assert SCENARIO_ID not in CALIBRATION_SCENARIO_IDS
    assert {nodeid for gate in scenario.gate_specs for nodeid in gate.test_nodeids} == set(
        scenario.test_nodeids
    )
    assert tuple(gate.gate_id for gate in scenario.gate_specs) == (
        "declared_entrypoint_and_effective_help_contract_are_observed",
        "dynamic_hidden_and_static_surfaces_remain_explicitly_non_equivalent",
        "focal_question_and_storage_reads_are_bounded_and_immutable",
        "invalid_abbreviated_and_incomplete_commands_fail_closed_without_state",
        "special_human_canonical_and_flat_dispatch_precedence_is_exact",
    )
    assert all(Path(nodeid.partition("::")[0]).is_file() for nodeid in scenario.test_nodeids)

    assert template.version == "v3"
    assert template.executable is True
    assert template.max_items == len(scenario.test_nodeids)
    assert template.scenario_ids == (SCENARIO_ID,)
    assert template.acceptance_gates == tuple(gate.gate_id for gate in scenario.gate_specs)
    assert template.applies_to(
        question_id="structure.static_cli_calls_require_runtime_contract_evidence",
        subject_key="entrypoint:neocortex-interface-surface",
    )
    assert not template.applies_to(
        question_id="structure.static_cli_calls_require_runtime_contract_evidence",
        subject_key="entrypoint:another-interface-surface",
    )
    assert template.authority == "advisory"
    assert template.mutation_authority is False

    assert manual.version == "v2"
    assert manual.executable is False
    assert "execute_public_help_and_dispatch_acceptance_scenarios" not in manual.action_ids
