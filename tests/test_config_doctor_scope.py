"""Read-only configuration doctor and CLI-owned Knowledge context scope."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import neocortex.api.cli.cli_knowledge as cli_knowledge
from neocortex.api.cli.cli_app import main
from neocortex.api.cli.cli_parser import build_parser
from neocortex.api.cli.cli_validation import validate_arguments
from neocortex.interface.entrypoint import entrypoint


def test_config_doctor_reports_defaults_limits_and_effective_paths_without_state(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    corpus = tmp_path / "corpus"
    state = tmp_path / "state"

    assert (
        entrypoint(
            (
                "doctor",
                "config",
                "--json",
                "--root",
                str(corpus),
                "--state-directory",
                str(state),
            )
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "configuration_report"
    assert payload["read_only"] is True
    assert payload["state_access"] == "none"
    assert payload["defaults"]["knowledge"]["scope"] == "personal"
    assert payload["limits"]["knowledge_context_limit"] == {
        "minimum": 1,
        "maximum": 100,
    }
    assert payload["effective_paths"] == {
        "corpus": str(corpus),
        "state": str(state),
    }
    assert payload["capabilities"]["mutation"]["available"] is True
    assert not corpus.exists()
    assert not state.exists()


def test_config_doctor_never_echoes_secret_environment_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "config-doctor-secret-sentinel"
    monkeypatch.setenv("NEOCORTEX_API_KEY", secret)
    monkeypatch.setenv("NEOCORTEX_CORPUS_ROOT", "/tmp/corpus-override")

    from neocortex.api.cli.cli_config_doctor import configuration_report

    serialized = json.dumps(configuration_report(), ensure_ascii=False)
    assert secret not in serialized
    assert "NEOCORTEX_API_KEY" not in serialized
    assert "NEOCORTEX_CORPUS_ROOT" in serialized
    assert "configured" in serialized


def test_config_doctor_rejects_mutation_and_missing_json_owner(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as raised:
        main(("--doctor-config", "--apply"))
    assert raised.value.code == 2
    assert "doctor config is read-only" in capsys.readouterr().err

    with pytest.raises(SystemExit) as raised:
        main(("--doctor-config-json",))
    assert raised.value.code == 2
    assert "requires --doctor-config" in capsys.readouterr().err


def test_knowledge_context_scope_defaults_to_personal_and_is_cli_only() -> None:
    parser = build_parser()
    args = parser.parse_args(("--knowledge-context", "relay"))
    validate_arguments(args)
    assert args.knowledge_scope == "personal"

    selected = parser.parse_args(
        ("--knowledge-context", "relay", "--scope", "framework")
    )
    validate_arguments(selected)
    assert selected.knowledge_scope == "framework"


def test_knowledge_context_scope_requires_context() -> None:
    args = build_parser().parse_args(("--scope", "all"))
    with pytest.raises(SystemExit, match="--scope requires --knowledge-context"):
        validate_arguments(args)


def test_knowledge_context_v2_forwards_the_selected_scope(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    observed: dict[str, object] = {}

    class Result:
        blocking_owners = ()
        snapshot = type("Snapshot", (), {"to_dict": lambda self: {}})()
        complete = True
        hits = ()

    class Service:
        paths = type("Paths", (), {"semantic": Path("/tmp/semantic.sqlite3")})()

        def search(self, *_args: object, **_kwargs: object) -> Result:
            return Result()

    def hydration(service: object, query: object, *, scope: str, **_kwargs: object):
        observed["scope"] = scope
        return Result(), {
            "snapshot": {"owners": []},
            "hits": [],
            "complete": True,
            "blocking_owners": [],
            "rankings": [],
            "truncated": False,
        }

    monkeypatch.setattr(cli_knowledge, "_service", lambda _args: Service())
    monkeypatch.setattr(
        cli_knowledge,
        "_with_cancellation",
        lambda operation: operation(lambda: None),
    )
    monkeypatch.setattr(
        cli_knowledge,
        "knowledge_search_exit_code",
        lambda _result: cli_knowledge.KnowledgeExitCode.SUCCESS,
    )
    monkeypatch.setattr(
        "neocortex.knowledge.knowledge_context_hydration.search_context_evidence",
        hydration,
    )

    args = build_parser().parse_args(
        ("--knowledge-context", "relay", "--scope", "all", "--knowledge-json")
    )
    validate_arguments(args)
    assert cli_knowledge.run_knowledge_context(args) == 3
    payload = json.loads(capsys.readouterr().out)
    assert observed["scope"] == "personal"
    assert payload["scope"] == "all"


def test_cli_scope_does_not_change_public_context_default() -> None:
    from neocortex.api.read_api import context_payload

    assert context_payload.__kwdefaults__["response_version"] == 1
    assert context_payload.__defaults__ is not None
    assert context_payload.__defaults__[0] == "all"
