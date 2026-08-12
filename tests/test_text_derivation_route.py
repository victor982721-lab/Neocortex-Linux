"""End-to-end TextRoute coverage for owner-local derivation receipts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

import _04_Nucleo_Operativo.text_route as text_route_module
import _04_Nucleo_Operativo.text_state as text_state_module
import _04_Nucleo_Operativo.legacy_office_worker as legacy_worker_module
from _02_Deduplicacion import FileSnapshot, snapshot_path
from _04_Nucleo_Operativo.cancellation import CancellationRequested, CancellationToken
from _04_Nucleo_Operativo.file_identity import file_key_from_snapshot
from _04_Nucleo_Operativo.locking import FrameworkRunLock
from _04_Nucleo_Operativo.route_filters import CandidateSelection
from _04_Nucleo_Operativo.text_derivation_repository import (
    TextDerivationIntegrityError,
    read_reusable_text_derivation,
    read_reusable_text_derivation_from_connection,
    read_text_document_lineage,
    validate_text_publications_from_connection,
)
from _04_Nucleo_Operativo.text_route import TextRoute, TextRouteConfig
from _04_Nucleo_Operativo.text_state import initialize_text_state, text_database
from neocortex.capability_broker import CapabilityBinaryIdentity


class _FrameworkState:
    def __init__(self, snapshot: FileSnapshot, mime: str = "text/plain") -> None:
        self.snapshot = snapshot
        self.mime = mime

    def selected_route_candidate_counts(
        self,
        _run_id: int,
        mime: str,
        max_file_bytes: int | None,
        _route_name: str,
        _selection: CandidateSelection,
    ) -> tuple[int, int]:
        if mime != self.mime:
            return 0, 0
        eligible = max_file_bytes is None or self.snapshot.size <= max_file_bytes
        return 1, int(eligible)

    def iter_selected_route_candidates(
        self,
        _run_id: int,
        mime: str,
        _route_name: str,
        _selection: CandidateSelection,
    ):
        if mime == self.mime:
            yield self.snapshot


def _route(
    state: Path,
    source: Path,
    run_id: int,
    *,
    cancellation: CancellationToken | None = None,
    max_text_chars: int = 4_000_000,
    worker_timeout_seconds: float = 60.0,
    mime: str = "text/plain",
    retry_errors: bool = False,
) -> TextRoute:
    return TextRoute(
        TextRouteConfig(
            state_path=state,
            max_text_chars=max_text_chars,
            worker_timeout_seconds=worker_timeout_seconds,
            retry_errors=retry_errors,
        ),
        _FrameworkState(snapshot_path(source), mime),
        run_id,
        cancellation=cancellation or CancellationToken(),
    )


def _scalar(connection: sqlite3.Connection, statement: str) -> int:
    return int(connection.execute(statement).fetchone()[0])


def _attempts(path: Path) -> list[sqlite3.Row]:
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute(
            "SELECT * FROM text_derivation_attempts ORDER BY recorded_ns,attempt_id"
        ).fetchall()


def test_first_execution_and_cache_hit_publish_complete_causal_receipts(
    tmp_path: Path,
) -> None:
    source = tmp_path / "fuente.txt"
    source.write_text("protección diferencial del transformador", encoding="utf-8")
    state = tmp_path / "text.sqlite3"
    snapshot = snapshot_path(source)

    first = _route(state, source, 41).run()
    second = _route(state, source, 99).run()

    assert (first.processed, first.extracted, first.cache_hits) == (1, 1, 0)
    assert (second.processed, second.extracted, second.cache_hits) == (0, 0, 1)
    attempts = _attempts(state)
    assert [(row["stage_id"], row["stage_version"]) for row in attempts] == [
        ("text.extract", "2"),
        ("text.extract", "2"),
    ]
    assert [row["execution_mode"] for row in attempts] == ["executed", "cache_hit"]
    assert [row["attempt_number"] for row in attempts] == [1, 2]
    assert [row["reproducibility_class"] for row in attempts] == [
        "environment_bound",
        "environment_bound",
    ]
    assert attempts[1]["causation_id"] == attempts[0]["receipt_id"]
    assert first.effective_processing_signatures == (str(attempts[0]["processing_signature"]),)
    assert first.processing_signature != first.effective_processing_signatures[0]

    with sqlite3.connect(state) as connection:
        assert _scalar(connection, "SELECT COUNT(*) FROM text_work_receipts") == 2
        assert _scalar(connection, "SELECT COUNT(*) FROM text_derivation_outbox") == 2
        assert _scalar(connection, "SELECT COUNT(*) FROM text_materializations") == 2
        assert _scalar(connection, "SELECT COUNT(*) FROM text_materialization_heads") == 2
        assert _scalar(connection, "SELECT COUNT(*) FROM text_derivation_output_bindings") == 4
        revision = connection.execute(
            "SELECT resource_id,fingerprint_algorithm,fingerprint FROM text_input_revisions"
        ).fetchone()
        assert revision == (
            f"resource:file:{snapshot.volume_id}:{snapshot.file_id}:{snapshot.birthtime_ns}",
            "xxh3-128",
            text_route_module.xxhash.xxh3_128_hexdigest(source.read_bytes()),
        )
        output_sets = [
            tuple(
                connection.execute(
                    "SELECT binding_name,materialization_id "
                    "FROM text_derivation_output_bindings WHERE attempt_id=? "
                    "ORDER BY binding_name",
                    (row["attempt_id"],),
                )
            )
            for row in attempts
        ]
        assert output_sets[0] == output_sets[1]
        receipts = [
            json.loads(row[0])
            for row in connection.execute(
                "SELECT receipt_json FROM text_work_receipts ORDER BY recorded_ns,receipt_id"
            )
        ]
        assert [item["stage"]["provider"] for item in receipts] == [
            "neocortex-builtin",
            "neocortex-builtin",
        ]
        assert [item["stage"]["provider_version"] for item in receipts] == [
            "text-route-v2",
            "text-route-v2",
        ]
        capability_configuration = {
            key: value
            for key, value in receipts[0]["effective_configuration"].items()
            if key.startswith("capability_")
        }
        assert {
            key: capability_configuration[key]
            for key in (
                "capability_id",
                "capability_implementation",
                "capability_policy",
                "capability_provider",
                "capability_provider_version",
                "capability_selection",
            )
        } == {
            "capability_id": "text.extract",
            "capability_implementation": "neocortex.text.builtin",
            "capability_policy": "neocortex-text-local-v1",
            "capability_provider": "neocortex-builtin",
            "capability_provider_version": "text-route-v2",
            "capability_selection": "selected:neocortex.text.builtin",
        }
        assert capability_configuration["capability_manifest_fingerprint"].startswith("sha256:")
        assert (
            receipts[0]["stage"]["implementation_digest"]
            == receipts[0]["effective_configuration"]["implementation_digest"]
        )
        assert capability_configuration["capability_readiness"].startswith("xxhash@")
        assert capability_configuration["capability_policy_fingerprint"].startswith("sha256:")
        assert capability_configuration["capability_selection_fingerprint"].startswith("sha256:")

    lineage = read_text_document_lineage(
        state,
        file_key_from_snapshot(snapshot),
    )
    assert lineage is not None
    assert lineage.attribution == "attributed"
    assert {item.materialization.kind for item in lineage.materializations} == {
        "text_fts",
        "text_representation",
    }


def test_fresh_nested_state_directory_is_created_before_route_lock(tmp_path: Path) -> None:
    source = tmp_path / "fuente.txt"
    source.write_text("estado nuevo", encoding="utf-8")
    state = tmp_path / "nested" / "state" / "text.sqlite3"

    summary = _route(state, source, 1).run()

    assert summary.extracted == 1
    assert state.is_file()
    assert state.with_suffix(state.suffix + ".route.lock").is_file()


def test_cache_lookup_reuses_the_route_connection_and_matches_path_wrapper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sources = tuple(tmp_path / f"fuente-{index}.txt" for index in range(3))
    for index, source in enumerate(sources):
        source.write_text(f"contenido reusable {index}", encoding="utf-8")
    snapshots = tuple(snapshot_path(source) for source in sources)
    state = tmp_path / "text.sqlite3"

    class ManyFrameworkState:
        def selected_route_candidate_counts(self, _run, mime, *_args):
            return (len(snapshots), len(snapshots)) if mime == "text/plain" else (0, 0)

        def iter_selected_route_candidates(self, _run, mime, *_args):
            if mime == "text/plain":
                yield from snapshots

    config = TextRouteConfig(state_path=state)
    TextRoute(config, ManyFrameworkState(), 1).run()
    first_key = file_key_from_snapshot(snapshots[0])
    with text_database(state, readonly=True) as connection:
        candidate_signature = str(
            connection.execute(
                "SELECT processing_signature FROM documents WHERE file_key=?",
                (first_key,),
            ).fetchone()[0]
        )
        via_connection = read_reusable_text_derivation_from_connection(
            connection,
            first_key,
            stage_id="text.extract",
            processing_signature=candidate_signature,
        )
    via_path = read_reusable_text_derivation(
        state,
        first_key,
        stage_id="text.extract",
        processing_signature=candidate_signature,
    )
    assert via_connection == via_path
    assert via_connection is not None

    connection_ids: set[int] = set()
    begin_connection_ids: set[int] = set()
    original = text_route_module.read_reusable_text_derivation_from_connection
    original_begin = text_route_module.begin_text_derivation_attempt_from_connection

    def tracked(connection, *args, **kwargs):
        connection_ids.add(id(connection))
        return original(connection, *args, **kwargs)

    def tracked_begin(connection, *args, **kwargs):
        begin_connection_ids.add(id(connection))
        return original_begin(connection, *args, **kwargs)

    monkeypatch.setattr(
        text_route_module,
        "read_reusable_text_derivation_from_connection",
        tracked,
    )
    monkeypatch.setattr(
        text_route_module,
        "begin_text_derivation_attempt_from_connection",
        tracked_begin,
    )
    replay = TextRoute(config, ManyFrameworkState(), 2).run()

    assert replay.cache_hits == 3
    assert connection_ids and len(connection_ids) == 1
    assert begin_connection_ids == connection_ids


def test_batch_publication_validation_has_constant_query_count(tmp_path: Path) -> None:
    sources = tuple(tmp_path / f"batch-{index}.txt" for index in range(20))
    for index, source in enumerate(sources):
        source.write_text(f"publicación validada {index}", encoding="utf-8")
    snapshots = tuple(snapshot_path(source) for source in sources)
    state = tmp_path / "text.sqlite3"

    class ManyFrameworkState:
        def selected_route_candidate_counts(self, _run, mime, *_args):
            return (len(snapshots), len(snapshots)) if mime == "text/plain" else (0, 0)

        def iter_selected_route_candidates(self, _run, mime, *_args):
            if mime == "text/plain":
                yield from snapshots

    summary = TextRoute(TextRouteConfig(state_path=state), ManyFrameworkState(), 1).run()
    assert summary.extracted == len(snapshots)
    statements: list[str] = []
    with text_database(state, readonly=True) as connection:
        publications = tuple(
            (str(row["file_key"]), str(row["revision_id"]))
            for row in connection.execute(
                "SELECT file_key,revision_id FROM documents ORDER BY file_key"
            )
        )
        connection.set_trace_callback(statements.append)
        validate_text_publications_from_connection(connection, publications)

    selects = [
        statement for statement in statements if statement.lstrip().upper().startswith("SELECT")
    ]
    # At most ten SELECTs per bounded 16-document page, never per document.
    assert len(selects) <= 20


def test_configuration_stage_and_content_changes_force_real_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "fuente.txt"
    source.write_text("contenido inicial suficientemente largo", encoding="utf-8")
    state = tmp_path / "text.sqlite3"

    baseline = _route(state, source, 1).run()
    configuration_change = _route(
        state,
        source,
        2,
        max_text_chars=10_000,
    ).run()
    monkeypatch.setattr(text_route_module, "_TEXT_EXTRACT_STAGE_VERSION", "3")
    stage_change = _route(state, source, 3, max_text_chars=10_000).run()
    source.write_text("contenido modificado con una revisión nueva", encoding="utf-8")
    content_change = _route(state, source, 4, max_text_chars=10_000).run()

    assert [
        (item.extracted, item.cache_hits)
        for item in (
            baseline,
            configuration_change,
            stage_change,
            content_change,
        )
    ] == [(1, 0)] * 4
    attempts = _attempts(state)
    assert [row["stage_version"] for row in attempts] == ["2", "2", "3", "3"]
    assert all(row["execution_mode"] == "executed" for row in attempts)
    with sqlite3.connect(state) as connection:
        assert _scalar(connection, "SELECT COUNT(*) FROM text_input_revisions") == 2
        assert _scalar(connection, "SELECT COUNT(*) FROM text_materializations") == 8
        assert _scalar(connection, "SELECT COUNT(*) FROM text_materialization_heads") == 2
        assert _scalar(connection, "SELECT COUNT(*) FROM text_work_receipts") == 4


def test_plain_text_cache_ignores_unrelated_office_worker_configuration(
    tmp_path: Path,
) -> None:
    source = tmp_path / "fuente.txt"
    source.write_text("contenido de texto sin Office", encoding="utf-8")
    state = tmp_path / "text.sqlite3"

    first = _route(state, source, 1, worker_timeout_seconds=60.0).run()
    replay = _route(state, source, 2, worker_timeout_seconds=7.0).run()

    assert (first.extracted, first.cache_hits) == (1, 0)
    assert (replay.extracted, replay.cache_hits) == (0, 1)
    attempts = _attempts(state)
    assert attempts[0]["processing_signature"] == attempts[1]["processing_signature"]
    configuration = json.loads(attempts[0]["effective_configuration_json"])
    runtime = json.loads(attempts[0]["runtime_json"])
    assert "worker_timeout_seconds" not in configuration
    assert "worker_memory_bytes" not in configuration
    assert "soffice" not in runtime


def test_text_provider_selection_is_per_work_and_abstains_without_legacy_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(text_route_module.shutil, "which", lambda _name: None)
    plain_source = tmp_path / "fuente.txt"
    plain_source.write_text("texto local sin LibreOffice", encoding="utf-8")
    plain_state = tmp_path / "plain.sqlite3"

    plain = _route(plain_state, plain_source, 1).run()

    assert (plain.extracted, plain.errors) == (1, 0)
    with sqlite3.connect(plain_state) as connection:
        plain_receipt = json.loads(
            connection.execute("SELECT receipt_json FROM text_work_receipts").fetchone()[0]
        )
    assert plain_receipt["stage"]["provider"] == "neocortex-builtin"

    legacy_source = tmp_path / "legado.doc"
    legacy_source.write_bytes(b"legacy office payload")
    legacy_state = tmp_path / "legacy.sqlite3"

    legacy = _route(
        legacy_state,
        legacy_source,
        2,
        mime="application/msword",
    ).run()

    assert (legacy.processed, legacy.extracted, legacy.errors) == (1, 0, 1)
    with sqlite3.connect(legacy_state) as connection:
        legacy_receipt = json.loads(
            connection.execute("SELECT receipt_json FROM text_work_receipts").fetchone()[0]
        )
        assert _scalar(connection, "SELECT COUNT(*) FROM text_materializations") == 0
        assert _scalar(connection, "SELECT COUNT(*) FROM text_materialization_heads") == 0
    assert legacy_receipt["stage"]["provider"] is None
    assert legacy_receipt["outcome"] == "failed"
    assert legacy_receipt["failure"]["reason_code"] == ("TextCapabilityUnavailableError")
    assert legacy_receipt["effective_configuration"]["capability_selection"] == (
        "unavailable:text.extract"
    )
    assert (
        "legacy_office_extractor_unavailable" in legacy_receipt["failure"]["details"]["rejections"]
    )


def test_mime_branch_change_invalidates_cache_and_records_effective_adapter(
    tmp_path: Path,
) -> None:
    source = tmp_path / "fuente.txt"
    source.write_text(
        "<html><script>oculto()</script><body>contenido visible</body></html>",
        encoding="utf-8",
    )
    state = tmp_path / "text.sqlite3"

    plain = _route(state, source, 1, mime="text/plain").run()
    html_source = source.with_suffix(".html")
    source.rename(html_source)
    html = _route(state, html_source, 2, mime="text/html").run()
    replay = _route(state, html_source, 3, mime="text/html").run()

    assert (plain.extracted, plain.cache_hits) == (1, 0)
    assert (html.extracted, html.cache_hits) == (1, 0)
    assert (replay.extracted, replay.cache_hits) == (0, 1)
    attempts = _attempts(state)
    assert [row["execution_mode"] for row in attempts] == [
        "executed",
        "executed",
        "cache_hit",
    ]
    assert attempts[0]["processing_signature"] != attempts[1]["processing_signature"]
    assert attempts[1]["processing_signature"] == attempts[2]["processing_signature"]
    plain_configuration = json.loads(attempts[0]["effective_configuration_json"])
    html_configuration = json.loads(attempts[1]["effective_configuration_json"])
    assert (
        plain_configuration
        | {
            "declared_mime": "text/plain",
            "extractor_adapter": "strict_text_decode",
            "output_content_kind": "txt",
        }
        == plain_configuration
    )
    assert (
        html_configuration
        | {
            "declared_mime": "text/html",
            "extractor_adapter": "strict_text_decode+html_visible_text",
            "output_content_kind": "html",
        }
        == html_configuration
    )

    with sqlite3.connect(state) as connection:
        connection.row_factory = sqlite3.Row
        document = connection.execute("SELECT * FROM documents").fetchone()
        fts = connection.execute("SELECT * FROM document_fts").fetchone()
        assert (document["path"], document["media_type"], document["content_kind"]) == (
            str(html_source),
            "text/html",
            "html",
        )
        assert fts["body"] == "contenido visible"
        assert "oculto" not in fts["body"]
        assert _scalar(connection, "SELECT COUNT(*) FROM text_materializations") == 4
        assert _scalar(connection, "SELECT COUNT(*) FROM text_materialization_heads") == 2


def _create_legacy_v1(path: Path, snapshot: FileSnapshot) -> None:
    with sqlite3.connect(path) as connection:
        text_state_module._create_text_v1_schema(connection)
        connection.execute("INSERT INTO metadata VALUES('schema_version','1')")
        payload = Path(snapshot.path).read_text(encoding="utf-8")
        encoded = payload.encode("utf-8")
        file_key = file_key_from_snapshot(snapshot)
        connection.execute(
            """INSERT INTO documents(
            file_key,path,size,mtime_ns,birthtime_ns,processing_signature,status,
            content_kind,media_type,text_zlib,text_chars,text_xxh3_128,
            last_seen_run_id,updated_ns)
            VALUES(?,?,?,?,?,'text-route-v1','complete','txt','text/plain',?,?,?,1,1)""",
            (
                file_key,
                snapshot.path,
                snapshot.size,
                snapshot.mtime_ns,
                snapshot.birthtime_ns,
                zlib.compress(encoded),
                len(payload),
                text_route_module.xxhash.xxh3_128_hexdigest(encoded),
            ),
        )
        connection.execute(
            "INSERT INTO document_fts VALUES(?,?,?,?,?,?)",
            (file_key, snapshot.path, "txt", "", "", payload),
        )


def test_legacy_and_missing_physical_output_are_reexecuted_not_cached(
    tmp_path: Path,
) -> None:
    source = tmp_path / "legacy.txt"
    source.write_text("contenido legacy atribuible", encoding="utf-8")
    snapshot = snapshot_path(source)
    state = tmp_path / "text.sqlite3"
    _create_legacy_v1(state, snapshot)

    initialize_text_state(state)
    with sqlite3.connect(state) as connection:
        assert connection.execute("SELECT revision_id FROM documents").fetchone() == (None,)
        assert _scalar(connection, "SELECT COUNT(*) FROM text_work_receipts") == 0

    migrated = _route(state, source, 2).run()
    assert (migrated.extracted, migrated.cache_hits) == (1, 0)
    with sqlite3.connect(state) as connection:
        original_heads = {
            row[0]
            for row in connection.execute(
                "SELECT materialization_id FROM text_materialization_heads"
            )
        }
        connection.execute("DELETE FROM document_fts")

    with pytest.raises(TextDerivationIntegrityError, match="physical outputs"):
        read_reusable_text_derivation(
            state,
            file_key_from_snapshot(snapshot),
            stage_id="text.extract",
            processing_signature=migrated.effective_processing_signatures[0],
        )

    rebuilt = _route(state, source, 3).run()
    assert (rebuilt.extracted, rebuilt.cache_hits) == (1, 0)
    attempts = _attempts(state)
    assert [row["execution_mode"] for row in attempts] == ["executed", "executed"]
    with sqlite3.connect(state) as connection:
        current_heads = {
            row[0]
            for row in connection.execute(
                "SELECT materialization_id FROM text_materialization_heads"
            )
        }
        assert len(original_heads) == len(current_heads) == 2
        assert original_heads.isdisjoint(current_heads)
        assert _scalar(connection, "SELECT COUNT(*) FROM document_fts") == 1
        assert _scalar(connection, "SELECT COUNT(*) FROM text_materializations") == 4
        assert _scalar(connection, "SELECT COUNT(*) FROM text_work_receipts") == 2


def test_extraction_error_publishes_error_document_and_failed_receipt_atomically(
    tmp_path: Path,
) -> None:
    source = tmp_path / "malformado.txt"
    source.write_text("publicación previa válida", encoding="utf-8")
    state = tmp_path / "text.sqlite3"
    assert _route(state, source, 1).run().extracted == 1
    source.write_bytes(b"\x81\x8d\x8f\x90\x9d")

    summary = _route(state, source, 2).run()

    assert (summary.processed, summary.errors, summary.extracted) == (1, 1, 0)
    with sqlite3.connect(state) as connection:
        connection.row_factory = sqlite3.Row
        document = connection.execute("SELECT * FROM documents").fetchone()
        attempt = connection.execute(
            "SELECT * FROM text_derivation_attempts WHERE status='failed'"
        ).fetchone()
        receipt = json.loads(
            connection.execute(
                "SELECT receipt_json FROM text_work_receipts WHERE outcome='failed'"
            ).fetchone()[0]
        )
        assert document["status"] == "error"
        assert document["revision_id"] is not None
        assert attempt["status"] == "failed"
        assert attempt["receipt_id"] == receipt["receipt_id"]
        assert receipt["failure"]["reason_code"] == "UnicodeError"
        assert _scalar(connection, "SELECT COUNT(*) FROM document_fts") == 0
        assert _scalar(connection, "SELECT COUNT(*) FROM text_materializations") == 2
        assert _scalar(connection, "SELECT COUNT(*) FROM text_materialization_heads") == 0
        assert _scalar(connection, "SELECT COUNT(*) FROM text_derivation_outbox") == 2


def test_failed_extraction_is_cached_by_default_and_retried_only_when_requested(
    tmp_path: Path,
) -> None:
    source = tmp_path / "malformado.txt"
    source.write_bytes(b"\x81\x8d\x8f\x90\x9d")
    state = tmp_path / "text.sqlite3"

    first = _route(state, source, 1).run()
    cached = _route(state, source, 2).run()
    retried = _route(state, source, 3, retry_errors=True).run()

    assert (first.processed, first.errors, first.cached_errors) == (1, 1, 0)
    assert (cached.processed, cached.errors, cached.cache_hits, cached.cached_errors) == (
        0,
        0,
        1,
        1,
    )
    assert (retried.processed, retried.errors, retried.cached_errors) == (1, 1, 0)
    with sqlite3.connect(state) as connection:
        assert _scalar(connection, "SELECT COUNT(*) FROM text_work_receipts") == 2
        assert _scalar(connection, "SELECT COUNT(*) FROM text_derivation_attempts") == 2


def test_cached_failure_never_treats_same_size_and_mtime_as_content_identity(
    tmp_path: Path,
) -> None:
    source = tmp_path / "same-metadata.txt"
    source.write_bytes(b"\x81\x8d\x8f\x90\x9d")
    state = tmp_path / "text.sqlite3"
    assert _route(state, source, 1).run().errors == 1
    failed_stat = source.stat()

    source.write_bytes(b"VALID")
    os.utime(source, ns=(failed_stat.st_atime_ns, failed_stat.st_mtime_ns))
    recovered = _route(state, source, 2).run()

    assert (recovered.extracted, recovered.cache_hits, recovered.cached_errors) == (1, 0, 0)
    with sqlite3.connect(state) as connection:
        assert connection.execute("SELECT status FROM documents").fetchone() == ("complete",)
        assert _scalar(connection, "SELECT COUNT(*) FROM text_derivation_attempts") == 2


def test_error_diagnostics_never_persist_provider_secret_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "secreto.txt"
    source.write_text("contenido", encoding="utf-8")
    state = tmp_path / "text.sqlite3"
    sentinel = "SECRET_SENTINEL_TOKEN_123"

    def fail_with_secret(*_args, **_kwargs):
        raise ValueError(f"provider failed with token={sentinel}")

    monkeypatch.setattr(text_route_module, "_extract", fail_with_secret)
    summary = _route(state, source, 1).run()

    assert summary.errors == 1
    with sqlite3.connect(state) as connection:
        document_error = str(
            connection.execute("SELECT error_message FROM documents").fetchone()[0]
        )
        receipt_json = str(
            connection.execute("SELECT receipt_json FROM text_work_receipts").fetchone()[0]
        )
        outbox_json = str(
            connection.execute("SELECT payload_json FROM text_derivation_outbox").fetchone()[0]
        )
    assert sentinel not in document_error
    assert sentinel not in receipt_json
    assert sentinel not in outbox_json
    assert "diagnostic detail redacted" in document_error
    failure_details = json.loads(receipt_json)["failure"]["details"]
    assert failure_details["diagnostic_detail"] == "[redacted]"
    assert failure_details["selection"].startswith("selected:neocortex.text.builtin")


def test_cancellation_after_begin_is_terminal_without_publishing_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "fuente.txt"
    source.write_text("contenido cancelable", encoding="utf-8")
    state = tmp_path / "text.sqlite3"
    token = CancellationToken()
    original = text_route_module.begin_text_derivation_attempt_from_connection

    def begin_and_cancel(*args, **kwargs) -> None:
        original(*args, **kwargs)
        token.cancel()

    monkeypatch.setattr(
        text_route_module,
        "begin_text_derivation_attempt_from_connection",
        begin_and_cancel,
    )

    with pytest.raises(CancellationRequested):
        _route(state, source, 1, cancellation=token).run()

    attempts = _attempts(state)
    assert [row["status"] for row in attempts] == ["cancelled"]
    with sqlite3.connect(state) as connection:
        assert _scalar(connection, "SELECT COUNT(*) FROM text_work_receipts") == 1
        assert _scalar(connection, "SELECT COUNT(*) FROM text_derivation_outbox") == 1
        assert _scalar(connection, "SELECT COUNT(*) FROM documents") == 0
        assert _scalar(connection, "SELECT COUNT(*) FROM document_fts") == 0
        assert _scalar(connection, "SELECT COUNT(*) FROM text_materializations") == 0


def test_cancellation_during_read_records_partial_input_and_terminal_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "fuente.txt"
    source.write_text("contenido cuya lectura será cancelada", encoding="utf-8")
    state = tmp_path / "text.sqlite3"
    token = CancellationToken()

    def cancel_during_read(_snapshot, _limit, cancellation):
        cancellation.cancel()
        cancellation.checkpoint()

    monkeypatch.setattr(text_route_module, "_read_exact", cancel_during_read)

    with pytest.raises(CancellationRequested):
        _route(state, source, 7, cancellation=token).run()

    attempts = _attempts(state)
    assert [row["status"] for row in attempts] == ["cancelled"]
    assert [row["attempt_number"] for row in attempts] == [1]
    with sqlite3.connect(state) as connection:
        revision = connection.execute(
            "SELECT revision_state,fingerprint_algorithm FROM text_input_revisions"
        ).fetchone()
        assert revision == ("partial", "text-snapshot-v1")
        assert _scalar(connection, "SELECT COUNT(*) FROM text_work_receipts") == 1
        assert _scalar(connection, "SELECT COUNT(*) FROM text_derivation_outbox") == 1
        assert _scalar(connection, "SELECT COUNT(*) FROM documents") == 0
        assert _scalar(connection, "SELECT COUNT(*) FROM document_fts") == 0


def test_crash_leaves_running_and_next_run_reconciles_before_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "fuente.txt"
    source.write_text("contenido recuperable", encoding="utf-8")
    state = tmp_path / "text.sqlite3"
    original = text_route_module._extract

    def crash(*_args, **_kwargs):
        raise KeyboardInterrupt("worker disappeared")

    monkeypatch.setattr(text_route_module, "_extract", crash)
    with pytest.raises(KeyboardInterrupt, match="worker disappeared"):
        _route(state, source, 1).run()

    assert [row["status"] for row in _attempts(state)] == ["running"]
    with sqlite3.connect(state) as connection:
        assert _scalar(connection, "SELECT COUNT(*) FROM text_work_receipts") == 0
        assert _scalar(connection, "SELECT COUNT(*) FROM documents") == 0

    lock_path = state.with_suffix(state.suffix + ".route.lock")
    with FrameworkRunLock(lock_path):
        with pytest.raises(RuntimeError, match="another framework execution"):
            _route(state, source, 2).run()
    assert [row["status"] for row in _attempts(state)] == ["running"]

    monkeypatch.setattr(text_route_module, "_extract", original)
    recovered = _route(state, source, 2).run()

    assert recovered.extracted == 1
    recovered_attempts = _attempts(state)
    assert [row["status"] for row in recovered_attempts] == ["abandoned", "succeeded"]
    assert [row["attempt_number"] for row in recovered_attempts] == [1, 2]
    with sqlite3.connect(state) as connection:
        assert _scalar(connection, "SELECT COUNT(*) FROM text_work_receipts") == 2
        assert _scalar(connection, "SELECT COUNT(*) FROM text_derivation_outbox") == 2
        assert _scalar(connection, "SELECT COUNT(*) FROM documents") == 1
        assert _scalar(connection, "SELECT COUNT(*) FROM document_fts") == 1


def test_terminal_publication_rollback_leaves_no_partial_document_or_fts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "fuente.txt"
    source.write_text("contenido transaccional", encoding="utf-8")
    state = tmp_path / "text.sqlite3"
    original = text_route_module.succeed_text_derivation_attempt

    def fail_after_terminalization(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("fault after terminalization")

    monkeypatch.setattr(
        text_route_module,
        "succeed_text_derivation_attempt",
        fail_after_terminalization,
    )

    with pytest.raises(RuntimeError, match="fault after terminalization"):
        _route(state, source, 1).run()

    assert [row["status"] for row in _attempts(state)] == ["running"]
    with sqlite3.connect(state) as connection:
        assert _scalar(connection, "SELECT COUNT(*) FROM documents") == 0
        assert _scalar(connection, "SELECT COUNT(*) FROM document_fts") == 0
        assert _scalar(connection, "SELECT COUNT(*) FROM text_work_receipts") == 0
        assert _scalar(connection, "SELECT COUNT(*) FROM text_materializations") == 0
        assert _scalar(connection, "SELECT COUNT(*) FROM text_derivation_outbox") == 0


def test_failed_terminalization_rollback_preserves_previous_published_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "fuente.txt"
    source.write_text("representación publicada", encoding="utf-8")
    state = tmp_path / "text.sqlite3"
    assert _route(state, source, 1).run().extracted == 1
    source.write_bytes(b"\x81\x8d\x8f\x90\x9d")
    original = text_route_module.fail_text_derivation_attempt

    def fail_after_terminalization(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("fault after failed terminalization")

    monkeypatch.setattr(
        text_route_module,
        "fail_text_derivation_attempt",
        fail_after_terminalization,
    )

    with pytest.raises(RuntimeError, match="fault after failed terminalization"):
        _route(state, source, 2).run()

    attempts = _attempts(state)
    assert [row["status"] for row in attempts] == ["succeeded", "running"]
    with sqlite3.connect(state) as connection:
        connection.row_factory = sqlite3.Row
        document = connection.execute("SELECT * FROM documents").fetchone()
        fts = connection.execute("SELECT * FROM document_fts").fetchone()
        assert document["status"] == "complete"
        assert zlib.decompress(document["text_zlib"]).decode("utf-8") == (
            "representación publicada"
        )
        assert fts["body"] == "representación publicada"
        assert _scalar(connection, "SELECT COUNT(*) FROM text_work_receipts") == 1
        assert _scalar(connection, "SELECT COUNT(*) FROM text_materializations") == 2
        assert _scalar(connection, "SELECT COUNT(*) FROM text_materialization_heads") == 2
        assert _scalar(connection, "SELECT COUNT(*) FROM text_derivation_outbox") == 1


def test_legacy_worker_verifies_the_pinned_backend_artifact() -> None:
    command = Path(sys.executable).resolve()
    payload = command.read_bytes()
    arguments = argparse.Namespace(
        backend="catdoc",
        kind="doc",
        backend_command=str(command),
        backend_sha256=hashlib.sha256(payload).hexdigest(),
        backend_size=len(payload),
    )

    assert legacy_worker_module._verified_backend(arguments) == command
    arguments.backend_sha256 = "0" * 64
    with pytest.raises(ValueError, match="identity changed"):
        legacy_worker_module._verified_backend(arguments)


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable fixture")
def test_legacy_worker_stops_if_backend_grows_during_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = tmp_path / "catdoc"
    command.write_bytes(b"x")
    command.chmod(0o755)
    real_access = legacy_worker_module.os.access

    def grow_before_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        mode: int,
    ) -> bool:
        command.write_bytes(b"x" * (16 * 1024 * 1024 + 1))
        command.chmod(0o755)
        return real_access(path, mode)

    monkeypatch.setattr(legacy_worker_module.os, "access", grow_before_open)
    arguments = argparse.Namespace(
        backend="catdoc",
        kind="doc",
        backend_command=str(command),
        backend_sha256=hashlib.sha256(b"x").hexdigest(),
        backend_size=1,
    )

    with pytest.raises(ValueError, match="exceeds the verified size limit"):
        legacy_worker_module._verified_backend(arguments)


def test_text_route_passes_only_the_selected_backend_to_the_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[str, ...]] = []

    def run(command, **_kwargs):
        captured.append(tuple(command))
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {
                    "ok": True,
                    "backend": "catdoc",
                    "conversion": "catdoc",
                    "text": "contenido legado",
                    "truncated": False,
                }
            ).encode(),
            stderr=b"",
        )

    monkeypatch.setattr(text_route_module, "run_bounded_capture", run)
    backend = CapabilityBinaryIdentity(
        name="catdoc",
        command=str(Path(sys.executable).resolve()),
        command_sha256=hashlib.sha256(str(Path(sys.executable).resolve()).encode()).hexdigest(),
        artifact_sha256=hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest(),
        size_bytes=Path(sys.executable).stat().st_size,
    )

    extracted = text_route_module._legacy_office_text(
        b"fixture",
        "doc",
        TextRouteConfig(tmp_path / "text.sqlite3"),
        backend,
    )

    command = captured[0]
    assert command[command.index("--backend") + 1] == "catdoc"
    assert command[command.index("--backend-command") + 1] == backend.command
    assert command[command.index("--backend-sha256") + 1] == backend.artifact_sha256
    assert "--libreoffice-cmd" not in command
    assert extracted.detail == "backend=catdoc;conversion=catdoc"


def test_explicit_libreoffice_command_disables_implicit_alternatives(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    explicit = str(Path(sys.executable).resolve())
    monkeypatch.setattr(
        text_route_module.shutil,
        "which",
        lambda name: explicit if name in {explicit, "catdoc"} else None,
    )

    finder = text_route_module._text_executable_finder(
        TextRouteConfig(tmp_path / "text.sqlite3", libreoffice_cmd=explicit)
    )

    assert finder("soffice") is None
    assert finder("libreoffice") == explicit
    assert finder("catdoc") is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable fixture")
def test_plain_processing_provenance_does_not_execute_office_probe(tmp_path: Path) -> None:
    marker = tmp_path / "office-probe-ran"
    backend = tmp_path / "libreoffice"
    backend.write_text(
        f"#!/bin/sh\nprintf ran > '{marker}'\nprintf 'fixture 1.0\\n'\n",
        encoding="utf-8",
    )
    backend.chmod(0o755)

    provenance = TextRouteConfig(
        tmp_path / "text.sqlite3",
        libreoffice_cmd=str(backend),
    ).processing_provenance

    assert provenance.signature
    assert not marker.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable fixture")
def test_legacy_backend_artifact_change_invalidates_cache_and_is_receipted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "legacy.doc"
    source.write_bytes(b"legacy fixture")
    state = tmp_path / "text.sqlite3"
    backend = tmp_path / "catdoc"

    def install_backend(text: str) -> None:
        backend.write_text(f"#!/bin/sh\nprintf '{text}\\n'\n", encoding="utf-8")
        backend.chmod(0o755)

    install_backend("primera extracción")
    monkeypatch.setattr(
        text_route_module.shutil,
        "which",
        lambda name: str(backend) if name == "catdoc" else None,
    )

    first = _route(state, source, 1, mime="application/msword").run()
    install_backend("segunda extracción")
    second = _route(state, source, 2, mime="application/msword").run()

    assert (first.extracted, first.cache_hits, first.legacy_office) == (1, 0, 1)
    assert (second.extracted, second.cache_hits, second.legacy_office) == (1, 0, 1)
    attempts = _attempts(state)
    assert [row["execution_mode"] for row in attempts] == ["executed", "executed"]
    assert [row["reproducibility_class"] for row in attempts] == [
        "non_replayable",
        "non_replayable",
    ]
    assert attempts[0]["processing_signature"] != attempts[1]["processing_signature"]
    with sqlite3.connect(state) as connection:
        row = connection.execute("SELECT detail,text_zlib FROM documents").fetchone()
        assert row[0] == "backend=catdoc;conversion=catdoc"
        assert zlib.decompress(row[1]).decode() == "segunda extracción\n"
        receipts = tuple(
            json.loads(value)
            for (value,) in connection.execute(
                "SELECT receipt_json FROM text_work_receipts ORDER BY recorded_ns"
            )
        )
    configurations = [dict(receipt["effective_configuration"]) for receipt in receipts]
    assert [item["capability_binary_backend"] for item in configurations] == [
        "catdoc",
        "catdoc",
    ]
    assert (
        configurations[0]["capability_binary_artifact_sha256"]
        != (configurations[1]["capability_binary_artifact_sha256"])
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable fixture")
def test_legacy_launcher_with_mutable_engine_is_never_reused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "legacy.doc"
    source.write_bytes(b"legacy fixture")
    state = tmp_path / "text.sqlite3"
    backend = tmp_path / "catdoc"
    engine = tmp_path / "engine"
    backend.write_text(f"#!/bin/sh\nexec '{engine}' \"$@\"\n", encoding="utf-8")
    backend.chmod(0o755)

    def install_engine(text: str) -> None:
        engine.write_text(f"#!/bin/sh\nprintf '{text}\\n'\n", encoding="utf-8")
        engine.chmod(0o755)

    install_engine("ENGINE_ONE")
    monkeypatch.setattr(
        text_route_module.shutil,
        "which",
        lambda name: str(backend) if name == "catdoc" else None,
    )
    first = _route(state, source, 1, mime="application/msword").run()
    install_engine("ENGINE_TWO")
    second = _route(state, source, 2, mime="application/msword").run()

    assert (first.extracted, first.cache_hits) == (1, 0)
    assert (second.extracted, second.cache_hits) == (1, 0)
    attempts = _attempts(state)
    assert [row["execution_mode"] for row in attempts] == ["executed", "executed"]
    assert [row["reproducibility_class"] for row in attempts] == [
        "non_replayable",
        "non_replayable",
    ]
    assert attempts[0]["processing_signature"] == attempts[1]["processing_signature"]
    with sqlite3.connect(state) as connection:
        row = connection.execute("SELECT text_zlib FROM documents").fetchone()
    assert zlib.decompress(row[0]).decode() == "ENGINE_TWO\n"


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable fixture")
def test_failed_selected_legacy_backend_does_not_fall_through_to_another(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "legacy.doc"
    source.write_bytes(b"legacy fixture")
    state = tmp_path / "text.sqlite3"
    primary = tmp_path / "soffice"
    fallback = tmp_path / "catdoc"
    fallback_marker = tmp_path / "fallback-used"
    primary.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    fallback.write_text(
        f"#!/bin/sh\nprintf used > '{fallback_marker}'\nprintf 'hidden fallback'\n",
        encoding="utf-8",
    )
    primary.chmod(0o755)
    fallback.chmod(0o755)
    monkeypatch.setattr(
        text_route_module.shutil,
        "which",
        lambda name: (
            str(primary) if name == "soffice" else str(fallback) if name == "catdoc" else None
        ),
    )

    summary = _route(state, source, 1, mime="application/msword").run()

    assert (summary.extracted, summary.errors, summary.legacy_office) == (0, 1, 0)
    assert not fallback_marker.exists()
    attempts = _attempts(state)
    assert [(row["status"], row["execution_mode"]) for row in attempts] == [("failed", "attempted")]
    with sqlite3.connect(state) as connection:
        receipt = json.loads(
            connection.execute("SELECT receipt_json FROM text_work_receipts").fetchone()[0]
        )
    assert dict(receipt["effective_configuration"])["capability_binary_backend"] == "soffice"


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable fixture")
@pytest.mark.parametrize(
    ("suffix", "mime_type", "specific_backend"),
    (
        ("xls", "application/vnd.ms-excel", "xls2csv"),
        ("ppt", "application/vnd.ms-powerpoint", "catppt"),
    ),
)
def test_failed_format_specific_backend_does_not_fall_through_to_soffice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    suffix: str,
    mime_type: str,
    specific_backend: str,
) -> None:
    source = tmp_path / f"legacy.{suffix}"
    source.write_bytes(b"legacy fixture")
    state = tmp_path / "text.sqlite3"
    specific = tmp_path / specific_backend
    soffice = tmp_path / "soffice"
    soffice_marker = tmp_path / "soffice-used"
    specific.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    soffice.write_text(
        f"#!/bin/sh\nprintf used > '{soffice_marker}'\nprintf 'hidden fallback\\n'\n",
        encoding="utf-8",
    )
    specific.chmod(0o755)
    soffice.chmod(0o755)
    executables = {specific_backend: str(specific), "soffice": str(soffice)}
    monkeypatch.setattr(
        text_route_module.shutil,
        "which",
        executables.get,
    )

    summary = _route(state, source, 1, mime=mime_type).run()

    assert (summary.extracted, summary.errors, summary.legacy_office) == (0, 1, 0)
    assert not soffice_marker.exists()
    attempts = _attempts(state)
    assert [(row["status"], row["execution_mode"]) for row in attempts] == [("failed", "attempted")]
    with sqlite3.connect(state) as connection:
        receipt = json.loads(
            connection.execute("SELECT receipt_json FROM text_work_receipts").fetchone()[0]
        )
    assert dict(receipt["effective_configuration"])["capability_binary_backend"] == (
        specific_backend
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable fixture")
def test_explicit_libreoffice_command_overrides_xls2csv_for_the_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "legacy.xls"
    source.write_bytes(b"legacy fixture")
    state = tmp_path / "text.sqlite3"
    explicit = tmp_path / "explicit-libreoffice"
    xls2csv = tmp_path / "xls2csv"
    xls2csv_marker = tmp_path / "xls2csv-used"
    explicit.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    xls2csv.write_text(
        f"#!/bin/sh\nprintf used > '{xls2csv_marker}'\nprintf 'hidden fallback\\n'\n",
        encoding="utf-8",
    )
    explicit.chmod(0o755)
    xls2csv.chmod(0o755)
    monkeypatch.setattr(
        text_route_module.shutil,
        "which",
        lambda name: (
            str(explicit) if name == str(explicit) else str(xls2csv) if name == "xls2csv" else None
        ),
    )
    route = TextRoute(
        TextRouteConfig(state_path=state, libreoffice_cmd=str(explicit)),
        _FrameworkState(snapshot_path(source), "application/vnd.ms-excel"),
        1,
        cancellation=CancellationToken(),
    )

    summary = route.run()

    assert (summary.extracted, summary.errors, summary.legacy_office) == (0, 1, 0)
    assert not xls2csv_marker.exists()
    with sqlite3.connect(state) as connection:
        receipt = json.loads(
            connection.execute("SELECT receipt_json FROM text_work_receipts").fetchone()[0]
        )
    assert dict(receipt["effective_configuration"])["capability_binary_backend"] == ("libreoffice")
