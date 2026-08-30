"""Professional interactive summary for the integrated ``--all`` flow."""

from __future__ import annotations

from types import SimpleNamespace

from neocortex.api.cli.cli_reporting import (
    _print_audio_report,
    _print_office_report,
    print_professional_summary,
)


def _route(**values: int) -> SimpleNamespace:
    defaults = {
        "candidates": 0,
        "processed": 0,
        "cache_hits": 0,
        "cached_errors": 0,
        "errors": 0,
        "review_candidates": 0,
        "catalog_review_required": 0,
    }
    defaults.update(values)
    return SimpleNamespace(**defaults)


def test_professional_summary_includes_every_all_route_and_semantic(capsys) -> None:
    semantic_summary = SimpleNamespace(
        done=20,
        pending=0,
        leased=0,
        errors=0,
        stale=0,
    )
    semantic_result = SimpleNamespace(
        sources=("pdf", "docx", "audio"),
        items_staged=12,
        chunks_staged=20,
        new_jobs_staged=4,
        errors=0,
        stale=0,
        incomplete=0,
        truncated=False,
        generations=(
            SimpleNamespace(
                reused=16,
                embedded=4,
                summary=semantic_summary,
            ),
        ),
    )
    actions = SimpleNamespace(
        errors=0,
        apply_actions=False,
        duplicates_trashed=0,
        files_renamed=0,
        empty_directories_trashed=0,
        types_detected=80,
        unknown_types=20,
    )
    result = SimpleNamespace(
        run_id=78,
        scan=SimpleNamespace(files_seen=100, errors=0),
        dedup_plan=SimpleNamespace(group_count=3, reclaimable_bytes=2048),
        actions=actions,
        inventory_mode="full",
        organization_plan=None,
        organization_apply=None,
        route_results={},
        pdf=_route(candidates=10, processed=10, cache_hits=9),
        docx=_route(candidates=11, processed=11, cache_hits=11),
        office=_route(candidates=12, processed=12, cache_hits=12),
        audio=_route(candidates=13, processed=13, cache_hits=13),
        image=_route(candidates=14, processed=14, cache_hits=14),
        code=_route(candidates=15, processed=15, cache_hits=15),
    )
    args = SimpleNamespace(all=True)

    print_professional_summary(
        result,
        args,
        semantic_results=(("text", semantic_result),),
        semantic_exit_code=0,
    )

    output = capsys.readouterr().out
    for label in ("PDF", "DOCX", "Office", "Audio", "Imágenes", "Código"):
        assert label in output
    assert "Semantic" in output
    assert "PUBLICADO" in output
    assert "simulación segura" in output
    assert "inventario completo" in output.lower()

    semantic_result.truncated = True
    print_professional_summary(
        result,
        args,
        semantic_results=(("text", semantic_result),),
        semantic_exit_code=0,
    )
    assert "REANUDABLE" in capsys.readouterr().out


def test_raw_report_keeps_office_and_audio_for_piped_output(capsys) -> None:
    shared = {
        "candidate_pool": 2,
        "candidates": 2,
        "processed": 2,
        "cache_hits": 1,
        "cached_errors": 0,
        "errors": 0,
        "review_candidates": 0,
        "deletion_candidates": 0,
        "retryable_errors": 0,
        "catalog_candidates": 2,
        "catalog_classified": 1,
        "catalog_cache_hits": 1,
        "catalog_review_required": 0,
        "catalog_errors": 0,
    }
    result = SimpleNamespace(
        office=SimpleNamespace(**shared, extracted=1),
        audio=SimpleNamespace(
            **shared,
            transcribed=1,
            no_speech=0,
            transcript_chars=120,
            transcript_segments=3,
        ),
    )

    _print_office_report(result)
    _print_audio_report(result)

    output = capsys.readouterr().out
    assert "route=office" in output
    assert "route=audio" in output


def test_professional_summary_explains_semantic_skip_after_framework_failure(
    capsys,
) -> None:
    result = SimpleNamespace(
        run_id=9,
        actions=SimpleNamespace(errors=1),
        organization_plan=None,
        organization_apply=None,
        route_results={},
    )

    print_professional_summary(
        result,
        SimpleNamespace(all=True),
        semantic_attempted=False,
    )

    assert "OMITIDO POR INCIDENCIAS PREVIAS" in capsys.readouterr().out
