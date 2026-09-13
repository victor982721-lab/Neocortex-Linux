"""Guard-focused tests for the future SEM-A05 layout benchmark.

The root agent will place this file at
``tests/test_semantic_text_chunk_layout_benchmark.py`` when the A05 epoch is
started. These tests exercise guards and synthetic result maps, plus one small
SQLite input-feed fixture; they are not an A/B benchmark.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import re
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest


BENCHMARK_RELATIVE_PATH = Path("benchmarks/semantic_text_chunk_layout_benchmark.py")


def _future_benchmark_path() -> Path:
    """Resolve the stable repository path after root relocates this test."""

    return Path(__file__).resolve().parents[1] / BENCHMARK_RELATIVE_PATH


@pytest.fixture()
def benchmark_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Load only the future benchmark module, without product initialization."""

    path = _future_benchmark_path()
    if not path.is_file():
        pytest.fail("SEM-A05 benchmark is missing at its stable repository path")
    original_environment = dict(os.environ)
    original_tempdir = tempfile.tempdir
    try:
        artifact_root = tmp_path / "fixture-artifacts"
        artifact_root.mkdir()
        lab_root = artifact_root / "lab"
        lab_root.mkdir()
        source_root = tmp_path / "fixture-source"
        source_root.mkdir()
        monkeypatch.setenv("NEOCORTEX_AUDIT_LAB_ROOT", str(lab_root))
        monkeypatch.setenv("NEOCORTEX_AUDIT_ARTIFACT_ROOT", str(artifact_root))
        monkeypatch.setenv("NEOCORTEX_AUDIT_SOURCE_ROOT", str(source_root))
        module_name = "_semantic_text_chunk_layout_benchmark_test_subject"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            pytest.fail("could not create an importlib spec for the benchmark")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        yield module
    finally:
        if "module_name" in locals():
            sys.modules.pop(module_name, None)
        os.environ.clear()
        os.environ.update(original_environment)
        tempfile.tempdir = original_tempdir


def test_private_environment_maps_home_xdg_and_preserves_lab_marker(
    benchmark_module,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_root = tmp_path / "artifacts"
    lab_root = artifact_root / "lab"
    lab_root.mkdir(parents=True)
    run_root = lab_root / "private-run"
    source_root = tmp_path / "source"
    source_root.mkdir()
    marker = str(lab_root)
    monkeypatch.setenv("NEOCORTEX_AUDIT_LAB_ROOT", marker)
    monkeypatch.setenv("NEOCORTEX_AUDIT_ARTIFACT_ROOT", str(artifact_root))
    monkeypatch.setenv("NEOCORTEX_AUDIT_SOURCE_ROOT", str(source_root))
    monkeypatch.setenv("NEOCORTEX_CORPUS_ROOT", "/caller-owned/corpus")

    benchmark_module._private_environment(run_root)

    for name in (
        "HOME",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "XDG_RUNTIME_DIR",
        "TMPDIR",
    ):
        mapped = Path(os.environ[name])
        assert benchmark_module._path_is_within(mapped, run_root)
        assert mapped.is_dir()
    assert os.environ["NEOCORTEX_AUDIT_LAB_ROOT"] == marker
    assert "NEOCORTEX_CORPUS_ROOT" not in os.environ


def test_safe_path_rejects_checkout_product_state_and_symlink_components(
    benchmark_module,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_root = tmp_path / "artifacts"
    lab_root = artifact_root / "lab"
    lab_root.mkdir(parents=True)
    monkeypatch.setenv("NEOCORTEX_AUDIT_LAB_ROOT", str(lab_root))
    monkeypatch.setenv("NEOCORTEX_AUDIT_ARTIFACT_ROOT", str(artifact_root))
    repository = tmp_path / "repo"
    repository.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    with pytest.raises(benchmark_module.BenchmarkConfigurationError):
        benchmark_module._safe_path(
            repository / "inside",
            label="candidate",
            repository_root=repository,
        )

    product_path = Path.home() / ".config" / "Neocortex" / "candidate"
    with pytest.raises(benchmark_module.BenchmarkConfigurationError):
        benchmark_module._safe_path(
            product_path,
            label="candidate",
            repository_root=repository,
        )

    real = scratch / "real"
    real.mkdir()
    alias = scratch / "alias"
    alias.symlink_to(real, target_is_directory=True)
    with pytest.raises(benchmark_module.BenchmarkConfigurationError):
        benchmark_module._safe_path(
            alias / "output.json",
            label="candidate",
            repository_root=repository,
        )


def test_source_path_rejects_explicit_corpus_boundary(
    benchmark_module,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    source = corpus / "semantic.sqlite3"
    source.write_bytes(b"fixture-not-opened")
    repository = tmp_path / "repo"
    repository.mkdir()
    monkeypatch.setenv("NEOCORTEX_CORPUS_ROOT", str(corpus))
    monkeypatch.setenv("NEOCORTEX_AUDIT_SOURCE_ROOT", str(corpus))

    with pytest.raises(benchmark_module.BenchmarkConfigurationError, match="NEOCORTEX_CORPUS_ROOT"):
        benchmark_module._source_path(source, repository_root=repository)


def test_source_path_requires_authorized_root_and_exact_filename(
    benchmark_module,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lab_root = Path(os.environ["NEOCORTEX_AUDIT_LAB_ROOT"])
    source_root = tmp_path / "authorized-source"
    source_root.mkdir()
    authorized = source_root / "semantic.sqlite3"
    authorized.write_bytes(b"fixture-not-opened")
    wrong_name = source_root / "other.sqlite3"
    wrong_name.write_bytes(b"fixture-not-opened")
    outside = tmp_path / "outside.sqlite3"
    outside.write_bytes(b"fixture-not-opened")
    repository = tmp_path / "repo"
    repository.mkdir()
    in_checkout = repository / "semantic.sqlite3"
    in_checkout.write_bytes(b"fixture-not-opened")
    monkeypatch.setenv("NEOCORTEX_AUDIT_LAB_ROOT", str(lab_root))
    monkeypatch.setenv("NEOCORTEX_AUDIT_SOURCE_ROOT", str(source_root))

    assert benchmark_module._source_path(authorized, repository_root=repository).name == "semantic.sqlite3"
    with pytest.raises(benchmark_module.BenchmarkConfigurationError):
        benchmark_module._source_path(wrong_name, repository_root=repository)
    with pytest.raises(benchmark_module.BenchmarkConfigurationError):
        benchmark_module._source_path(outside, repository_root=repository)
    with pytest.raises(benchmark_module.BenchmarkConfigurationError):
        benchmark_module._source_path(in_checkout, repository_root=repository)


def test_output_path_is_new_and_refuses_reuse(
    benchmark_module,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_root = tmp_path / "artifacts"
    lab_root = artifact_root / "lab"
    lab_root.mkdir(parents=True)
    monkeypatch.setenv("NEOCORTEX_AUDIT_LAB_ROOT", str(lab_root))
    monkeypatch.setenv("NEOCORTEX_AUDIT_ARTIFACT_ROOT", str(artifact_root))
    repository = tmp_path / "repo"
    repository.mkdir()
    temp_root = lab_root / "scratch"
    temp_root.mkdir()
    reports = artifact_root / "reports"
    reports.mkdir()
    output = reports / "layout.json"

    selected = benchmark_module._output_path(
        output,
        repository_root=repository,
        temp_root=temp_root,
    )
    assert selected == output
    output.write_text("{}\n", encoding="utf-8")
    with pytest.raises(benchmark_module.BenchmarkConfigurationError):
        benchmark_module._output_path(
            output,
            repository_root=repository,
            temp_root=temp_root,
        )


def test_output_path_cannot_escape_inherited_artifact_root(
    benchmark_module,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_root = tmp_path / "artifacts"
    lab_root = artifact_root / "lab"
    lab_root.mkdir(parents=True)
    monkeypatch.setenv("NEOCORTEX_AUDIT_LAB_ROOT", str(lab_root))
    monkeypatch.setenv("NEOCORTEX_AUDIT_ARTIFACT_ROOT", str(artifact_root))
    repository = tmp_path / "repo"
    repository.mkdir()
    temp_root = lab_root / "scratch"
    temp_root.mkdir()

    with pytest.raises(benchmark_module.BenchmarkConfigurationError):
        benchmark_module._output_path(
            tmp_path / "outside-artifacts" / "layout.json",
            repository_root=repository,
            temp_root=temp_root,
        )


def test_temp_root_cannot_escape_audit_lab(
    benchmark_module,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_root = tmp_path / "artifacts"
    lab_root = artifact_root / "lab"
    lab_root.mkdir(parents=True)
    source_root = lab_root / "source"
    source_root.mkdir()
    monkeypatch.setenv("NEOCORTEX_AUDIT_LAB_ROOT", str(lab_root))
    monkeypatch.setenv("NEOCORTEX_AUDIT_ARTIFACT_ROOT", str(artifact_root))
    monkeypatch.setenv("NEOCORTEX_AUDIT_SOURCE_ROOT", str(source_root))
    repository = tmp_path / "repo"
    repository.mkdir()

    with pytest.raises(benchmark_module.BenchmarkConfigurationError):
        benchmark_module._effective_temp_parent(
            tmp_path / "outside-lab",
            repository_root=repository,
        )


def test_missing_lab_admission_fails_before_temp_effect(
    benchmark_module,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    candidate = tmp_path / "candidate-lab"
    monkeypatch.delenv("NEOCORTEX_AUDIT_LAB_ROOT", raising=False)
    monkeypatch.delenv("NEOCORTEX_AUDIT_ARTIFACT_ROOT", raising=False)
    monkeypatch.delenv("NEOCORTEX_AUDIT_SOURCE_ROOT", raising=False)

    with pytest.raises(benchmark_module.BenchmarkConfigurationError):
        benchmark_module._effective_temp_parent(candidate, repository_root=repository)
    assert not candidate.exists()


@pytest.mark.parametrize(
    "extra",
    (
        ("--limit", "0"),
        ("--limit", "100001"),
        ("--batch-size", "0"),
        ("--batch-size", "1025"),
        ("--read-repeats", "11"),
        ("--timeout-seconds", "nan"),
    ),
)
def test_cli_rejects_cardinality_and_timeout_caps(benchmark_module, extra: tuple[str, str]) -> None:
    base = (
        "--source-db",
        "/private/fixture.sqlite3",
        "--output",
        "/private/layout.json",
    )
    with pytest.raises(SystemExit):
        benchmark_module._parse_args((*base, *extra))


def test_cli_accepts_canary_and_comparable_transaction_options(benchmark_module) -> None:
    args = benchmark_module._parse_args(
        (
            "--source-db",
            "/private/fixture.sqlite3",
            "--output",
            "/private/layout.json",
            "--limit",
            "100",
            "--batch-size",
            "32",
            "--transaction-mode",
            "one_run",
        )
    )
    assert args.limit == 100
    assert args.batch_size == 32
    assert args.transaction_mode == "one_run"


def test_rowid_candidate_requires_explicit_text_not_null(benchmark_module) -> None:
    ddl = "CREATE TABLE text_chunks(chunk_id TEXT PRIMARY KEY, text_zlib BLOB) WITHOUT ROWID"
    candidate = benchmark_module._remove_without_rowid(ddl)
    assert "without rowid" not in candidate.casefold()
    normalized = benchmark_module._normalize_sql(candidate)
    assert re.search(
        r"chunk_id\s+text\s+(?:primary\s+key\s+)?not\s+null|"
        r"chunk_id\s+text\s+not\s+null\s+primary\s+key",
        normalized,
    )
    with pytest.raises(benchmark_module.BenchmarkExecutionError):
        benchmark_module._remove_without_rowid(
            "CREATE TABLE text_chunks(chunk_id TEXT PRIMARY KEY)"
        )


def test_upsert_sql_carries_every_non_key_column_without_embedded_blob(
    benchmark_module,
) -> None:
    columns = ("chunk_id", "item_id", "text_zlib", "content_bytes")
    sql = benchmark_module._insert_sql("text_chunks", columns, upsert=True)
    assert sql.count("?") == len(columns)
    assert 'ON CONFLICT("chunk_id") DO UPDATE SET' in sql
    for column in columns[1:]:
        assert f'"{column}"=excluded."{column}"' in sql
    assert "payload" not in sql


def test_raw_tuple_digest_changes_for_any_column_including_blob(benchmark_module) -> None:
    columns = ("chunk_id", "text_zlib", "content_bytes")

    def digest(row: tuple[object, ...]) -> str:
        value = benchmark_module._tuple_digest_header("text_chunks", columns)
        benchmark_module._update_tuple_digest(value, row)
        return value.hexdigest()

    original = ("chunk:1", b"compressed-bytes", 17)
    assert digest(original) != digest(("chunk:1", b"compressed-bytez", 17))
    assert digest(original) != digest(("chunk:1", b"compressed-bytes", 18))


def _passing_case(*, case_name: str, rowid_layout: bool) -> dict[str, object]:
    return {
        "case": case_name,
        "rowid_layout": rowid_layout,
        "logical_fixture": {
            "rows": 3,
            "raw_tuple_hash_equal": True,
            "row_count_equal": True,
            "initial_raw_tuple_hash_equal": True,
            "initial_row_count_equal": True,
            "parent_hash_equal": True,
            "parent_count_equal": True,
            "no_decompress_or_recompress": True,
        },
        "integrity": {
            "truncated": False,
            "integrity_check_ok": True,
            "quick_check_ok": True,
            "foreign_key_error_count": 0,
        },
        "layout": {
            "ddl_matches_expected": True,
            "rowid_candidate_only_change": True,
            "foreign_key_matches_source": True,
            "explicit_index_count_matches_source": True,
            "without_rowid": not rowid_layout,
        },
        "storage": {"dbstat_available": True},
        "pk_semantics": {
            "null_rejected": True,
            "duplicate_rejected": True,
            "upsert_conflict_preserved": True,
            "rolled_back": True,
        },
        "replay": (
            {"conflict_passes": 0, "refresh": None}
            if case_name == "T0"
            else {
                "conflict_passes": 1,
                "conflict_rows": 3,
                "expected_refresh_rows": 3,
                "expected_refresh_groups": 1,
                "refresh": {
                    "rows_touched": 3,
                    "groups": 1,
                    "stale_deactivation_rows": 0,
                    "source_preserving_assignments": True,
                },
            }
        ),
    }


@pytest.mark.parametrize(
    ("case_name", "rowid_layout"),
    (("T0", False), ("T1", False), ("T2", True)),
)
def test_case_gate_accepts_only_complete_fixture_equivalence(
    benchmark_module,
    case_name: str,
    rowid_layout: bool,
) -> None:
    assert benchmark_module._check_case_result(
        _passing_case(case_name=case_name, rowid_layout=rowid_layout)
    ) == []


@pytest.mark.parametrize(
    ("section", "key"),
    (
        ("logical_fixture", "raw_tuple_hash_equal"),
        ("logical_fixture", "parent_hash_equal"),
        ("integrity", "foreign_key_error_count"),
        ("layout", "foreign_key_matches_source"),
        ("layout", "rowid_candidate_only_change"),
    ),
)
def test_case_gate_rejects_tuple_fk_or_layout_mismatch(
    benchmark_module,
    section: str,
    key: str,
) -> None:
    result = _passing_case(case_name="T2", rowid_layout=True)
    if key == "foreign_key_error_count":
        result[section][key] = 1  # type: ignore[index]
    else:
        result[section][key] = False  # type: ignore[index]
    reasons = benchmark_module._check_case_result(result)
    assert reasons


def test_fence_serializer_exposes_only_identity_evidence(benchmark_module) -> None:
    identity = SimpleNamespace(
        device=1,
        inode=2,
        mode=0o600,
        size=3,
        mtime_ns=4,
        ctime_ns=5,
    )
    before = benchmark_module._fence_payload(
        SimpleNamespace(main=identity, sidecars=())
    )
    changed = benchmark_module._fence_payload(
        SimpleNamespace(
            main=SimpleNamespace(**{**identity.__dict__, "size": 4}),
            sidecars=(),
        )
    )
    assert before["main"]["size"] == 3  # type: ignore[index]
    assert before != changed
    assert "path" not in before


def test_static_source_fence_hash_symbols_are_present(
    benchmark_module,
) -> None:
    source = Path(benchmark_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(benchmark_module.__file__))
    names = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "capture_sqlite_immutable_fence" in names
    assert "_hash_file" in names
    assert "SQLiteReadSession" in {
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
    }
    assert "source_unchanged" in {
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
    }


def test_future_benchmark_uses_runtime_admissions_not_audit_date_literals(
    benchmark_module,
) -> None:
    source = Path(benchmark_module.__file__).read_text(encoding="utf-8")
    assert "NEOCORTEX_AUDIT_LAB_ROOT" in source
    assert "NEOCORTEX_AUDIT_ARTIFACT_ROOT" in source
    assert "NEOCORTEX_AUDIT_SOURCE_ROOT" in source
    assert "2026-09-12" not in source


def test_static_main_exception_shape_does_not_name_base_exception(
    benchmark_module,
) -> None:
    source = Path(benchmark_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(benchmark_module.__file__))
    main = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "main"
    )
    caught_base_exception = [
        handler
        for handler in ast.walk(main)
        if isinstance(handler, ast.ExceptHandler)
        and isinstance(handler.type, ast.Name)
        and handler.type.id == "BaseException"
    ]
    assert not caught_base_exception


def test_readonly_admission_anchors_can_contain_private_home_and_frozen_source(
    benchmark_module,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts = tmp_path / "artifacts"
    lab = artifacts / "runs" / "one"
    home = lab / "home"
    repository = artifacts / "frozen-source"
    source_root = tmp_path / "preserved-snapshot"
    for directory in (home, repository, source_root):
        directory.mkdir(parents=True)
    source = source_root / "semantic.sqlite3"
    source.write_bytes(b"preflight-only; never opened as SQLite")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("NEOCORTEX_AUDIT_LAB_ROOT", str(lab))
    monkeypatch.setenv("NEOCORTEX_AUDIT_ARTIFACT_ROOT", str(artifacts))
    monkeypatch.setenv("NEOCORTEX_AUDIT_SOURCE_ROOT", str(source_root))

    assert benchmark_module._audit_roots(repository_root=repository) == (
        lab,
        artifacts,
        source_root,
    )
    assert benchmark_module._source_path(source, repository_root=repository) == source
    with pytest.raises(benchmark_module.BenchmarkConfigurationError, match="checkout"):
        benchmark_module._output_path(
            repository / "must-not-write.json",
            repository_root=repository,
            temp_root=lab,
        )
    with pytest.raises(benchmark_module.BenchmarkConfigurationError, match="installed"):
        benchmark_module._safe_path(
            home / ".local/state/Neocortex/forbidden",
            label="scratch",
            repository_root=repository,
        )


@pytest.mark.parametrize(
    ("section", "key", "value", "reason"),
    (
        ("logical_fixture", "initial_raw_tuple_hash_equal", False, "initial_raw_tuple_hash_equal"),
        ("replay", "conflict_rows", 0, "conflict_row_count"),
        ("integrity", "truncated", True, "integrity_truncated"),
        ("layout", "without_rowid", None, "without_rowid_unavailable"),
        ("pk_semantics", "null_rejected", False, "pk_null_rejected"),
    ),
)
def test_case_gate_rejects_missing_work_even_when_final_tuple_hash_is_equal(
    benchmark_module, section: str, key: str, value: object, reason: str
) -> None:
    result = _passing_case(case_name="T2", rowid_layout=True)
    assert isinstance(result[section], dict)
    result[section][key] = value
    assert reason in benchmark_module._check_case_result(result)


def test_captured_tuple_feed_uses_source_indexes_for_grouped_replay(
    benchmark_module, tmp_path: Path
) -> None:
    indexes = (
        "CREATE INDEX text_chunks_item_active_idx ON text_chunks"
        "(item_id,active,chunking_signature,ordinal)",
        "CREATE INDEX text_chunks_refresh_idx ON text_chunks"
        "(item_id,chunking_signature,refresh_token)",
    )
    connection = benchmark_module._create_fixture_database(
        tmp_path / "captured-tuples.sqlite3",
        parent_ddl="CREATE TABLE semantic_items(item_id TEXT PRIMARY KEY) WITHOUT ROWID",
        chunk_ddl=(
            "CREATE TABLE text_chunks(chunk_id TEXT PRIMARY KEY,item_id TEXT NOT NULL,"
            "active INTEGER NOT NULL,chunking_signature TEXT NOT NULL,"
            "ordinal INTEGER NOT NULL,refresh_token TEXT NOT NULL) WITHOUT ROWID"
        ),
        index_ddls=indexes,
        page_size=4096,
    )
    try:
        assert {name for name, _sql in benchmark_module._index_ddls(connection, "text_chunks")} == {
            "text_chunks_item_active_idx", "text_chunks_refresh_idx"
        }
        plan = [
            str(row[3])
            for row in connection.execute(
                "EXPLAIN QUERY PLAN SELECT chunk_id FROM text_chunks "
                "WHERE item_id=? AND chunking_signature=? AND active=1 ORDER BY chunk_id",
                ("item", "signature"),
            )
        ]
        assert any("SEARCH text_chunks" in row for row in plan), plan
        assert not any("SCAN text_chunks" in row for row in plan), plan
    finally:
        connection.close()
