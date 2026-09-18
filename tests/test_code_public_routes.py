"""Executable public-route scenarios for the Code/content boundary."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from neocortex.interface.entrypoint import entrypoint


def test_public_text_route_replays_and_reaches_search_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    source = corpus / "fuente.txt"
    source.write_text("proteccion diferencial del transformador\n", encoding="utf-8")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))

    arguments = (
        "--root",
        str(corpus),
        "--route",
        "text",
        "--text-max-count",
        "1",
        "--strict-exit-codes",
        # The real governor remains active. This one-file CLI fixture uses
        # explicit, small public limits instead of the desktop's 3 GiB reserve.
        "--global-min-free-memory-mb", "128",
        "--global-min-free-commit-mb", "128",
        "--global-memory-budget-mb", "256",
    )
    assert entrypoint(arguments) == 0
    first = capsys.readouterr()
    assert "route=text" in first.out
    assert "processed=1" in first.out
    assert "extracted=1" in first.out
    assert "cache_hits=0" in first.out

    assert entrypoint(arguments) == 0
    replay = capsys.readouterr()
    assert "route=text" in replay.out
    assert "processed=0" in replay.out
    assert "extracted=0" in replay.out
    assert "cache_hits=1" in replay.out

    # Other owners are intentionally absent from this one-file product probe, so
    # the public federated query must return its exact Text evidence and a
    # structured partial exit rather than pretending the whole corpus is ready.
    assert entrypoint(("search", "proteccion diferencial", "--json")) == 4
    search = capsys.readouterr()
    payload = json.loads(search.out)
    personal = next(item for item in payload["scopes"] if item["scope"] == "personal")
    result = personal["result"]
    assert result["complete"] is False
    assert result["hits"]
    hit = result["hits"][0]
    assert hit["resource"]["current_path"] == str(source)
    assert hit["resource"]["owner"] == "text"
    assert hit["evidence"]["method"] == "extracted"
    assert "proteccion" in hit["evidence"]["snippet"]
    assert payload["read_only"] is True
