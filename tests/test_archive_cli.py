from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from neocortex.deduplication import snapshot_path
from _04_Nucleo_Operativo.archive_route import ArchiveRoute, ArchiveRouteConfig
from _04_Nucleo_Operativo.cli_app import dispatch_direct
from _04_Nucleo_Operativo.cli_parser import build_parser
from _04_Nucleo_Operativo.cli_validation import validate_arguments
from _04_Nucleo_Operativo.cancellation import CancellationToken
from tests.test_archive_route import FakeFrameworkRouteState


def _indexed_state(tmp_path: Path) -> Path:
    source = tmp_path / "contenedor.zip"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("carpeta/documento.txt", "protección diferencial visible")
    state_directory = tmp_path / "state"
    route = ArchiveRoute(
        ArchiveRouteConfig(state_path=state_directory / "archive.sqlite3"),
        FakeFrameworkRouteState((snapshot_path(source),)),  # type: ignore[arg-type]
        1,
        cancellation=CancellationToken(),
    )
    route.run()
    return state_directory


def _args(*values: str):
    args = build_parser().parse_args(values)
    validate_arguments(args)
    return args


def test_archive_status_search_and_list_are_explicit_about_zip_location(
    tmp_path: Path,
    capsys,
) -> None:
    state = _indexed_state(tmp_path)

    assert dispatch_direct(_args("--state-directory", str(state), "--archive-status")) == 0
    status_output = capsys.readouterr().out
    assert "ARCHIVE_STATUS state=available" in status_output
    assert "members=1" in status_output

    assert (
        dispatch_direct(
            _args(
                "--state-directory",
                str(state),
                "--archive-search",
                "protección",
            )
        )
        == 0
    )
    search_output = capsys.readouterr().out
    assert "location=archive_member inside_zip=1" in search_output
    assert 'member="carpeta/documento.txt"' in search_output
    assert 'virtual_path="' in search_output

    assert (
        dispatch_direct(
            _args(
                "--state-directory",
                str(state),
                "--archive-list",
                "1",
                "--archive-json",
            )
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["inside_zip"] is True
    assert payload["location"] == "archive_member"
    assert payload["member_chain"] == "carpeta/documento.txt"


def test_archive_status_does_not_create_missing_state(tmp_path: Path) -> None:
    state = tmp_path / "missing"

    assert dispatch_direct(_args("--state-directory", str(state), "--archive-status")) == 2
    assert not state.exists()


def test_knowledge_human_output_marks_archive_members_explicitly(
    tmp_path: Path,
    capsys,
) -> None:
    state = _indexed_state(tmp_path)

    code = dispatch_direct(
        _args(
            "--state-directory",
            str(state),
            "--knowledge-search",
            "protección diferencial",
        )
    )

    output = capsys.readouterr().out
    assert code == 4  # other historical Knowledge owners are absent in this fixture
    assert "KNOWLEDGE_HIT" in output
    assert "location=archive_member inside_zip=1" in output
    assert 'container="' in output
    assert 'member="carpeta/documento.txt"' in output
    assert 'chain="carpeta/documento.txt"' in output


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (("--archive-max-depth", "0"), "--archive-max-depth must be positive"),
        (("--archive-list", "0"), "--archive-list must be between 1 and 1000"),
        (("--archive-json",), "--archive-json requires"),
        (("--archive-container", "x"), "--archive-container requires"),
        (("--archive-search-limit", "2"), "--archive-search-limit requires"),
    ],
)
def test_archive_cli_rejects_unconsumed_or_unbounded_controls(
    arguments: tuple[str, ...],
    message: str,
) -> None:
    args = build_parser().parse_args(arguments)

    with pytest.raises(SystemExit, match=message):
        validate_arguments(args)
