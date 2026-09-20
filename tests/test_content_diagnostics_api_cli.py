"""Diagnostic adapters never infer latest-root scope or missing-owner health."""

import argparse
import json
from pathlib import Path

import pytest

from neocortex.api.content_diagnostics_api import content_diagnostics_error_payload, content_diagnostics_payload
from neocortex.api.cli.cli_content_diagnostics import (
    register_content_diagnostics_arguments,
    run_pdf_diagnostics,
    run_text_errors,
    validate_content_diagnostics_arguments,
)
from neocortex.capabilities.formats.pdf.pdf_state import initialize_pdf_state, pdf_database
from neocortex.capabilities.formats.text.text_state import initialize_text_state, text_database
from tests.test_pdf_coverage_diagnostics import _pdf

ROOT = "/fixture/A"


@pytest.fixture
def state(tmp_path: Path) -> Path:
    pdf = tmp_path / "pdf.sqlite3"
    initialize_pdf_state(pdf)
    text = tmp_path / "text.sqlite3"
    initialize_text_state(text)
    for key, root in (("a", ROOT), ("b", ROOT), ("c", ROOT + "-extra"), ("d", "/fixture/a")):
        _pdf(pdf, key, source=f"{root}/{key}.pdf", status="error")
        with pdf_database(pdf) as connection:
            connection.execute("UPDATE documents SET error_type='DecodeError' WHERE file_key=?", (key,))
        with text_database(text) as connection:
            connection.execute(
                """INSERT INTO documents(file_key,path,size,mtime_ns,birthtime_ns,
                processing_signature,status,content_kind,media_type,error_type,error_message,
                retryable,last_seen_run_id,updated_ns)
                VALUES(?,?,1,1,-1,'fixture','error','plain','text/plain','DecodeError','bounded',0,1,1)""",
                (key, f"{root}/{key}.txt"),
            )
            connection.commit()
    return tmp_path


@pytest.mark.parametrize("owner", ["pdf", "text"])
def test_exact_root_boundary_page_cursor_and_read_only_sources(state: Path, owner: str) -> None:
    before = {path.name: path.read_bytes() for path in state.iterdir()}
    first = content_diagnostics_payload(owner, state, ROOT, 1)
    assert first["status"] == "ok", first.get("error")
    assert first["count"] == 1 and first["truncated"] is True
    assert first["next_cursor"]
    assert first["coverage"]["snapshot_consistent"] is True
    second = content_diagnostics_payload(owner, state, ROOT, 1, cursor=first["next_cursor"])
    assert second["status"] == "ok" and second["count"] == 1
    assert second["next_cursor"] is None and second["truncated"] is False
    assert first["items"] != second["items"]
    for result in (first, second):
        item = result["items"][0]
        assert item.get("path", item.get("container_path")).startswith(ROOT + "/")
    assert first["matched_count"] == 2
    assert first["coverage"]["root_summary"]["documents"] == 2
    invalid = content_diagnostics_payload(owner, state, ROOT + "-extra", 1, cursor=first["next_cursor"])
    assert invalid["status"] == "error" and invalid["error"]["kind"] == "invalid_cursor"
    assert invalid["items"] == [] and invalid["matched_count"] is None
    assert {path.name: path.read_bytes() for path in state.iterdir()} == before


@pytest.mark.parametrize("owner", ["pdf", "text"])
def test_filters_intersect_root_and_never_relabel_root_summary_as_filtered(state: Path, owner: str) -> None:
    reason = "DecodeError"
    result = content_diagnostics_payload(owner, state, ROOT, 20, file_key="a", reason=reason)
    assert result["status"] == "ok"
    assert result["count"] == 1
    assert result["reason_field"] == "error_type"
    assert result["coverage"]["root_summary_scope"] == "requested_root_without_query_filters"
    other = content_diagnostics_payload(owner, state, ROOT, file_key="c")
    assert other["status"] == "ok" and other["count"] == 0
    fragment = content_diagnostics_payload(owner, state, ROOT, path_fragment="a.")
    assert fragment["status"] == "ok" and fragment["count"] == 1


@pytest.mark.parametrize("owner", ["pdf", "text"])
def test_missing_owner_is_unknown_not_zero_complete(tmp_path: Path, owner: str) -> None:
    missing = tmp_path / "missing"
    result = content_diagnostics_payload(owner, missing, ROOT)
    assert result["status"] == "unavailable"
    assert result["matched_count"] is None and result["truncated"] is None
    assert result["coverage"]["status"] == "unknown"
    assert result["error"]["kind"] == "owner_missing"
    assert not missing.exists()


def test_incompatible_pdf_schema_is_owner_state_failure_not_invalid_request(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    pdf = state / "pdf.sqlite3"
    initialize_pdf_state(pdf)
    with pdf_database(pdf) as connection:
        connection.execute("UPDATE metadata SET value='999' WHERE key='schema_version'")
    result = content_diagnostics_payload("pdf", state, ROOT)
    assert result["status"] == "blocked"
    assert result["error"] == {
        "kind": "owner_state_unavailable",
        "message": "PDF diagnostics require schema 13; found 999",
    }
    assert result["items"] == []
    assert result["matched_count"] is None


@pytest.mark.parametrize("options", [
    {"owner": "other"}, {"limit": 0}, {"limit": True}, {"source_root": "relative"},
    {"source_root": ""}, {"cursor": " "}, {"reason": "x" * 257}, {"file_key": "x\x00y"},
    {"source_root": "/fixture/A/../B"},
])
def test_invalid_request_is_typed_before_owner_access(monkeypatch: pytest.MonkeyPatch, options: dict) -> None:
    def unexpected_access(_self: Path):
        raise AssertionError("invalid request reached owner filesystem")

    monkeypatch.setattr(Path, "lstat", unexpected_access)
    arguments = {"owner": "pdf", "state_directory": "/does-not-exist", "source_root": ROOT, "limit": 20}
    arguments.update(options)
    result = content_diagnostics_payload(**arguments)
    assert result["error"]["kind"] == "invalid_request"
    assert result["items"] == []


def test_owner_race_discards_page_and_root_summary(state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from neocortex.capabilities.formats.text import text_diagnostics

    original = text_diagnostics.read_text_coverage

    def changed(path: Path, **kwargs):
        result = original(path, **kwargs)
        with text_database(path) as writer:
            writer.execute("UPDATE documents SET updated_ns=updated_ns+1 WHERE file_key='a'")
            writer.commit()
        return result

    monkeypatch.setattr(text_diagnostics, "read_text_coverage", changed)
    result = content_diagnostics_payload("text", state, ROOT)
    assert result["error"]["kind"] == "state_changed"
    assert result["items"] == [] and result["coverage"]["status"] == "unknown"


def test_disappearing_owner_never_turns_into_zero_complete(state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from neocortex.capabilities.formats import diagnostic_queries

    original = diagnostic_queries.state_snapshot_id
    removed = [False]

    def disappear(path: Path) -> str:
        if not removed[0]:
            removed[0] = True
            path.unlink()
        return original(path)

    monkeypatch.setattr(diagnostic_queries, "state_snapshot_id", disappear)
    result = content_diagnostics_payload("text", state, ROOT)
    assert result["error"]["kind"] == "state_changed"
    assert result["matched_count"] is None and result["truncated"] is None
    assert result["coverage"]["status"] == "unknown"


def test_symlink_owner_rejected_without_reading_target(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"do not read as owner")
    (tmp_path / "pdf.sqlite3").symlink_to(target)
    result = content_diagnostics_payload("pdf", tmp_path, ROOT)
    assert result["status"] == "blocked" and result["error"]["kind"] == "owner_unsafe"
    assert target.read_bytes() == b"do not read as owner"


def _parser(state: Path) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(ROOT))
    parser.add_argument("--state-directory", type=Path, default=state)
    register_content_diagnostics_arguments(parser)
    return parser


@pytest.mark.parametrize("flag,runner", [
    ("--pdf-diagnostics", run_pdf_diagnostics), ("--text-errors", run_text_errors),
])
def test_cli_default_root_and_json_are_shared_with_api(state: Path, capsys: pytest.CaptureFixture, flag, runner) -> None:
    args = _parser(state).parse_args([flag, "1", "--diagnostics-json"])
    validate_content_diagnostics_arguments(args)
    assert runner(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["requested_root"] == ROOT and result["count"] == 1
    assert result["read_only"] is True


def test_cli_selector_conflicts_orphan_filter_and_absent_json(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    parser = _parser(tmp_path)
    with pytest.raises(SystemExit):
        parser.parse_args(["--text-errors", "1", "--pdf-diagnostics", "1"])
    with pytest.raises(SystemExit, match="requires"):
        validate_content_diagnostics_arguments(parser.parse_args(["--diagnostics-path", "manual"]))
    capsys.readouterr()
    args = parser.parse_args(["--text-errors", "1", "--diagnostics-json"])
    assert run_text_errors(args) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "unavailable" and result["matched_count"] is None


@pytest.mark.parametrize("option,value", [
    ("route", "  PDF , text "), ("all", True), ("route_only", True),
    ("apply", True), ("organization_apply", True),
])
def test_cli_rejects_processing_modes_before_query(tmp_path: Path, option, value) -> None:
    args = _parser(tmp_path).parse_args(["--text-errors", "2"])
    setattr(args, option, value)
    with pytest.raises(SystemExit, match="cannot be combined"):
        validate_content_diagnostics_arguments(args)


def test_json_validation_errors_and_config_failure_share_error_envelope(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    args = _parser(tmp_path).parse_args(["--text-errors", "0", "--diagnostics-json"])
    with pytest.raises(SystemExit) as caught:
        validate_content_diagnostics_arguments(args)
    assert caught.value.code == 2
    invalid = json.loads(capsys.readouterr().out)
    assert invalid["error"]["kind"] == "invalid_request" and invalid["matched_count"] is None
    unavailable = content_diagnostics_error_payload(
        "text", kind="configuration_unavailable", message="root configuration is unavailable", status="blocked",
    )
    assert unavailable["requested_root"] is None
    assert unavailable["coverage"]["status"] == "unknown"
    assert unavailable.keys() == invalid.keys()


@pytest.mark.parametrize("owner,flag", [
    ("pdf", "--pdf-diagnostics"), ("text", "--text-errors"),
])
def test_registered_public_parser_dispatches_the_same_scoped_read(
    state: Path, capsys: pytest.CaptureFixture, owner: str, flag: str,
) -> None:
    from neocortex.api.cli.cli_operations import dispatch_direct_operation, selected_direct_operations
    from neocortex.api.cli.cli_parser import build_parser

    args = build_parser().parse_args([
        flag, "1", "--root", ROOT, "--state-directory", str(state), "--diagnostics-json",
    ])
    validate_content_diagnostics_arguments(args)
    assert len(selected_direct_operations(args)) == 1
    assert dispatch_direct_operation(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["owner"] == owner and result["count"] == 1
    assert result["requested_root"] == ROOT
