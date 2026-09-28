"""Synthetic replay efficiency measurements that retain Text fences."""

from __future__ import annotations

from pathlib import Path

import pytest

import neocortex.capabilities.formats.text.text_route as text_route_module
from neocortex.capabilities.formats.text.text_route import TextRoute, TextRouteConfig
from neocortex.deduplication import snapshot_path
from neocortex.runtime.control.cancellation import CancellationToken
from neocortex.runtime.control.cancellation import CancellationRequested
from tests.test_text_route import FakeTextFrameworkState


def _fixture(tmp_path: Path, count: int = 40):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    by_mime: dict[str, list] = {"text/plain": [], "text/csv": []}
    for index in range(count):
        if index % 2:
            path = corpus / f"item-{index:04}.txt"
            path.write_text(f"Synthetic maintenance report {index}\n", encoding="utf-8")
            mime = "text/plain"
        else:
            path = corpus / f"item-{index:04}.csv"
            path.write_text("field,value\ntransformer,42\n", encoding="utf-8")
            mime = "text/csv"
        by_mime[mime].append(snapshot_path(path))
    return {key: tuple(value) for key, value in by_mime.items()}


def test_replay_reuses_outputs_but_keeps_source_read_and_identity_fence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidates = _fixture(tmp_path)
    state = tmp_path / "text.sqlite3"
    reads: list[int] = []
    lookup_probes = 0
    original_read = text_route_module._read_exact
    original_lookup = text_route_module.text_route_lookups_available

    def read(*args, **kwargs):
        payload = original_read(*args, **kwargs)
        reads.append(len(payload))
        return payload

    def lookup(connection):
        nonlocal lookup_probes
        lookup_probes += 1
        return original_lookup(connection)

    monkeypatch.setattr(text_route_module, "_read_exact", read)
    monkeypatch.setattr(text_route_module, "text_route_lookups_available", lookup)
    first = TextRoute(
        TextRouteConfig(state_path=state),
        FakeTextFrameworkState(candidates),
        1,
        cancellation=CancellationToken(),
    ).run()
    reads.clear()
    second = TextRoute(
        TextRouteConfig(state_path=state),
        FakeTextFrameworkState(candidates),
        2,
        cancellation=CancellationToken(),
    ).run()

    assert first.extracted == 40
    assert second.cache_hits == 40 and second.extracted == 0
    # Replay still re-reads admitted bytes before cache validation; this is the
    # source-change fence, not a hidden assumption about unchanged files.
    assert len(reads) == 40 and sum(reads) > 0
    assert lookup_probes == 2
    assert second.text_chars == first.text_chars


def test_changed_source_cannot_become_a_replay_hit(tmp_path: Path) -> None:
    candidates = _fixture(tmp_path, count=4)
    state = tmp_path / "text.sqlite3"
    first = TextRoute(
        TextRouteConfig(state_path=state),
        FakeTextFrameworkState(candidates),
        1,
        cancellation=CancellationToken(),
    ).run()
    assert first.extracted == 4
    changed = Path(candidates["text/plain"][0].path)
    changed.write_text("changed after first extraction", encoding="utf-8")
    refreshed = {key: tuple(value) for key, value in candidates.items()}
    refreshed["text/plain"] = tuple(
        snapshot_path(Path(item.path)) for item in refreshed["text/plain"]
    )
    second = TextRoute(
        TextRouteConfig(state_path=state),
        FakeTextFrameworkState(refreshed),
        2,
        cancellation=CancellationToken(),
    ).run()
    assert second.extracted == 1
    assert second.cache_hits == 3


def test_replay_cancellation_still_abstains_before_publication(tmp_path: Path) -> None:
    candidates = _fixture(tmp_path, count=4)
    state = tmp_path / "text.sqlite3"
    TextRoute(
        TextRouteConfig(state_path=state),
        FakeTextFrameworkState(candidates),
        1,
        cancellation=CancellationToken(),
    ).run()
    token = CancellationToken()
    token.cancel()
    with pytest.raises(CancellationRequested):
        TextRoute(
            TextRouteConfig(state_path=state),
            FakeTextFrameworkState(candidates),
            2,
            cancellation=token,
        ).run()
