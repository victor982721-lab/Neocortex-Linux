"""Read-only CLI pages distinguish an empty answer from absent or failed state."""

import argparse
import json

from neocortex.api.cli.cli_direct import (
    run_file_action_recovery_status,
    run_review_candidates,
)
from neocortex.persistence.framework_state_writer import FrameworkState


def _review_args(directory):
    return argparse.Namespace(
        state_directory=directory, review_candidates=5, review_route=None,
        review_recommendation=None, review_status='open', review_json=True,
        review_json_lines=False, review_after=None,
    )


def _recovery_args(directory):
    return argparse.Namespace(
        state_directory=directory, action_recovery_limit=5,
        action_recovery_after=0, action_recovery_run=None,
        action_recovery_json=True, action_recovery_json_lines=False,
    )


def test_review_empty_and_absent_are_distinct(tmp_path, capsys):
    args = _review_args(tmp_path)
    assert run_review_candidates(args) == 2
    absent = json.loads(capsys.readouterr().out)
    assert absent['availability'] == 'absent'
    assert absent['complete'] is False
    assert absent['total_matching'] is None
    assert not (tmp_path / 'framework.sqlite3').exists()
    with FrameworkState(tmp_path / 'framework.sqlite3'):
        pass
    original = (tmp_path / 'framework.sqlite3').read_bytes()
    assert run_review_candidates(args) == 0
    empty = json.loads(capsys.readouterr().out)
    assert empty['availability'] == 'ready'
    assert empty['returned'] == empty['total_matching'] == 0
    assert empty['has_more'] is False
    assert empty['complete'] is True
    assert (tmp_path / 'framework.sqlite3').read_bytes() == original


def test_recovery_empty_and_absent_are_distinct(tmp_path, capsys):
    args = _recovery_args(tmp_path)
    assert run_file_action_recovery_status(args) == 2
    absent = json.loads(capsys.readouterr().out)
    assert absent['availability'] == 'absent'
    assert absent['complete'] is False
    with FrameworkState(tmp_path / 'framework.sqlite3'):
        pass
    original = (tmp_path / 'framework.sqlite3').read_bytes()
    assert run_file_action_recovery_status(args) == 0
    empty = json.loads(capsys.readouterr().out)
    assert empty['returned'] == 0
    assert empty['items'] == []
    assert empty['availability'] == 'ready'
    assert empty['complete'] is True
    assert (tmp_path / 'framework.sqlite3').read_bytes() == original
