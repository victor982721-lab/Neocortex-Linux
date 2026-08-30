"""Canonical model lifecycle reporting and sequential preparation contracts."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from neocortex.runtime.config import model_management
from neocortex.api.cli.cli_app import main
from neocortex.interface.entrypoint import _translate_canonical_arguments, entrypoint


def _complete_report(root: Path) -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "models_report",
        "models_root": str(root),
        "all_prepared": True,
        "models": [],
    }


def test_prepare_models_runs_semantic_then_whisper_and_requires_complete_status(
    tmp_path: Path,
) -> None:
    events: list[str] = []

    def semantic(*_args: object, **kwargs: object) -> None:
        events.append("semantic")
        assert kwargs["include_compact"] is True
        assert kwargs["local_files_only"] is False

    def whisper(cache: Path) -> None:
        events.append("whisper")
        assert cache == tmp_path / "models" / "whisper"

    with patch.object(
        model_management,
        "inspect_models",
        side_effect=lambda *, models_root: _complete_report(models_root),
    ) as inspect:
        report = model_management.prepare_models(
            models_root=tmp_path / "models",
            semantic_preparer=semantic,
            whisper_preparer=whisper,
        )

    assert events == ["semantic", "whisper"]
    assert report["all_prepared"] is True
    inspect.assert_called_once_with(models_root=tmp_path / "models")


def test_models_status_is_local_read_only_and_reports_six_models(tmp_path: Path) -> None:
    root = tmp_path / "missing-models"
    with (
        patch.object(model_management, "current_platform_policy") as policy,
        patch.object(
            model_management,
            "require_local_fastembed_model",
            side_effect=model_management.SemanticModelUnavailableError("not_cached"),
        ),
        patch.object(
            model_management,
            "distribution_component",
            return_value={"status": "unavailable"},
        ),
    ):
        policy.return_value.models_directory = root
        report = model_management.inspect_models()

    assert report["all_prepared"] is False
    assert len(report["models"]) == 6
    assert not root.exists()


def test_canonical_models_facade_translates_help_and_json(
    capsys,
) -> None:
    assert _translate_canonical_arguments(("models", "prepare", "--json")) == [
        "--models-prepare",
        "--models-json",
    ]
    assert _translate_canonical_arguments(("models", "status", "--json")) == [
        "--models-status",
        "--models-json",
    ]
    assert entrypoint(("models", "status", "--help")) == 0
    assert "Neocortex models status" in capsys.readouterr().out


def test_models_status_json_exit_contract(capsys) -> None:
    report = _complete_report(Path("/models"))
    with patch(
        "neocortex.api.cli.cli_models.inspect_models",
        return_value=report,
    ):
        assert main(("--models-status", "--models-json")) == 0
    assert json.loads(capsys.readouterr().out) == report


def test_incomplete_models_status_exits_two(capsys) -> None:
    report = {**_complete_report(Path("/models")), "all_prepared": False}
    with patch(
        "neocortex.api.cli.cli_models.inspect_models",
        return_value=report,
    ):
        assert main(("--models-status", "--models-json")) == 2
    assert json.loads(capsys.readouterr().out)["all_prepared"] is False
