"""Functional defaults reporting never upgrades proposals or missing coverage."""

from __future__ import annotations

from types import SimpleNamespace

from neocortex.api.cli.cli_reporting import (
    _print_action_report,
    _print_catalog_reports,
    has_strict_route_errors,
    print_professional_summary,
)


def test_unavailable_route_cannot_look_complete(capsys) -> None:
    result = SimpleNamespace(
        run_id=55, actions=_actions(), route_results={},
        route_failures={"audio": "AudioRuntimeUnavailableError: local model is absent"},
        organization_plan=None, organization_apply=None,
    )
    print_professional_summary(result, SimpleNamespace(all=True, show_groups=0))
    output = capsys.readouterr().out
    assert "COMPLETADA CON INCIDENCIAS" in output and "NO DISPONIBLE" in output
    assert "local model is absent" in output


def test_catalog_loss_and_partial_zip_are_strict_incomplete(capsys) -> None:
    for field in ("catalog_source_missing", "catalog_source_stale", "containers_partial", "protected"):
        summary = SimpleNamespace(**{field: 1})
        assert has_strict_route_errors(SimpleNamespace(route_results={"archive": summary}))
    result = SimpleNamespace(route_results={"video": SimpleNamespace(
        catalog_complete=False, catalog_candidates=1, catalog_errors=0,
        catalog_source_missing=1,
    )})
    _print_catalog_reports(result)
    output = capsys.readouterr().out
    assert "ROUTE_CATALOG route=video complete=0" in output
    assert "catalog_source_missing=1" in output


def test_ready_partial_semantic_head_is_not_reported_as_published(capsys) -> None:
    summary = SimpleNamespace(
        generation_id=17, status="ready_partial", done=1, pending=0, leased=0,
        errors=0, stale=0,
    )
    semantic = SimpleNamespace(
        complete=False, generations=(SimpleNamespace(summary=summary, embedded=0, reused=1),),
        sources=("video",), items_staged=0, chunks_staged=0, new_jobs_staged=0,
        errors=0, stale=0, incomplete=0, truncated=False,
    )
    result = SimpleNamespace(run_id=57, actions=_actions(), route_results={},
                             organization_plan=None, organization_apply=None)
    print_professional_summary(result, SimpleNamespace(all=True, show_groups=0),
                               semantic_results=(("text", semantic),))
    output = capsys.readouterr().out
    assert "REANUDABLE" in output and "COMPLETADA CON INCIDENCIAS" in output
    assert "PUBLICADO" not in output


def test_partial_code_has_an_explicit_raw_exit_cause(capsys) -> None:
    from neocortex.api.cli.cli_reporting import _print_code_report
    from neocortex.code.code_contracts import CodeRouteSummary

    code = CodeRouteSummary(candidates=1, partial=1, errors=0)
    result = SimpleNamespace(code=code, route_results={"code": code})
    _print_code_report(result)
    _print_catalog_reports(result)
    output = capsys.readouterr().out
    assert "code_partial=1" in output and 'issues={"partial":1}' in output
    assert "next_action=inspect_owner_diagnostics_in_same_state" in output


def test_code_and_audio_replay_are_explicit_without_retranscription(capsys) -> None:
    from neocortex.code.code_contracts import CodeRouteSummary
    from neocortex.capabilities.formats.audio.models import AudioRouteSummary

    result = SimpleNamespace(route_results={
        "code": CodeRouteSummary(candidates=3, processed=0, cache_hits=3),
        "audio": AudioRouteSummary(candidates=2, processed=2, cache_hits=2, transcribed=2),
    })
    _print_catalog_reports(result)
    lines = capsys.readouterr().out.splitlines()
    for route in ("code", "audio"):
        line = next(x for x in lines if x.startswith(f"ROUTE_REPLAY route={route}"))
        assert "new_work=0 evidence=observado" in line


def _actions(*, apply_actions: bool = False, **values: int) -> SimpleNamespace:
    defaults = {
        "apply_actions": apply_actions,
        "duplicate_candidates": 2,
        "duplicates_trashed": 0,
        "duplicate_skips": 0,
        "rename_candidates": 1,
        "files_renamed": 0,
        "rename_skips": 0,
        "empty_directory_candidates": 0,
        "empty_directories_trashed": 0,
        "empty_directory_skips": 0,
        "files_checked": 3,
        "types_detected": 3,
        "unknown_types": 0,
        "type_cache_hits": 0,
        "type_cache_misses": 3,
        "type_cache_pruned": 0,
        "stale_inventory": 0,
        "errors": 0,
    }
    defaults.update(values)
    return SimpleNamespace(**defaults)


def test_raw_action_report_separates_dry_run_plans_from_effects(capsys) -> None:
    _print_action_report(
        SimpleNamespace(actions=_actions()),
        "exact",
    )

    output = capsys.readouterr().out
    assert "action_mode=dry-run" in output
    assert "planned=3" in output
    assert "applied=0" in output
    assert "action_candidates=3" in output
    assert "action_skips=0" in output


def test_raw_action_report_does_not_call_apply_attempts_planned_effects(capsys) -> None:
    _print_action_report(
        SimpleNamespace(actions=_actions(apply_actions=True)),
        "exact",
    )

    output = capsys.readouterr().out
    assert "action_mode=apply" in output
    assert "planned=0" in output
    assert "applied=0" in output
    assert "action_candidates=3" in output


def test_professional_summary_classifies_zip_audio_and_empty_selection(capsys) -> None:
    result = SimpleNamespace(
        run_id=42,
        scan=SimpleNamespace(files_seen=3, errors=0),
        dedup_plan=None,
        actions=_actions(),
        inventory_mode="full",
        organization_plan=None,
        organization_apply=None,
        route_results={},
        archive=SimpleNamespace(
            candidates=1,
            processed=1,
            cache_hits=0,
            cached_errors=0,
            containers_partial=1,
            safety_issues=2,
            errors=0,
            processing_provenance={"owner": "fixture"},
        ),
        audio=SimpleNamespace(
            candidates=1,
            processed=1,
            cache_hits=0,
            cached_errors=0,
            no_speech=1,
            errors=0,
            processing_provenance={"owner": "fixture"},
        ),
        text=SimpleNamespace(
            candidates=0,
            processed=0,
            cache_hits=0,
            cached_errors=0,
            errors=0,
            processing_provenance={"owner": "fixture"},
        ),
    )

    print_professional_summary(
        result,
        SimpleNamespace(all=False, route="archive", show_groups=0),
    )

    output = capsys.readouterr().out
    assert "parciales=1" in output
    assert "seguridad=2" in output
    assert "no_speech=1" in output
    assert "SIN CANDIDATOS" in output
    assert "planeadas" in output
    assert "aplicadas" in output
    # Archive has no catalog/FTS fields in its summary; report that gap rather
    # than printing fabricated zeros.
    assert "catalogo=no_verificado" in output
    assert "fts=no_verificado" in output


def test_professional_summary_keeps_catalog_and_derived_zeroes_distinct(capsys) -> None:
    result = SimpleNamespace(
        run_id=43,
        actions=_actions(),
        organization_plan=None,
        organization_apply=None,
        route_results={},
        pdf=SimpleNamespace(
            candidates=357,
            processed=357,
            cache_hits=357,
            cached_errors=0,
            protected=15,
            partial_documents=0,
            errors=0,
            catalog_candidates=342,
            catalog_classified=342,
            fts_pages_indexed=0,
            profiles_built=0,
            processing_provenance={"owner": "fixture"},
        ),
    )

    print_professional_summary(
        result,
        SimpleNamespace(all=False, route="pdf", show_groups=0),
    )

    output = capsys.readouterr().out
    assert "catalogo=342/357" in output
    assert "fts_pages_indexed=0" in output
    assert "profiles_built=0" in output
    assert "protected=15" in output
    assert "ATENCIÓN" in output


def test_missing_route_counters_are_not_rendered_as_zero_ok(capsys) -> None:
    result = SimpleNamespace(
        run_id=7,
        actions=_actions(),
        organization_plan=None,
        organization_apply=None,
        route_results={},
        image=SimpleNamespace(errors=0),
    )

    print_professional_summary(
        result,
        SimpleNamespace(all=False, route="image", show_groups=0),
    )

    output = capsys.readouterr().out
    assert "NO VERIFICADO" in output
    assert "no_verificado" in output
