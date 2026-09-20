"""Real local CLI keeper selections, never corpus effects or inferred authority."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from neocortex.api.cli.cli_config import framework_config_from_args
from neocortex.api.cli.cli_parser import build_parser
from neocortex.api.cli.cli_validation import validate_arguments
from neocortex.deduplication import DedupIndex
from neocortex.runtime.orchestration.dedup_keeper import KeeperSelectionError, resolve_keeper_inputs


def _file(root: Path, relative: str, content: bytes = b"same keeper fixture bytes") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _scan(tmp_path: Path, root: Path):
    index = DedupIndex(tmp_path / "dedup.sqlite3")
    summary = index.scan(root, excluded_paths=())
    return index, summary.scan_id


def test_repeatable_keeper_options_survive_parser_to_runtime_config(tmp_path: Path) -> None:
    files = (tmp_path / "a", tmp_path / "b")
    roots = (tmp_path / "first", tmp_path / "second")
    args = build_parser().parse_args(
        [
            "--dedup-keep",
            str(files[0]),
            "--dedup-keep",
            str(files[1]),
            "--dedup-prefer-root",
            str(roots[0]),
            "--dedup-prefer-root",
            str(roots[1]),
        ]
    )
    validate_arguments(args)
    config = framework_config_from_args(args)
    assert config.dedup_keep_paths == files
    assert config.dedup_prefer_roots == roots


@pytest.mark.parametrize(
    "operation", [["--status"], ["--curation-preview", "1"], ["--route-only", "--route", "pdf"]]
)
def test_keeper_options_are_not_silently_ignored_by_nonplanning_commands(
    operation: list[str],
) -> None:
    args = build_parser().parse_args([*operation, "--dedup-keep", "/fixture/keep.pdf"])
    with pytest.raises(SystemExit, match=r"keeper|dedup-keep"):
        validate_arguments(args)


def test_keep_requires_current_inventoried_snapshot_and_containment(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    source = _file(root, "source.bin")
    outside = _file(tmp_path / "outside", "source.bin")
    index, scan_id = _scan(tmp_path, root)
    try:
        with pytest.raises(KeeperSelectionError, match="outside_inventory_root"):
            resolve_keeper_inputs(index, scan_id, keep_paths=(outside,))
        newcomer = _file(root, "newcomer.bin")
        with pytest.raises(KeeperSelectionError, match="not_in_selected_inventory"):
            resolve_keeper_inputs(index, scan_id, keep_paths=(newcomer,))
        source.write_bytes(b"changed after inventory")
        with pytest.raises(KeeperSelectionError, match="snapshot_changed"):
            resolve_keeper_inputs(index, scan_id, keep_paths=(source,))
    finally:
        index.close()


def test_preferred_root_is_bound_to_directory_identity_and_inventory(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    preferred = root / "preferred"
    _file(preferred, "source.bin")
    empty = root / "empty"
    empty.mkdir()
    index, scan_id = _scan(tmp_path, root)
    try:
        with pytest.raises(KeeperSelectionError, match="has_no_inventory_members"):
            resolve_keeper_inputs(index, scan_id, preferred_roots=(empty,))
        selection = resolve_keeper_inputs(index, scan_id, preferred_roots=(preferred,))
        assert selection.policy.preferred_roots == (str(preferred),)
        preferred.rename(root / "old-preferred")
        preferred.mkdir()
        with pytest.raises(KeeperSelectionError, match="directory_identity_changed"):
            selection.verify()
    finally:
        index.close()


def test_symlink_does_not_supply_a_keeper_or_preference_root(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    source = _file(root / "original", "source.bin")
    file_link, directory_link = root / "file-link", root / "dir-link"
    file_link.symlink_to(source)
    directory_link.symlink_to(source.parent, target_is_directory=True)
    index, scan_id = _scan(tmp_path, root)
    try:
        with pytest.raises(KeeperSelectionError, match="symlink_or_alias"):
            resolve_keeper_inputs(index, scan_id, keep_paths=(file_link,))
        with pytest.raises(KeeperSelectionError, match="symlink_or_alias"):
            resolve_keeper_inputs(index, scan_id, preferred_roots=(directory_link,))
    finally:
        index.close()


def test_repeated_keep_aliases_coalesce_to_one_physical_decision(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    source = _file(root, "original.bin")
    alias = root / "alias.bin"
    os.link(source, alias)
    index, scan_id = _scan(tmp_path, root)
    try:
        selection = resolve_keeper_inputs(index, scan_id, keep_paths=(source, alias, source))
        assert len(selection.policy.explicit_keep_identities) == 1
        assert len(selection.keep_snapshots) == 2
    finally:
        index.close()


def test_default_keeper_inputs_do_not_enumerate_the_inventory(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "corpus"
    _file(root, "source.bin")
    index, scan_id = _scan(tmp_path, root)
    try:
        monkeypatch.setattr(
            index,
            "snapshots",
            lambda *_: pytest.fail("default preferences must stay constant-cost"),
        )
        selection = resolve_keeper_inputs(index, scan_id)
        assert selection.policy.explicit_keep_identities == ()
        assert selection.policy.preferred_roots == ()
        selection.verify()
    finally:
        index.close()


def _cli(*arguments: str) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, "-B", "-m", "neocortex", *arguments],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )


@pytest.mark.parametrize("choice", ["explicit", "preferred"])
def test_real_cli_selection_reaches_plan_proof_and_replays_without_effects(
    tmp_path: Path, choice: str
) -> None:
    root, state = tmp_path / "corpus", tmp_path / "state"
    default = _file(root, "default/report.bin")
    preferred = _file(root, "preferred/report (1).bin")
    before = {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in (default, preferred)
    }
    selected = (
        ["--dedup-keep", str(preferred)]
        if choice == "explicit"
        else ["--dedup-prefer-root", str(preferred.parent)]
    )
    reason = "explicit_user_decision" if choice == "explicit" else "preferred_location"
    arguments = [
        "--root",
        str(root),
        "--state-directory",
        str(state),
        "--route",
        "none",
        "--no-document-catalog",
        "--dedup-policy",
        "exact",
        "--show-groups",
        "5",
        *selected,
    ]
    for _ in range(2):
        result = _cli(*arguments)
        assert result.returncode == 0, result.stderr[-1000:] + result.stdout[-2000:]
        assert f"KEEP {preferred}" in result.stdout
        assert f"keeper_reason={reason}" in result.stdout
        assert "KEEPER_REFERENCES status=refs_unverified" in result.stdout
        preview = _cli(
            "--state-directory", str(state), "--curation-preview", "10", "--curation-json"
        )
        assert preview.returncode in {0, 2}, preview.stderr[-1000:]
        payload = json.loads(preview.stdout)
        group = next(item for item in payload["items"] if item["kind"] == "duplicate_group")
        assert group["evidence"]["group_proof"]["keeper_reason"] == reason
        assert group["evidence"]["physical_reclaimable_bytes"] is None
        assert group["evidence"]["keep_path"] == str(preferred)
    assert {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in (default, preferred)
    } == before


def test_real_cli_conflicting_explicit_keeps_fail_without_effects(tmp_path: Path) -> None:
    root, state = tmp_path / "corpus", tmp_path / "state"
    first, second = _file(root, "one.bin"), _file(root, "two.bin")
    result = _cli(
        "--root",
        str(root),
        "--state-directory",
        str(state),
        "--route",
        "none",
        "--no-document-catalog",
        "--dedup-policy",
        "exact",
        "--dedup-keep",
        str(first),
        "--dedup-keep",
        str(second),
    )
    assert result.returncode != 0
    assert "conflicting_explicit_keepers" in (result.stdout + result.stderr)
    assert first.read_bytes() == second.read_bytes() == b"same keeper fixture bytes"


def test_real_cli_out_of_scope_keep_fails_before_creating_inventory_state(tmp_path: Path) -> None:
    root, state = tmp_path / "corpus", tmp_path / "state"
    _file(root, "inside.bin")
    outside = _file(tmp_path / "outside", "requested.bin")
    result = _cli(
        "--root", str(root), "--state-directory", str(state), "--dedup-keep", str(outside)
    )
    assert result.returncode != 0
    assert "outside_inventory_root" in result.stdout + result.stderr
    assert not state.exists()


def test_real_cli_without_keeper_or_route_keeps_root_only_invocation_a_noop(tmp_path: Path) -> None:
    root, state = tmp_path / "corpus", tmp_path / "state"
    source = _file(root, "source.bin")
    result = _cli("--root", str(root), "--state-directory", str(state))
    assert result.returncode == 0
    assert not state.exists()
    assert source.read_bytes() == b"same keeper fixture bytes"
