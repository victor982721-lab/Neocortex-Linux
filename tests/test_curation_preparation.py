"""Linux-only preparation contracts for curation boundaries."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import neocortex.documents.document_cache_sync as cache_sync
from neocortex.curation.kio_harness import (
    KIO_DESKTOP_HARNESS_TRASH_URL,
    KioDesktopHarnessError,
    KioDesktopHarnessSpec,
)
from neocortex.curation.read import read_curation_snapshot
from neocortex.deduplication import FileSnapshot, snapshot_path
from neocortex.documents.document_cache_sync import (
    CacheDatabaseSync,
    DocumentCacheSyncResult,
    curation_cache_policy,
    synchronize_curation_move_fixture,
)
from neocortex.persistence.framework_schema import initialize_framework_schema
from neocortex.workflow.authorization.contracts import AuthorizationGrant
from neocortex.workflow.authorization.repository import issue_authorization_grant
from neocortex.workflow.authorization.principal import (
    AUTHENTICATED_PRINCIPAL_AUTH_METHOD,
    AUTHENTICATED_PRINCIPAL_SCHEMA,
    AuthenticatedPrincipal,
    PrincipalValidationError,
    principal_proof_digest,
    require_authenticated_principal,
)


TEST_CAPABILITIES = ("base", "platform")
pytestmark = pytest.mark.capability("base", "platform")


def _text_cache_fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path, FileSnapshot]:
    root = tmp_path / "fixture"
    root.mkdir()
    state = root / "state"
    state.mkdir()
    corpus = root / "corpus"
    corpus.mkdir()
    source = corpus / "source.txt"
    destination = corpus / "organized" / source.name
    destination.parent.mkdir()
    source.write_bytes(b"fixture cache content")
    snapshot = snapshot_path(source)
    file_key = f"{snapshot.volume_id}:{snapshot.file_id}"
    with sqlite3.connect(state / "text.sqlite3") as connection:
        connection.execute(
            "CREATE TABLE documents("
            "file_key TEXT PRIMARY KEY,path TEXT NOT NULL,updated_ns INTEGER)"
        )
        connection.execute(
            "INSERT INTO documents(file_key,path,updated_ns) VALUES(?,?,?)",
            (file_key, str(source), 1),
        )
    with sqlite3.connect(state / "framework.sqlite3") as connection:
        initialize_framework_schema(connection, lambda: None)
    return root, state, source, destination, snapshot


def _cache_sync_kwargs(
    root: Path,
    state: Path,
    source: Path,
    destination: Path,
    snapshot: FileSnapshot,
    *,
    action: str = "move",
    effect_applied: bool = True,
) -> dict[str, object]:
    return {
        "state_directory": state,
        "fixture_root": root,
        "action": action,
        "source_kind": "text",
        "file_key": f"{snapshot.volume_id}:{snapshot.file_id}",
        "old_path": str(source),
        "new_path": str(destination),
        "volume_id": str(snapshot.volume_id),
        "file_id": str(snapshot.file_id),
        "effect_applied": effect_applied,
    }


def test_principal_contract_rejects_actor_text_and_client_mappings() -> None:
    for value in (None, "victor", {"principal_id": "victor"}, {"authenticated": True}):
        with pytest.raises(PrincipalValidationError, match="not an authenticated principal"):
            require_authenticated_principal(value)


def test_principal_contract_requires_trusted_factory_and_live_window() -> None:
    proof = principal_proof_digest({"session": "fixture", "nonce": 1})
    with pytest.raises(PrincipalValidationError, match="trusted context"):
        AuthenticatedPrincipal(
            "victor",
            "fixture-issuer",
            "session-1",
            10,
            20,
            proof,
        )
    principal = AuthenticatedPrincipal.from_trusted_context(
        principal_id="principal:fixture",
        issuer="fixture-issuer",
        session_id="session-1",
        issued_ns=10,
        expires_ns=20,
        proof_digest=proof,
    )
    assert require_authenticated_principal(principal, now_ns=10) is principal
    with pytest.raises(PrincipalValidationError, match="not active yet"):
        require_authenticated_principal(principal, now_ns=9)
    with pytest.raises(PrincipalValidationError, match="expired"):
        require_authenticated_principal(principal, now_ns=20)
    payload = principal.to_dict()
    assert payload["schema"] == AUTHENTICATED_PRINCIPAL_SCHEMA
    assert payload["authentication_method"] == AUTHENTICATED_PRINCIPAL_AUTH_METHOD


def test_curation_cache_sync_moves_paths_without_crossing_file_action_frontier(
    tmp_path: Path,
) -> None:
    root, state, source, destination, snapshot = _text_cache_fixture(tmp_path)
    source.rename(destination)
    before_framework = (state / "framework.sqlite3").read_bytes()
    result = synchronize_curation_move_fixture(
        **_cache_sync_kwargs(root, state, source, destination, snapshot)
    )
    assert result.status == "complete"
    assert result.policy.strategy == "path_transition"
    assert result.publication_status == "complete"
    assert destination.read_bytes() == b"fixture cache content"
    assert not source.exists()
    with sqlite3.connect(state / "text.sqlite3") as connection:
        assert connection.execute("SELECT path FROM documents").fetchone() == (str(destination),)
    with sqlite3.connect(state / "framework.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM file_actions").fetchone() == (0,)
    assert (state / "framework.sqlite3").read_bytes() == before_framework


def test_curation_cache_sync_reports_moved_cache_pending_and_replays(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, state, source, destination, snapshot = _text_cache_fixture(tmp_path)
    source.rename(destination)
    original = cache_sync._synchronize_database

    def fail_semantic(label: str, *args: object, **kwargs: object):
        if label == "semantic":
            return CacheDatabaseSync(label, "error", detail="fixture semantic unavailable")
        return original(label, *args, **kwargs)

    monkeypatch.setattr(cache_sync, "_synchronize_database", fail_semantic)
    first = synchronize_curation_move_fixture(
        **_cache_sync_kwargs(root, state, source, destination, snapshot)
    )
    assert first.status == "moved_cache_pending"
    assert first.retryable is True
    assert first.publication_status == "partial"

    monkeypatch.setattr(cache_sync, "_synchronize_database", original)
    resumed = synchronize_curation_move_fixture(
        **_cache_sync_kwargs(root, state, source, destination, snapshot)
    )
    assert resumed.status == "complete"
    assert resumed.updated_rows == 0


def test_curation_cache_sync_classifies_boundary_exception_as_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, state, source, destination, snapshot = _text_cache_fixture(tmp_path)

    def interrupted(*_args: object, **_kwargs: object):
        raise KeyboardInterrupt("fixture boundary interruption")

    monkeypatch.setattr(cache_sync, "synchronize_moved_document", interrupted)
    result = synchronize_curation_move_fixture(
        **_cache_sync_kwargs(root, state, source, destination, snapshot)
    )
    assert result.status == "recovery_required"
    assert "uncertain" in (result.detail or "")
    with sqlite3.connect(state / "framework.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM file_actions").fetchone() == (0,)


def test_curation_cache_sync_has_separate_trash_invalidation_policy(tmp_path: Path) -> None:
    root, state, source, destination, snapshot = _text_cache_fixture(tmp_path)
    before = source.read_bytes()
    policy = curation_cache_policy("trash")
    result = synchronize_curation_move_fixture(
        **_cache_sync_kwargs(
            root,
            state,
            source,
            destination,
            snapshot,
            action="trash",
            effect_applied=False,
        )
    )
    assert policy.strategy == "invalidation_separate"
    assert policy.supported is False
    assert result.status == "blocked"
    assert "invalidation policy" in (result.detail or "")
    assert source.read_bytes() == before
    assert not (state / "state-publication-journal.jsonl").exists()
    with sqlite3.connect(state / "framework.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM file_actions").fetchone() == (0,)


def test_curation_cache_sync_lock_order_is_framework_before_owner(tmp_path: Path, monkeypatch):
    root, state, source, destination, snapshot = _text_cache_fixture(tmp_path)
    order: list[str] = []

    class FakeLock:
        def __init__(self, _path: Path) -> None:
            pass

        def __enter__(self):
            order.append("framework.lock")
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    def fake_sync(*_args: object, **_kwargs: object) -> DocumentCacheSyncResult:
        order.append("owner.sqlite3")
        return DocumentCacheSyncResult(
            complete=True,
            updated_rows=0,
            databases=(),
            publication_epoch=1,
            publication_status="complete",
            publication_id="fixture-publication",
        )

    monkeypatch.setattr(cache_sync, "FrameworkRunLock", FakeLock)
    monkeypatch.setattr(cache_sync, "synchronize_moved_document", fake_sync)
    result = synchronize_curation_move_fixture(
        **_cache_sync_kwargs(root, state, source, destination, snapshot)
    )
    assert result.status == "complete"
    assert order == ["framework.lock", "owner.sqlite3"]
    assert cache_sync.CURATION_CACHE_SYNC_LOCK_ORDER == (
        "framework.lock",
        "state-publication.lock",
        "owner.sqlite3",
    )


def test_curation_read_snapshot_is_read_only_and_exposes_grant_without_attempt(
    tmp_path: Path,
) -> None:
    _root, framework, _grant = _authorization_read_fixture(tmp_path)
    before = framework.read_bytes()
    snapshot = read_curation_snapshot(framework, limit=10)
    assert snapshot.status == "complete"
    assert len(snapshot.grants) == 1
    assert snapshot.attempts == ()
    assert snapshot.recovery == ()
    payload = snapshot.to_dict()
    assert payload["read_only"] is True
    assert payload["effects"] == {"state": "none", "corpus": "none", "external": "none"}
    assert snapshot.grants[0].principal_state == "not_authenticated"
    assert framework.read_bytes() == before


def test_curation_read_snapshot_exposes_receipt_and_recovery_without_effects_in_read(
    tmp_path: Path,
) -> None:
    root, framework, grant = _authorization_read_fixture(tmp_path)
    _insert_read_only_actions(framework, root, grant)
    snapshot = read_curation_snapshot(framework, limit=10)
    assert len(snapshot.attempts) == 2
    applied = next(item for item in snapshot.attempts if item.status == "applied")
    assert applied.receipt_digest is not None
    assert applied.receipt_state == "object"
    assert len(snapshot.recovery) == 1
    assert snapshot.recovery[0].status == "recovery_required"
    with sqlite3.connect(framework) as connection:
        assert connection.execute("SELECT COUNT(*) FROM file_actions").fetchone() == (2,)


def test_curation_read_missing_owner_does_not_create_state(tmp_path: Path) -> None:
    database = tmp_path / "missing" / "framework.sqlite3"
    snapshot = read_curation_snapshot(database)
    assert snapshot.status == "unavailable"
    assert snapshot.error_code == "state_absent"
    assert not database.exists()


def _authorization_read_fixture(tmp_path: Path) -> tuple[Path, Path, AuthorizationGrant]:
    root = tmp_path / "fixture"
    root.mkdir()
    database = root / "framework.sqlite3"
    with sqlite3.connect(database) as connection:
        initialize_framework_schema(connection, lambda: None)
    grant = AuthorizationGrant(
        grant_id="curation-authorization-grant-v1:read-fixture",
        authorization_key="curation-authorization-key-v1:read-fixture",
        scope="personal",
        task_type="curation-review",
        selector_signature="curation-plan-v1",
        plan_digest="sha256:" + "1" * 64,
        snapshot_id="sha256:" + "2" * 64,
        source_snapshot_fingerprint="review-task-source-snapshot-v1:sha256:" + "3" * 64,
        root=str(root),
        actor="victor",
        action="move",
        backend="linux",
        item_ids=("item:read-fixture",),
        task_ids=("task:read-fixture",),
        max_actions=1,
        max_bytes=100,
        issued_ns=10,
        expires_ns=20,
    )
    issue_authorization_grant(database, grant)
    return root, database, grant


def _insert_read_only_actions(
    database: Path,
    root: Path,
    grant: AuthorizationGrant,
) -> None:
    source = root / "source.txt"
    target = root / "target.txt"
    intent = json.dumps(
        {
            "effect": {"effect_id": "item:read-fixture:effect:1"},
            "grant_id": grant.grant_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    receipt = json.dumps(
        {
            "operation": "move",
            "receipt_type": "successful_return_and_observation",
            "schema_version": 1,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            """INSERT INTO initial_runs(
                run_id,root,started_ns,status,run_kind,corpus_access_mode
            ) VALUES(1,?,'1','completed','initial','normal')""",
            (str(root),),
        )
        connection.executemany(
            """INSERT INTO file_actions(
                run_id,action_type,source_path,target_path,detected_mime,evidence,
                apply_requested,status,detail,started_ns,completed_ns,idempotency_key,
                expected_identity_json,effect_receipt_json,corpus_access_mode,
                protected_root,protected_root_device_id_hex,protected_root_file_id_hex,
                protected_root_birthtime_ns
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                (
                    1,
                    "move_curation",
                    str(source),
                    str(target),
                    None,
                    intent,
                    1,
                    "applied",
                    "fixture applied",
                    2,
                    3,
                    "read-fixture-applied",
                    '{"schema_version":1}',
                    receipt,
                    "normal",
                    None,
                    None,
                    None,
                    None,
                ),
                (
                    1,
                    "move_curation",
                    str(source),
                    str(target),
                    None,
                    intent + "-recovery",
                    1,
                    "recovery_required",
                    "fixture recovery",
                    4,
                    None,
                    "read-fixture-recovery",
                    '{"schema_version":1}',
                    None,
                    "normal",
                    None,
                    None,
                    None,
                    None,
                ),
            ),
        )
        connection.execute(
            """INSERT INTO file_action_reconciliation_events(
                action_id,sequence,previous_event_id,reconciliation_key,observed_ns,
                recorded_ns,action_status,reconciler_signature,event_schema_version,
                actor,provenance_json,classification,recommendation,detail,evidence_json
            ) VALUES(2,1,NULL,'read-fixture-reconciliation',5,6,'recovery_required',
                'fixture-reconciler-v1',1,'victor','{}','ambiguous',
                'preserve_evidence_and_review_manually','fixture recovery evidence','{}')"""
        )


def test_kio_desktop_harness_is_fixture_only_and_declares_observations(tmp_path: Path) -> None:
    root = tmp_path / "fixture"
    root.mkdir()
    spec = KioDesktopHarnessSpec(
        fixture_root=root,
        source=root / "corpus" / "source.txt",
        trash_root=root / "trash",
        config_home=root / "config",
        client_path=root / "bin" / "kioclient5",
    )
    assert spec.command[-1] == KIO_DESKTOP_HARNESS_TRASH_URL
    assert spec.environment["XDG_CONFIG_HOME"] == str(root / "config")
    payload = spec.to_dict()
    assert payload["execution"] == "fixture-only"
    assert payload["real_execution"] is False
    assert payload["runner"] == "injected-only"
    with pytest.raises(KioDesktopHarnessError, match="real KIO"):
        KioDesktopHarnessSpec(
            fixture_root=root,
            source=root / "source.txt",
            trash_root=root / "trash",
            config_home=root / "config",
            client_path=root / "kioclient5",
            execution="real",  # type: ignore[arg-type]
        )
