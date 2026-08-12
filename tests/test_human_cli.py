from __future__ import annotations

import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from neocortex import cli, human_cli


def _hit() -> dict[str, object]:
    return {
        "rank": 1,
        "resource": {
            "current_path": "/corpus/Transformador.pdf",
            "resource_id": "resource:1",
        },
        "evidence": {
            "evidence_id": "evidence:1",
            "page": 7,
            "snippet": "Prueba TTR del transformador de potencia.",
        },
        "reasons": ["fts_pdf returned this concrete evidence"],
    }


@pytest.mark.parametrize(
    ("arguments", "runner", "expected"),
    (
        (
            ("review", "task", "show", "task-1", "--json"),
            "run_review_task_show",
            {"task_id": "task-1", "scope": "personal", "json_output": True},
        ),
        (
            ("review", "task", "history", "task-1", "--scope", "framework"),
            "run_review_task_history",
            {"task_id": "task-1", "scope": "framework", "json_output": False},
        ),
        (
            (
                "review",
                "task",
                "claim",
                "task-1",
                "--expected-event-id",
                "event-1",
                "--actor",
                "victor",
                "--note",
                "inicio",
            ),
            "run_review_task_claim",
            {
                "task_id": "task-1",
                "scope": "personal",
                "expected_event_id": "event-1",
                "actor": "victor",
                "note": "inicio",
                "json_output": False,
            },
        ),
        (
            (
                "review",
                "task",
                "decide",
                "task-1",
                "--expected-event-id",
                "event-2",
                "--decision",
                "dismissed",
                "--decision-scope",
                "permanent",
                "--actor",
                "victor",
                "--json",
            ),
            "run_review_task_decide",
            {
                "task_id": "task-1",
                "scope": "personal",
                "expected_event_id": "event-2",
                "decision": "dismissed",
                "decision_scope": "permanent",
                "actor": "victor",
                "note": None,
                "json_output": True,
            },
        ),
    ),
)
def test_human_review_task_commands_use_one_shared_adapter(
    monkeypatch: pytest.MonkeyPatch,
    arguments: tuple[str, ...],
    runner: str,
    expected: dict[str, object],
) -> None:
    calls: list[dict[str, object]] = []
    fake = SimpleNamespace(**{runner: lambda **kwargs: calls.append(kwargs) or 0})
    original_import = human_cli.importlib.import_module
    monkeypatch.setattr(
        human_cli.importlib,
        "import_module",
        lambda name: fake if name == "neocortex.review_task_cli_adapter" else original_import(name),
    )

    assert human_cli.run_human_command(arguments) == 0
    assert calls == [expected]


def test_human_review_task_rejects_all_scope_without_loading_adapter(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    loaded = False

    def forbidden_import(_name: str):
        nonlocal loaded
        loaded = True
        raise AssertionError("adapter must not load for an invalid scope")

    monkeypatch.setattr(human_cli.importlib, "import_module", forbidden_import)
    assert human_cli.run_human_command(("review", "task", "show", "task-1", "--scope", "all")) == 2
    assert not loaded
    assert "personal o framework" in capsys.readouterr().err


def test_human_search_explains_path_locator_and_non_authority(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        human_cli,
        "search_payload",
        lambda *_args, **_kwargs: {
            "query": "transformador",
            "scope_requested": "personal",
            "exit_code": 0,
            "scopes": [
                {
                    "scope": "personal",
                    "result": {
                        "complete": True,
                        "result_window_full": False,
                        "hits": [_hit()],
                    },
                }
            ],
        },
    )

    assert human_cli.run_human_command(("search", "transformador")) == 0
    output = capsys.readouterr().out
    assert "Transformador.pdf · página 7" in output
    assert "Prueba TTR" in output
    assert "no son probabilidades ni autorizan acciones" in output


def test_human_ask_labels_citations_without_fabricating_an_answer(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        human_cli,
        "context_payload",
        lambda *_args, **_kwargs: {
            "query": "TTR",
            "exit_code": 0,
            "scopes": [
                {
                    "scope": "personal",
                    "context": {
                        "completeness": "complete",
                        "selected_hits": [_hit()],
                        "citation_ids": [{"citation_id": "K1", "evidence_id": "evidence:1"}],
                    },
                }
            ],
        },
    )

    assert human_cli.run_human_command(("ask", "TTR")) == 0
    output = capsys.readouterr().out
    assert "[K1] Transformador.pdf" in output
    assert "no inventa una respuesta" in output


def test_human_status_json_is_one_machine_readable_document(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = {
        "kind": "neocortex_scoped_status",
        "exit_code": 0,
        "scopes": [],
    }
    monkeypatch.setattr(human_cli, "status_payload", lambda _scope: payload)

    assert human_cli.run_human_command(("status", "--json")) == 0
    assert json.loads(capsys.readouterr().out) == payload


def test_human_lineage_defaults_to_personal_and_explains_text_and_semantic(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen: list[tuple[str, str]] = []

    def fake_lineage(identifier: str, scope: str) -> dict[str, object]:
        seen.append((identifier, scope))
        return {
            "identifier": identifier,
            "exit_code": 4,
            "scopes": [
                {
                    "scope": "personal",
                    "status": "partial",
                    "exit_code": 4,
                    "lineage": {
                        "status": "partial",
                        "complete": False,
                        "text": {
                            "lineage": {
                                "file_key": "text:file:1",
                                "path": "/corpus/informe.txt",
                                "document_status": "ok",
                                "attribution": "recorded",
                                "revision": {
                                    "revision_id": "revision:text:1",
                                    "resource_id": "resource:text:1",
                                },
                                "materializations": [
                                    {
                                        "name": "structured_text",
                                        "materialization": {
                                            "materialization_id": "materialization:text:1"
                                        },
                                        "current_head": True,
                                    }
                                ],
                            },
                            "receipt_count": 2,
                            "current_materialization_heads": 1,
                            "dependencies": [{"receipt_id": "receipt:semantic:1"}],
                        },
                        "semantic": {
                            "lineage": {
                                "chunk_id": "chunk:1",
                                "lineage_status": "recorded",
                                "chunking_signature": "chunker:v1",
                                "origins": ({"source_kind": "text"},),
                                "embeddings": (
                                    {
                                        "generation_id": 7,
                                        "model_id": "local-model",
                                        "published": True,
                                        "lineage_status": "recorded",
                                    },
                                ),
                            }
                        },
                        "semantic_dependents": {
                            "revision_id": "revision:text:1",
                            "chunk_ids": ["chunk:1"],
                            "chunk_count_in_window": 1,
                            "truncated": False,
                        },
                        "warnings": ["text:receipt_window_truncated"],
                    },
                }
            ],
        }

    monkeypatch.setattr(human_cli, "lineage_payload", fake_lineage)

    assert human_cli.run_human_command(("inspect", "lineage", "chunk:1")) == 4
    assert seen == [("chunk:1", "personal")]
    output = capsys.readouterr().out
    assert "Text: /corpus/informe.txt" in output
    assert "Revisión: revision:text:1" in output
    assert "Receipts: 2" in output
    assert "Semantic: chunk chunk:1" in output
    assert "Semantic dependiente: 1 chunk" in output
    assert "generación 7 · modelo local-model · publicado" in output
    assert "Advertencias: text:receipt_window_truncated" in output
    assert output.rstrip().endswith("No se creó, migró ni modificó estado.")


def test_human_lineage_json_is_one_machine_readable_document(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = {
        "kind": "neocortex_scoped_derivation_lineage",
        "identifier": "revision:text:1",
        "exit_code": 3,
        "scopes": [],
    }
    seen: list[tuple[str, str]] = []

    def fake_lineage(identifier: str, scope: str) -> dict[str, object]:
        seen.append((identifier, scope))
        return payload

    monkeypatch.setattr(human_cli, "lineage_payload", fake_lineage)

    assert (
        human_cli.run_human_command(
            ("inspect", "lineage", "revision:text:1", "--scope", "framework", "--json")
        )
        == 3
    )
    assert seen == [("revision:text:1", "framework")]
    assert json.loads(capsys.readouterr().out) == payload


def test_human_lineage_distinguishes_not_found_from_scope_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        human_cli,
        "lineage_payload",
        lambda *_args: {
            "identifier": "missing",
            "exit_code": 3,
            "scopes": [
                {
                    "scope": "personal",
                    "status": "not_found",
                    "exit_code": 3,
                    "lineage": {
                        "status": "not_found",
                        "complete": False,
                        "text": None,
                        "semantic": None,
                        "warnings": [],
                    },
                },
                {
                    "scope": "framework",
                    "status": "error",
                    "exit_code": 5,
                    "error_type": "OperationalError",
                    "reason": "database is locked",
                },
            ],
        },
    )

    assert human_cli.run_human_command(("inspect", "lineage", "missing", "--scope", "all")) == 3
    output = capsys.readouterr().out
    assert "Personal: no se encontró ese identificador" in output
    assert "Framework: no se pudo consultar (OperationalError: database is locked)" in output
    assert output.rstrip().endswith("No se creó, migró ni modificó estado.")


def test_value_refresh_rejects_federated_scope_as_stable_usage_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert human_cli.run_human_command(("review", "value", "--scope", "all", "--refresh")) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "requiere --scope personal o framework" in captured.err
    assert "no se modificó estado" in captured.err


def test_installed_entrypoint_dispatches_human_commands_before_flat_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, ...]] = []
    monkeypatch.setattr(human_cli, "run_human_command", lambda args: seen.append(tuple(args)) or 17)

    assert cli.entrypoint(("status", "--scope", "personal")) == 17
    assert seen == [("status", "--scope", "personal")]


def test_help_lists_useful_facade_and_legacy_compatibility(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert human_cli.run_human_command(("help",)) == 0
    output = capsys.readouterr().out
    assert "status" in output
    assert "search" in output
    assert "ask" in output
    assert "flags siguen disponibles" in " ".join(output.split())


def test_value_help_does_not_load_review_task_storage() -> None:
    script = """
import contextlib
import io
import sys
from neocortex.cli import entrypoint

output = io.StringIO()
try:
    with contextlib.redirect_stdout(output):
        code = entrypoint(("review", "value", "--help"))
except SystemExit as error:
    code = error.code
forbidden = {
    "_04_Nucleo_Operativo.review_task_contracts",
    "_04_Nucleo_Operativo.review_task_repository",
    "_04_Nucleo_Operativo.value_review_repository",
    "_04_Nucleo_Operativo.value_review_tasks",
    "torch",
    "transformers",
    "sentence_transformers",
}
loaded = sorted(forbidden.intersection(sys.modules))
if code != 0 or loaded:
    raise SystemExit(f"help loaded operational state: code={code}, modules={loaded}")
"""
    completed = subprocess.run(
        (sys.executable, "-c", script),
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize("arguments", [("inspect",), ("review",), ("agent",)])
def test_nested_commands_require_a_concrete_read_only_action(arguments) -> None:
    with pytest.raises(SystemExit) as raised:
        human_cli.run_human_command(arguments)
    assert raised.value.code == 2
