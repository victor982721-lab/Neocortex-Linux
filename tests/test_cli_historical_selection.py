"""Exercise exact historical adoption through the public CLI and real owners."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from neocortex.api.cli.cli_app import main
from tests.test_historical_selection import _fixture


def test_public_selection_prepare_approve_apply_and_replay(tmp_path: Path, capsys):
    manager, selection, registry, preserved = _fixture(tmp_path, shared=True)
    neighbor = manager.root / "unselected"
    neighbor.mkdir(mode=0o700)
    (neighbor / "keep").write_bytes(b"keep")
    selection_file = tmp_path / "selection.json"
    selection_file.write_text(json.dumps([{
        "path": str(selection.path),
        "provenance_artifact_id": selection.provenance_artifact_id,
        "preserved_artifact_id": selection.preserved_artifact_id,
    }]))
    base = ["maintenance", "--scope", "historical-temp", "--maintenance-audit-root",
            str(manager.root), "--state-directory", str(manager.state_directory), "--maintenance-json"]

    def invoke(extra):
        code = main([*base, *extra])
        output = capsys.readouterr()
        return code, json.loads(output.out)

    code, plan = invoke(["--selection-file", str(selection_file)])
    assert code == 0 and plan["read_only"] is True
    assert not (manager.state_directory / "historical-adoptions").exists()
    code, prepared = invoke(["--selection-file", str(selection_file), "--prepare-adoption"])
    assert code == 0 and prepared["digest"] == plan["digest"]
    digest = prepared["digest"]
    assert prepared["read_only"] is False
    code, _ = invoke(["--apply-adoption", digest, "--apply"])
    assert code == 2 and selection.path.exists()
    code, approved = invoke(["--approve-adoption", digest])
    assert code == 0 and approved["status"] == "approved"
    code, applied = invoke(["--apply-adoption", digest, "--apply"])
    assert code == 0 and applied["operation_status"] == "complete"
    assert not selection.path.exists()
    assert (preserved / "payload").is_file() and (neighbor / "keep").read_bytes() == b"keep"
    assert registry.verify(selection.provenance_artifact_id).state == "retired"
    code, replay = invoke(["--apply-adoption", digest, "--apply"])
    assert code == 0 and replay["records"][0]["replayed"] is True
    assert replay["applied"] == 0 and replay["replayed"] == 1


@pytest.mark.parametrize("extra", [
    ["--select", "/tmp/example"],
    ["--approve-adoption", "sha256:" + "0" * 64, "--apply"],
    ["--apply-adoption", "sha256:" + "0" * 64],
    ["--selected-id", "abc"],
    ["--select", "/tmp/example", "--provenance-artifact", "p", "--selection-file", "/tmp/s.json"],
])
def test_invalid_authority_combinations_rejected_before_owner(tmp_path: Path, extra):
    with pytest.raises(SystemExit):
        main(["maintenance", "--scope", "historical-temp", "--maintenance-audit-root",
              str(tmp_path / "audit"), "--state-directory", str(tmp_path / "state"), *extra])
    assert not (tmp_path / "state").exists()


def test_self_approval_field_in_selection_file_is_rejected(tmp_path: Path, capsys):
    manager, selection, _, _ = _fixture(tmp_path)
    source = tmp_path / "selection.json"
    source.write_text(json.dumps([{"path": str(selection.path),
                                  "provenance_artifact_id": selection.provenance_artifact_id,
                                  "approved": True}]))
    assert main(["maintenance", "--scope", "historical-temp", "--maintenance-audit-root",
                 str(manager.root), "--state-directory", str(manager.state_directory),
                 "--selection-file", str(source), "--prepare-adoption", "--maintenance-json"]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "failed"
    assert selection.path.exists()
    assert not (manager.state_directory / "historical-adoptions").exists()


def test_selection_fifo_is_rejected_without_waiting_for_writer(tmp_path: Path, capsys):
    source = tmp_path / "selection.fifo"
    os.mkfifo(source)
    assert main(["maintenance", "--scope", "historical-temp", "--maintenance-audit-root",
                 str(tmp_path / "audit"), "--state-directory", str(tmp_path / "state"),
                 "--selection-file", str(source), "--maintenance-json"]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "failed"
    assert not (tmp_path / "state").exists()
