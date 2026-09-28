"""Cost reduction is explicit and never removes full lexical/source evidence."""

from __future__ import annotations

import pytest

from neocortex.capabilities.formats.text.text_state import search_text_state
from neocortex.deduplication import snapshot_path
from neocortex.semantic.semantic_sources import iter_text_source_records
from neocortex.semantic.semantic_tabular_projection import metadata_table_section
from tests.test_text_route import _route


def _metadata_csv(count: int = 1000) -> str:
    return "source,destination,category,score,status\n" + "".join(
        f"/fixture/incoming/img-{n:05}.jpg,/fixture/output/img-{n:05}.jpg,industrial,0.75,retained\n"
        for n in range(count)
    )


def test_large_metadata_table_has_explicit_small_navigation_projection() -> None:
    text = _metadata_csv()
    section = metadata_table_section(text, "csv", checkpoint=lambda: None)
    assert section is not None
    assert len(section.text) < len(text) // 10
    assert section.provenance["source_rows"] == 1000
    assert section.provenance["original_rows_unchanged"] is True
    assert section.provenance["advisory_only"] is True
    assert section.provenance["coverage"] == "summary_with_complete_source_lexical_body"
    assert "no exhaustivas" in section.text


def test_headerless_session_shape_keeps_field_samples_without_invented_headers() -> None:
    text = "".join(
        f"2026-01-01T12:00:00Z\t/fixture/session-{n}.jsonl\t12345678-1234-1234-1234-123456789abc\tExampleApp\tC:\\Windows\\System32\n"
        for n in range(100)
    )
    section = metadata_table_section(text, "tsv", checkpoint=lambda: None)
    assert section is not None
    assert not section.provenance["source_header_present"]
    assert section.provenance["session_shape_inferred"]
    assert "System32" in section.text and "ExampleApp" in section.text


@pytest.mark.parametrize("text", [
    "equipo,lectura\n" + "transformador,42\n" * 100,
    _metadata_csv(10),
    _metadata_csv(70) + '/some/file,/other/file,"' + ("narrative word " * 50) + '",0.75,done\n',
])
def test_business_small_and_late_narrative_tables_are_not_reduced(text: str) -> None:
    assert metadata_table_section(text, "csv", checkpoint=lambda: None) is None


def test_checkpoint_interrupts_during_rows_instead_of_silently_falling_back() -> None:
    calls = 0

    def checkpoint():
        nonlocal calls
        calls += 1
        if calls == 3:
            raise ValueError("fixture deadline")

    with pytest.raises(ValueError, match="fixture deadline"):
        metadata_table_section(_metadata_csv(), "csv", checkpoint=checkpoint)


def test_full_fts_and_original_survive_dense_projection_and_replay(tmp_path) -> None:
    source = tmp_path / "files.csv"
    original = _metadata_csv()
    source.write_text(original)
    state = tmp_path / "text.sqlite3"
    candidates = {"text/csv": (snapshot_path(source),)}
    first = _route(state, candidates).run()
    assert first.extracted == 1
    records = tuple(iter_text_source_records(tmp_path, "text"))
    assert len(records) == 1
    assert records[0].section.section_kind == "text_metadata_navigation"
    assert "00999" not in records[0].section.text
    assert search_text_state(state, "00999")
    replay = _route(state, candidates, run_id=2).run()
    assert replay.cache_hits == 1 and replay.extracted == 0
    assert source.read_text() == original
    assert search_text_state(state, "00999")
