from __future__ import annotations

# region [01] Imports and fixtures

import argparse

import pytest

from neocortex.deduplication import FileSnapshot
from neocortex.api.cli.cli_app import dispatch_direct
from neocortex.api.cli.cli_direct import (
    run_office_search,
    run_review_candidates,
    run_review_decisions,
)
from neocortex.api.cli.cli_parser import build_parser
from neocortex.api.cli.cli_validation import validate_arguments
from neocortex.capabilities.formats.office.state import (
    initialize_office_state,
    office_database,
)
from neocortex.workflow.review.review import (
    ReviewCandidate,
    get_review_candidate,
    list_review_candidates,
    list_review_decisions,
)
from neocortex.persistence.state import FrameworkRouteState, FrameworkState


def _snapshot() -> FileSnapshot:
    return FileSnapshot(
        r"C:\corpus\subestación\plano dañado.pdf",
        0xAA10,
        0xBB20,
        1234,
        5678,
        4567,
    )


def _review_state(state_directory) -> None:
    database = state_directory / "framework.sqlite3"
    with FrameworkState(database):
        pass
    FrameworkRouteState(database).store_review_candidates(
        21,
        (
            ReviewCandidate(
                route_name="image",
                snapshot=_snapshot(),
                reason_code="document_candidate",
                source_status="done",
                recommendation="manual_review",
                retryable=False,
                confidence=0.9,
                evidence={"detector": "ocr-layout"},
                detector_version="image-review-v1",
            ),
        ),
    )


# endregion [01]


# region [02] Office search


def test_office_search_cli_is_bounded_read_only_and_dispatched(
    tmp_path,
    capsys,
) -> None:
    database = tmp_path / "office.sqlite3"
    initialize_office_state(database)
    with office_database(database) as connection:
        connection.execute(
            """INSERT INTO document_fts(file_key,format,path,title,author,body)
            VALUES(?,?,?,?,?,?)""",
            (
                "office-1",
                "xlsx",
                r"C:\corpus\transformadores.xlsx",
                "Inventario",
                "Victor",
                "transformador de potencia y subestación",
            ),
        )
        connection.commit()
    before = database.read_bytes()

    args = build_parser().parse_args(
        [
            "--state-directory",
            str(tmp_path),
            "--office-search",
            "transformador",
            "--office-search-limit",
            "7",
        ]
    )
    validate_arguments(args)

    assert dispatch_direct(args) == 0
    output = capsys.readouterr().out
    assert "OFFICE format=xlsx" in output
    assert "transformadores.xlsx" in output
    assert "[transformador]" in output
    assert database.read_bytes() == before


def test_office_search_missing_state_returns_two_without_creating_database(
    tmp_path,
    capsys,
) -> None:
    state_directory = tmp_path / "missing"
    args = argparse.Namespace(
        state_directory=state_directory,
        office_search="transformador",
        office_search_limit=5,
    )

    assert run_office_search(args) == 2
    assert "ERROR office-search" in capsys.readouterr().out
    assert not state_directory.exists()


@pytest.mark.parametrize(
    ("arguments", "message"),
    (
        (["--office-search", ""], "--office-search must be non-empty"),
        (
            ["--office-search", "transformador", "--office-search-limit", "0"],
            "--office-search-limit must be between",
        ),
        (["--office-search-limit", "5"], "requires --office-search"),
        (
            ["--office-search", "transformador", "--apply"],
            "read-only",
        ),
    ),
)
def test_office_search_cli_rejects_ambiguous_or_unbounded_arguments(
    arguments: list[str],
    message: str,
) -> None:
    args = build_parser().parse_args(arguments)
    with pytest.raises(SystemExit, match=message):
        validate_arguments(args)


# endregion [02]


# region [03] Exact append-only review decisions


def _record_arguments(state_directory, *, generation: int = 21):
    return build_parser().parse_args(
        [
            "--state-directory",
            str(state_directory),
            "--review-record",
            "confirmed",
            "--review-route",
            "image",
            "--review-reason",
            "document_candidate",
            "--review-volume-id",
            f"{_snapshot().volume_id:x}",
            "--review-file-id",
            f"{_snapshot().file_id:x}",
            "--review-generation",
            str(generation),
            "--review-actor",
            "victor",
            "--review-note",
            "Confirmed rasterized document",
        ]
    )


def test_review_record_uses_exact_snapshot_generation_and_keeps_finding_open(
    tmp_path,
    capsys,
) -> None:
    _review_state(tmp_path)
    database = tmp_path / "framework.sqlite3"
    candidate = get_review_candidate(
        database,
        route_name="image",
        volume_id=_snapshot().volume_id,
        file_id=_snapshot().file_id,
        reason_code="document_candidate",
    )
    assert candidate is not None
    assert (candidate.size, candidate.mtime_ns, candidate.birthtime_ns) == (
        _snapshot().size,
        _snapshot().mtime_ns,
        _snapshot().birthtime_ns,
    )

    args = _record_arguments(tmp_path)
    validate_arguments(args)
    assert dispatch_direct(args) == 0
    output = capsys.readouterr().out
    assert "REVIEW_DECISION_RECORDED" in output
    assert "generation=21" in output
    assert "volume=aa10 file=bb20" in output

    # An identical CLI retry resolves to the same stable XXH3 decision key.
    assert dispatch_direct(args) == 0
    retry_output = capsys.readouterr().out
    assert "reused=1" in retry_output
    assert "snapshot_match=exact" in retry_output

    decisions = list_review_decisions(database, limit=10)
    assert len(decisions) == 1
    assert decisions[0].status == "confirmed"
    assert decisions[0].actor == "victor"
    assert decisions[0].note == "Confirmed rasterized document"
    assert decisions[0].provenance["source"] == "neocortex-cli"
    assert decisions[0].idempotency_key.startswith("neocortex-cli:xxh3-128:")
    assert decisions[0].source_status == "done"
    assert decisions[0].recommendation == "manual_review"
    assert decisions[0].retryable is False
    assert decisions[0].confidence == 0.9
    assert decisions[0].evidence == {"detector": "ocr-layout"}
    assert decisions[0].detector_version == "image-review-v1"
    findings = list_review_candidates(database, limit=10)
    assert len(findings) == 1
    assert findings[0].status == "open"


def test_review_record_rejects_same_key_for_changed_candidate_snapshot(
    tmp_path,
    capsys,
) -> None:
    _review_state(tmp_path)
    args = _record_arguments(tmp_path)
    validate_arguments(args)
    assert dispatch_direct(args) == 0
    capsys.readouterr()

    FrameworkRouteState(tmp_path / "framework.sqlite3").store_review_candidates(
        21,
        (
            ReviewCandidate(
                route_name="image",
                snapshot=_snapshot(),
                reason_code="document_candidate",
                source_status="done",
                recommendation="manual_review",
                retryable=False,
                confidence=0.9,
                evidence={"detector": "changed-layout-evidence"},
                detector_version="image-review-v2",
            ),
        ),
    )

    assert dispatch_direct(args) == 2
    assert "key collision identifies different candidate snapshot" in capsys.readouterr().out
    decisions = list_review_decisions(
        tmp_path / "framework.sqlite3",
        limit=10,
    )
    assert len(decisions) == 1
    assert decisions[0].evidence == {"detector": "ocr-layout"}
    assert decisions[0].detector_version == "image-review-v1"


def test_review_record_rejects_stale_generation_without_appending(
    tmp_path,
    capsys,
) -> None:
    _review_state(tmp_path)
    args = _record_arguments(tmp_path, generation=20)
    validate_arguments(args)

    assert dispatch_direct(args) == 2
    assert "ERROR review-record review decision is stale" in capsys.readouterr().out
    assert list_review_decisions(tmp_path / "framework.sqlite3", limit=10) == []


def test_review_candidate_and_decision_listings_expose_exact_identity(
    tmp_path,
    capsys,
) -> None:
    _review_state(tmp_path)
    record_args = _record_arguments(tmp_path)
    validate_arguments(record_args)
    assert dispatch_direct(record_args) == 0
    capsys.readouterr()

    candidate_args = argparse.Namespace(
        state_directory=tmp_path,
        review_candidates=5,
        review_route="image",
        review_recommendation=None,
        review_status="open",
    )
    assert run_review_candidates(candidate_args) == 0
    candidate_output = capsys.readouterr().out
    assert "generation=21 volume=aa10 file=bb20" in candidate_output

    decision_args = argparse.Namespace(
        state_directory=tmp_path,
        review_decisions=5,
        review_route="image",
        review_reason="document_candidate",
        review_decision_status="confirmed",
        review_volume_id=_snapshot().volume_id,
        review_file_id=_snapshot().file_id,
        review_generation=21,
    )
    assert run_review_decisions(decision_args) == 0
    decision_output = capsys.readouterr().out
    assert "REVIEW_DECISION id=" in decision_output
    assert "generation=21 volume=aa10 file=bb20" in decision_output
    assert "candidate_snapshot={" in decision_output
    assert '"source_status":"done"' in decision_output
    assert '"evidence":{"detector":"ocr-layout"}' in decision_output


@pytest.mark.parametrize(
    ("arguments", "message"),
    (
        (["--review-decisions", "0"], "--review-decisions must be between"),
        (
            ["--review-decisions", "10", "--review-volume-id", "aa10"],
            "must be supplied together",
        ),
        (
            ["--review-decision-status", "confirmed"],
            "review options require a review command",
        ),
        (
            ["--review-decisions", "10", "--review-actor", "victor"],
            "--review-actor requires --review-record",
        ),
        (
            ["--review-record", "deferred", "--review-route", "image"],
            "--review-record requires",
        ),
        (
            ["--review-decisions", "10", "--apply"],
            "read-only",
        ),
    ),
)
def test_review_decision_cli_validates_action_and_exact_identity(
    arguments: list[str],
    message: str,
) -> None:
    args = build_parser().parse_args(arguments)
    with pytest.raises(SystemExit, match=message):
        validate_arguments(args)


# endregion [03]
