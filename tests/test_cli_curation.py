"""Focused safety checks for the read-only curation CLI adapter."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from neocortex.api.cli import cli_curation
from neocortex.curation import CurationItem, CurationPreview
from neocortex.interface.entrypoint import entrypoint


_TRUNCATION_KEY = "__neocortex_sanitization_truncated__"
_TRUNCATION_VALUE = "[contenido omitido por límite]"


def _malicious_preview() -> CurationPreview:
    hostile = "\x1b[31m/Corpus/archivo\nforged\x1b[0m" + ("x" * 20_000)
    return CurationPreview(
        schema_version=1,
        coverage="complete",
        missing_owners=("\x1b[32mowner\nforged",),
        root=hostile,
        scan_id=7,
        inventory_files=1,
        duplicate_groups=1,
        duplicate_members=1,
        reclaimable_bytes=12,
        organization_plans=0,
        empty_files=0,
        preview_limit=1,
        items_total=1,
        items_truncated=False,
        preview_fingerprint="sha256:preview",
        items=(
            CurationItem(
                item_id="organization:1",
                kind="organization_plan",
                status="review",
                action="review_organization_proposal",
                source_path=hostile,
                destination_path=hostile,
                reason="\x1b[33mreason\nforged" + ("r" * 20_000),
                evidence={"payload": hostile, "nested": {"reason": hostile}},
            ),
        ),
    )


@pytest.mark.parametrize("json_output", (False, True))
def test_curation_cli_bounds_and_sanitizes_state_derived_output(
    json_output: bool,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    preview = _malicious_preview()
    monkeypatch.setattr(cli_curation, "build_curation_preview", lambda *_args, **_kwargs: preview)

    exit_code = cli_curation.run_curation_preview(
        argparse.Namespace(
            state_directory=tmp_path / "state",
            curation_preview=1,
            curation_json=json_output,
        )
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "\x1b" not in captured.out
    assert len(captured.out) < 8_000
    if json_output:
        payload = json.loads(captured.out)
        item = payload["items"][0]
        assert "\n" not in payload["root"]
        assert len(payload["root"]) <= 800
        assert "\n" not in item["source_path"]
        assert len(item["reason"]) <= 800
        assert item["evidence"] == {_TRUNCATION_KEY: _TRUNCATION_VALUE}
    else:
        assert captured.out.count("\n") == 2
        assert "CURATION_PREVIEW" in captured.out
        assert "CURATION_ITEM id=organization:1" in captured.out
        assert "evidence={\"__neocortex_sanitization_truncated__\"" in captured.out


def test_curation_cli_rejects_explicit_root_before_reading_state(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    called = False

    def fail_if_state_is_read(*_args: object, **_kwargs: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("curation state must not be read for a root-qualified preview")

    monkeypatch.setattr(cli_curation, "build_curation_preview", fail_if_state_is_read)

    exit_code = entrypoint(
        (
            "--root",
            str(tmp_path / "corpus"),
            "--curation-preview",
            "1",
        )
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert called is False
    assert captured.out == "ERROR curation-preview cannot be combined with --root\n"
