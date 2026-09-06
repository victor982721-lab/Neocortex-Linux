"""Text replay summaries retain extraction categories and output metrics."""

from __future__ import annotations

from pathlib import Path

from neocortex.capabilities.formats.text.text_route import TextRoute, TextRouteConfig
from neocortex.deduplication import FileSnapshot, snapshot_path
from neocortex.runtime.control.cancellation import CancellationToken
from neocortex.safety.route_filters import CandidateSelection


class _Candidates:
    def __init__(self, by_mime: dict[str, tuple[FileSnapshot, ...]]) -> None:
        self.by_mime = by_mime

    def selected_route_candidate_counts(
        self,
        _run_id: int,
        mime: str,
        max_file_bytes: int | None,
        _route_name: str,
        _selection: CandidateSelection,
    ) -> tuple[int, int]:
        snapshots = self.by_mime.get(mime, ())
        eligible = tuple(
            snapshot
            for snapshot in snapshots
            if max_file_bytes is None or snapshot.size <= max_file_bytes
        )
        return len(snapshots), len(eligible)

    def iter_selected_route_candidates(
        self,
        _run_id: int,
        mime: str,
        _route_name: str,
        _selection: CandidateSelection,
    ) -> tuple[FileSnapshot, ...]:
        return self.by_mime.get(mime, ())


def test_text_cache_hit_replays_all_extraction_metrics(
    tmp_path: Path,
) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    plain = corpus / "plain.txt"
    plain.write_text("abcdefghijk", encoding="utf-8")
    email = corpus / "message.eml"
    email.write_text(
        "From: Operacion <operacion@example.test>\n"
        "To: Victor <victor@example.test>\n"
        "Subject: Aviso\n"
        "MIME-Version: 1.0\n"
        "Content-Type: text/plain; charset=utf-8\n\n"
        "mail\n",
        encoding="utf-8",
    )
    candidates = _Candidates(
        {
            "text/plain": (snapshot_path(plain),),
            "message/rfc822": (snapshot_path(email),),
        }
    )
    state = tmp_path / "text.sqlite3"

    def run(run_id: int):
        return TextRoute(
            TextRouteConfig(state_path=state, max_text_chars=8),
            candidates,
            run_id,
            cancellation=CancellationToken(),
        ).run()

    first = run(1)
    replay = run(2)

    assert (
        first.plain_text,
        first.emails,
        first.text_chars,
        first.truncated,
    ) == (1, 1, 13, 1)
    assert (
        replay.processed,
        replay.extracted,
        replay.cache_hits,
        replay.plain_text,
        replay.emails,
        replay.text_chars,
        replay.truncated,
    ) == (0, 0, 2, 1, 1, 13, 1)
