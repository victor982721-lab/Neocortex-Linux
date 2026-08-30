"""Public read-only surfaces for focal Code questions and storage evidence."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

import neocortex.code.code_question_resolver as resolver_module
import neocortex.code.code_review as review_module
import neocortex.code.code_storage_analysis as storage_module
from neocortex.code.code_interface_surface_analysis import CLI_SURFACE_QUESTION
from neocortex.api.cli.cli_parser import build_parser
from neocortex.api.cli.cli_validation import validate_arguments
from neocortex.persistence.sqlite_immutable import capture_sqlite_immutable_fence
from neocortex import read_api
from neocortex.cli import _translate_canonical_arguments, entrypoint
from tests.test_code_review import _build_state, _status


@dataclass(frozen=True)
class _PayloadResult:
    status: str
    payload: dict[str, object]

    def as_payload(self) -> dict[str, object]:
        return self.payload


def _exit(arguments: tuple[str, ...]) -> int:
    try:
        return entrypoint(arguments)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2


def test_canonical_help_and_translation_accept_question_options_before_or_after_positional(
    capsys: pytest.CaptureFixture[str],
) -> None:
    question_id = CLI_SURFACE_QUESTION.question_id

    assert entrypoint(("code", "question", "--help")) == 0
    question_help = capsys.readouterr()
    assert question_help.err == ""
    assert "usage: Neocortex code question" in question_help.out
    assert "QUESTION_ID" in question_help.out
    assert "--limit" in question_help.out
    assert "--state-directory" in question_help.out
    assert "--code-question" not in question_help.out

    assert entrypoint(("code", "storage", "--help")) == 0
    storage_help = capsys.readouterr()
    assert storage_help.err == ""
    assert "usage: Neocortex code storage" in storage_help.out
    assert "--run-limit" in storage_help.out
    assert "--row-scan-limit" in storage_help.out
    assert "--retain-runs" in storage_help.out
    assert "--code-storage" not in storage_help.out

    before = _translate_canonical_arguments(
        ("code", "question", "--limit", "7", question_id, "--json")
    )
    after = _translate_canonical_arguments(("code", "question", question_id, "--json", "--limit=7"))
    assert before[:2] == after[:2] == ["--code-question", question_id]
    assert "--code-question-limit" in before
    assert "--code-question-limit=7" in after
    assert "--code-json" in before and "--code-json" in after


@pytest.mark.parametrize(
    ("arguments", "message"),
    (
        (("--code-question-limit", "3"), "requires --code-question"),
        (("--code-question", " "), "non-empty trimmed text"),
        (("--code-question", "q", "--code-question-limit", "51"), "between 1 and 50"),
        (("--code-storage-run-limit", "3"), "require --code-storage"),
        (
            ("--code-storage", "--code-storage-row-scan-limit", "1000001"),
            "between 1 and 1000000",
        ),
        (
            (
                "--code-storage",
                "--code-storage-run-limit",
                "2",
                "--code-storage-retain-runs",
                "3",
            ),
            "between 1 and --code-storage-run-limit",
        ),
    ),
)
def test_flat_observability_arguments_fail_closed(
    arguments: tuple[str, ...],
    message: str,
) -> None:
    args = build_parser().parse_args(arguments)
    with pytest.raises(SystemExit, match=message):
        validate_arguments(args)


def test_public_question_command_is_focal_and_preserves_owner_bytes_and_sidecars(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_directory = tmp_path / "state"
    database = _build_state(state_directory, hotspots=False)
    monkeypatch.setattr(
        resolver_module,
        "read_self_analysis_status",
        lambda _state_directory, _latest_run: _status(tmp_path),
    )

    def forbidden_global_review(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("focal question CLI must not materialize the global review")

    monkeypatch.setattr(review_module, "review_code_state", forbidden_global_review)
    before_fence = capture_sqlite_immutable_fence(database)
    before_digest = hashlib.sha256(database.read_bytes()).hexdigest()
    before_entries = tuple(sorted(item.name for item in state_directory.iterdir()))

    code = entrypoint(
        (
            "code",
            "question",
            "--limit",
            "7",
            CLI_SURFACE_QUESTION.question_id,
            "--state-directory",
            str(state_directory),
            "--json",
        )
    )

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["schema"] == "neocortex.code-question-resolution/v1"
    assert payload["status"] == "ready"
    assert payload["question_id"] == CLI_SURFACE_QUESTION.question_id
    assert payload["source_surface"] == "interface_surface"
    assert capture_sqlite_immutable_fence(database) == before_fence
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before_digest
    assert tuple(sorted(item.name for item in state_directory.iterdir())) == before_entries


def test_public_question_unsupported_and_missing_storage_exit_two_without_creating_state(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_directory = tmp_path / "missing"
    unsupported = entrypoint(
        (
            "code",
            "question",
            "architecture.unregistered_question",
            "--state-directory",
            str(state_directory),
            "--json",
        )
    )
    unsupported_payload = json.loads(capsys.readouterr().out)
    assert unsupported == 2
    assert unsupported_payload["status"] == "unsupported"
    assert unsupported_payload["fallback"]["automatic"] is False
    assert not state_directory.exists()

    storage = entrypoint(
        (
            "code",
            "storage",
            "--state-directory",
            str(state_directory),
            "--json",
        )
    )
    storage_payload = json.loads(capsys.readouterr().out)
    assert storage == 2
    assert storage_payload["status"] == "abstained"
    assert storage_payload["reason"] == "code_state_missing_or_not_regular"
    assert not state_directory.exists()


def test_public_storage_command_is_bounded_and_immutable(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_directory = tmp_path / "state"
    database = _build_state(state_directory, hotspots=False)
    before_fence = capture_sqlite_immutable_fence(database)
    before_digest = hashlib.sha256(database.read_bytes()).hexdigest()
    before_entries = tuple(sorted(item.name for item in state_directory.iterdir()))

    code = entrypoint(
        (
            "code",
            "storage",
            "--row-scan-limit",
            "1000",
            "--run-limit",
            "3",
            "--retain-runs",
            "2",
            "--state-directory",
            str(state_directory),
            "--json",
        )
    )

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["kind"] == "code-storage-analysis"
    assert payload["schema"] == "neocortex.code-storage-analysis/v1"
    assert payload["status"] == "ready"
    assert payload["mutation_authority"] is False
    assert payload["retention"]["action"] == "preview_only"
    assert payload["retention"]["deletion_supported"] is False
    assert capture_sqlite_immutable_fence(database) == before_fence
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before_digest
    assert tuple(sorted(item.name for item in state_directory.iterdir())) == before_entries


def test_read_api_code_question_uses_only_fixed_scopes_and_exact_focal_reader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bindings = (
        read_api.ScopeBinding(read_api.ReadScope.PERSONAL, tmp_path / "personal"),
        read_api.ScopeBinding(read_api.ReadScope.FRAMEWORK, tmp_path / "framework"),
    )
    calls: list[tuple[Path, str, int]] = []

    def resolve(state: Path, question_id: str, *, limit: int) -> _PayloadResult:
        calls.append((state, question_id, limit))
        status = "ready" if state.name == "framework" else "abstained"
        return _PayloadResult(
            status,
            {
                "schema": "neocortex.code-question-resolution/v1",
                "question_id": question_id,
                "status": status,
            },
        )

    monkeypatch.setattr(read_api, "scope_bindings", lambda _scope: bindings)
    monkeypatch.setattr(resolver_module, "resolve_code_question", resolve)

    payload = read_api.code_question_payload(
        CLI_SURFACE_QUESTION.question_id,
        "all",
        limit=4,
    )

    assert payload["kind"] == "neocortex_scoped_code_question"
    assert payload["read_only"] is True
    assert payload["status"] == "abstained"
    assert payload["exit_code"] == 2
    assert [entry["status"] for entry in payload["scopes"]] == ["abstained", "ready"]
    assert calls == [
        (tmp_path / "personal", CLI_SURFACE_QUESTION.question_id, 4),
        (tmp_path / "framework", CLI_SURFACE_QUESTION.question_id, 4),
    ]
    with pytest.raises(ValueError, match="personal, framework or all"):
        read_api.code_question_payload(
            CLI_SURFACE_QUESTION.question_id,
            str(tmp_path / "attacker-controlled"),
        )
    with pytest.raises(ValueError, match="non-empty trimmed text"):
        read_api.code_question_payload(" untrusted.question ", "framework")
    with pytest.raises(ValueError, match="between 1 and 50"):
        read_api.code_question_payload(CLI_SURFACE_QUESTION.question_id, "framework", limit=51)


def test_direct_handlers_map_only_ready_to_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        resolver_module,
        "resolve_code_question",
        lambda *_args, **_kwargs: _PayloadResult(
            "abstained",
            {
                "schema": "neocortex.code-question-resolution/v1",
                "question_id": "fixture.question",
                "status": "abstained",
                "reason": "fixture",
                "evaluations": [],
                "limitations": [],
            },
        ),
    )
    monkeypatch.setattr(
        storage_module,
        "analyze_code_storage",
        lambda *_args, **_kwargs: _PayloadResult(
            "abstained",
            {
                "kind": "code-storage-analysis",
                "schema": "neocortex.code-storage-analysis/v1",
                "database": str(tmp_path / "code.sqlite3"),
                "status": "abstained",
                "reason": "fixture",
                "tables": [],
                "providers": [],
                "runs": [],
                "limitations": [],
            },
        ),
    )

    assert _exit(("--code-question", "fixture.question", "--code-json")) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "abstained"
    assert _exit(("--code-storage", "--code-json")) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "abstained"
