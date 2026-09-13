"""Focused CLI contracts for the explicit Semantic exact-index adapter."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from neocortex.api.cli.cli_app import dispatch_direct
from neocortex.api.cli import cli_semantic
from neocortex.api.cli.cli_operations import DirectOperationFamily, selected_direct_operations
from neocortex.api.cli.cli_parser import build_parser
from neocortex.api.cli.cli_validation import validate_arguments


TEST_CAPABILITIES = ("inference",)
pytestmark = pytest.mark.capability("inference")


def _parse(tmp_path: Path, *arguments: str):
    return build_parser().parse_args(
        ("--state-directory", str(tmp_path), *arguments)
    )


def _validated(tmp_path: Path, *arguments: str):
    args = _parse(tmp_path, *arguments)
    validate_arguments(args)
    return args


def _search_result() -> SimpleNamespace:
    lexical = SimpleNamespace(
        ranking_name="lexical",
        availability=SimpleNamespace(value="available"),
        hits=(),
        unavailable_reason=None,
    )
    return SimpleNamespace(
        query="fixture query",
        complete=True,
        rankings=(),
        lexical_rankings=(lexical,),
        fused=(),
    )


def _handle(*, summary: object | None = None, usage: object | None = None) -> Mock:
    handle = Mock(name="exact_index_handle")
    handle.summary.return_value = summary or {
        "row_count": 24,
        "model_signature": "fixture-model",
        "text_scope": "content",
    }
    handle.usage_summary.return_value = usage or {
        "used_queries": 0,
        "fallback_queries": 0,
        "rows_scanned": 0,
        "last_fallback_reason": None,
    }
    return handle


def test_exact_index_flags_parse_and_validation_bind_only_to_their_actions(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "exact-index"
    args = _validated(
        tmp_path,
        "--semantic-exact-index-build",
        str(destination),
        "--semantic-exact-index-model",
        "fixture-model",
        "--semantic-exact-index-scope",
        "title",
    )

    assert args.semantic_exact_index_build == destination
    assert args.semantic_exact_index_model == "fixture-model"
    assert args.semantic_exact_index_scope == "title"
    assert tuple(
        operation.destination
        for operation in selected_direct_operations(
            args,
            family=DirectOperationFamily.SEMANTIC,
        )
    ) == ("semantic_exact_index_build",)


@pytest.mark.parametrize(
    ("arguments", "message"),
    (
        (
            ("--semantic-exact-index-build", "/tmp/exact-index"),
            "--semantic-exact-index-build requires a non-empty trimmed "
            "--semantic-exact-index-model",
        ),
        (
            ("--semantic-exact-index-model", "fixture-model"),
            "--semantic-exact-index-model requires --semantic-exact-index-build",
        ),
        (
            ("--semantic-exact-index-scope", "title"),
            "--semantic-exact-index-scope requires --semantic-exact-index-build",
        ),
        (
            ("--semantic-exact-index", "/tmp/exact-index"),
            "--semantic-exact-index requires --semantic-search",
        ),
    ),
)
def test_exact_index_option_validation_rejects_unbound_combinations(
    tmp_path: Path,
    arguments: tuple[str, ...],
    message: str,
) -> None:
    args = _parse(tmp_path, *arguments)
    with pytest.raises(SystemExit) as raised:
        validate_arguments(args)
    assert str(raised.value) == message


def test_exact_index_build_dispatches_once_and_closes_on_success(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    destination = tmp_path / "exact-index"
    args = _validated(
        tmp_path,
        "--semantic-exact-index-build",
        str(destination),
        "--semantic-exact-index-model",
        "fixture-model",
    )
    handle = _handle()

    with patch(
        "neocortex.semantic.semantic_exact_index.prepare_exact_index",
        return_value=handle,
    ) as prepare:
        assert dispatch_direct(args) == 0

    prepare.assert_called_once_with(
        tmp_path / "semantic.sqlite3",
        destination,
        model_signature="fixture-model",
        text_scope="content",
        max_rows=500_000,
        max_total_bytes=4_000_000_000,
        cancellation_check=None,
    )
    handle.close.assert_called_once_with()
    assert "SEMANTIC_EXACT_INDEX_BUILD" in capsys.readouterr().out


def test_exact_index_build_closes_handle_when_summary_fails(tmp_path: Path) -> None:
    args = _validated(
        tmp_path,
        "--semantic-exact-index-build",
        str(tmp_path / "exact-index"),
        "--semantic-exact-index-model",
        "fixture-model",
    )
    handle = _handle()
    handle.summary.side_effect = RuntimeError("summary unavailable")

    with patch(
        "neocortex.semantic.semantic_exact_index.prepare_exact_index",
        return_value=handle,
    ):
        assert dispatch_direct(args) == 2

    handle.close.assert_called_once_with()


@pytest.mark.parametrize("cancelled", (False, True))
def test_exact_index_build_adapts_boolean_cancellation_and_preserves_interrupt(
    tmp_path: Path,
    cancelled: bool,
) -> None:
    args = _validated(
        tmp_path,
        "--semantic-exact-index-build",
        str(tmp_path / "exact-index"),
        "--semantic-exact-index-model",
        "fixture-model",
    )
    args._semantic_cancellation_check = lambda: cancelled
    handle = _handle()
    checkpoint = None

    def prepare(*_positional: object, cancellation_check=None, **_keyword: object):
        nonlocal checkpoint
        assert callable(cancellation_check)
        checkpoint = cancellation_check
        return handle

    def summary():
        assert callable(checkpoint)
        assert checkpoint() is None
        return {
            "row_count": 24,
            "model_signature": "fixture-model",
            "text_scope": "content",
        }

    handle.summary.side_effect = summary

    with patch(
        "neocortex.semantic.semantic_exact_index.prepare_exact_index",
        side_effect=prepare,
    ):
        if cancelled:
            with pytest.raises(KeyboardInterrupt):
                dispatch_direct(args)
        else:
            assert dispatch_direct(args) == 0

    handle.close.assert_called_once_with()


def test_non_build_semantic_operation_does_not_prepare_an_exact_index(
    tmp_path: Path,
) -> None:
    args = _validated(tmp_path, "--semantic-status")
    with patch("neocortex.semantic.semantic_exact_index.prepare_exact_index") as prepare:
        assert dispatch_direct(args) == 0
    prepare.assert_not_called()


def test_exact_index_search_opens_once_reports_cold_validation_and_closes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    directory = tmp_path / "exact-index"
    args = _validated(
        tmp_path,
        "--semantic-search",
        "fixture query",
        "--semantic-exact-index",
        str(directory),
        "--semantic-max-vectors",
        "50000",
    )
    handle = _handle(
        usage={
            "used_queries": 1,
            "fallback_queries": 0,
            "rows_scanned": 24,
            "last_fallback_reason": None,
        }
    )
    result = _search_result()

    with (
        patch(
            "neocortex.semantic.semantic_exact_index.open_exact_index",
            return_value=handle,
        ) as open_index,
        patch(
            "neocortex.semantic.semantic_service.search_semantic_index",
            return_value=result,
        ) as search,
        patch.object(cli_semantic, "_semantic_text_model", return_value="fixture-text-model"),
    ):
        assert dispatch_direct(args) == 0

    open_index.assert_called_once_with(
        tmp_path / "semantic.sqlite3",
        directory,
        max_rows=500_000,
        max_total_bytes=4_000_000_000,
        cancellation_check=None,
    )
    search.assert_called_once()
    assert search.call_args.kwargs["exact_index"] is handle
    assert search.call_args.kwargs["max_vectors"] == 50_000
    assert search.call_args.kwargs["local_files_only"] is True
    assert search.call_args.kwargs["model_cache"] is None
    assert "cancellation_check" not in search.call_args.kwargs
    output = capsys.readouterr().out
    assert output.count("SEMANTIC_EXACT_INDEX_OPEN") == 1
    assert output.count("SEMANTIC_EXACT_INDEX_USAGE") == 1
    assert "cold_validation=bounded_may_scan_source" in output
    handle.close.assert_called_once_with()


def test_unavailable_exact_index_falls_back_without_a_build_or_handle(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = _validated(
        tmp_path,
        "--semantic-search",
        "fixture query",
        "--semantic-exact-index",
        str(tmp_path / "missing-exact-index"),
    )
    result = _search_result()
    from neocortex.semantic.semantic_exact_index import ExactIndexUnavailable

    with (
        patch(
            "neocortex.semantic.semantic_exact_index.open_exact_index",
            side_effect=ExactIndexUnavailable("artifact_missing"),
        ),
        patch(
            "neocortex.semantic.semantic_service.search_semantic_index",
            return_value=result,
        ) as search,
        patch.object(cli_semantic, "_semantic_text_model", return_value="fixture-text-model"),
    ):
        assert dispatch_direct(args) == 0

    search.assert_called_once()
    assert "exact_index" not in search.call_args.kwargs
    output = capsys.readouterr().out
    assert "falling back to normal semantic search" in output
    assert "SEMANTIC_EXACT_INDEX_OPEN" not in output


def test_exact_index_search_closes_handle_when_search_fails(tmp_path: Path) -> None:
    args = _validated(
        tmp_path,
        "--semantic-search",
        "fixture query",
        "--semantic-exact-index",
        str(tmp_path / "exact-index"),
    )
    handle = _handle()

    with (
        patch(
            "neocortex.semantic.semantic_exact_index.open_exact_index",
            return_value=handle,
        ),
        patch(
            "neocortex.semantic.semantic_service.search_semantic_index",
            side_effect=RuntimeError("search unavailable"),
        ),
        patch.object(cli_semantic, "_semantic_text_model", return_value="fixture-text-model"),
    ):
        assert dispatch_direct(args) == 2

    handle.close.assert_called_once_with()


def test_exact_index_search_preserves_boolean_cancellation_through_open_and_search(
    tmp_path: Path,
) -> None:
    args = _validated(
        tmp_path,
        "--semantic-search",
        "fixture query",
        "--semantic-exact-index",
        str(tmp_path / "exact-index"),
    )
    calls = 0

    def cancellation() -> bool:
        nonlocal calls
        calls += 1
        return calls >= 2

    args._semantic_cancellation_check = cancellation
    handle = _handle()

    def open_index(*_positional: object, cancellation_check=None, **_keyword: object):
        assert callable(cancellation_check)
        assert cancellation_check() is None
        return handle

    def search(*_positional: object, **kwargs: object):
        checkpoint = kwargs["cancellation_check"]
        assert callable(checkpoint)
        checkpoint()
        raise AssertionError("the cancellation checkpoint should interrupt first")

    with (
        patch(
            "neocortex.semantic.semantic_exact_index.open_exact_index",
            side_effect=open_index,
        ),
        patch(
            "neocortex.semantic.semantic_service.search_semantic_index",
            side_effect=search,
        ),
        patch.object(cli_semantic, "_semantic_text_model", return_value="fixture-text-model"),
    ):
        with pytest.raises(KeyboardInterrupt):
            dispatch_direct(args)

    assert calls == 2
    handle.close.assert_called_once_with()


def test_native_search_receives_cancellation_checkpoint_without_exact_index(
    tmp_path: Path,
) -> None:
    args = _validated(tmp_path, "--semantic-search", "fixture query")
    args._semantic_cancellation_check = lambda: True

    def search(*_positional: object, **kwargs: object):
        checkpoint = kwargs["cancellation_check"]
        assert callable(checkpoint)
        checkpoint()
        raise AssertionError("the cancellation checkpoint should interrupt first")

    with (
        patch(
            "neocortex.semantic.semantic_service.search_semantic_index",
            side_effect=search,
        ) as search_operation,
        patch.object(cli_semantic, "_semantic_text_model", return_value="fixture-text-model"),
    ):
        with pytest.raises(KeyboardInterrupt):
            dispatch_direct(args)

    search_operation.assert_called_once()
    assert "exact_index" not in search_operation.call_args.kwargs
