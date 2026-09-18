"""Shared primitives for the framework state repositories."""
# region [00] Contexto del módulo
# Módulo: neocortex/framework_state_common.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations
import json
import os
import sqlite3
import time
from collections.abc import Iterable
from pathlib import Path

from neocortex.foundation.hash_compat import stable_sha256_128_hexdigest

from neocortex.safety.corpus_access import (
    CorpusAccessPolicy,
    CorpusMutationGuard,
)
from neocortex.safety.internal_paths import (
    InternalPathProtectionError,
    InternalPathsPolicy,
    canonical_internal_paths_policy,
)
from neocortex.integrations.inventory.inventory_boundary import build_normal_inventory_boundary
from neocortex.safety.protected_content import (
    ProtectedContentPolicy,
    canonical_protected_content_policy,
)
# endregion [01]

# region [02] Implementación


CACHE_PRUNE_BATCH_SIZE = 1000
FileActionSpec = tuple[str, str, str | None, str | None, str | None, bool]

_TERMINAL_FILE_ACTION_STATUSES = frozenset({"planned", "applied", "skipped", "failed"})
_FILE_ACTION_TRANSITIONS = {
    "started": frozenset({"planned", "skipped", "failed", "applying", "recovery_required"}),
    "applying": frozenset({"applied", "recovery_required"}),
}


def _action_idempotency_key(
    run_id: int,
    action_type: str,
    source_path: str,
    target_path: str | None,
    detected_mime: str | None,
    evidence: str | None,
    apply_requested: bool,
) -> str:
    payload = json.dumps(
        [
            "file-action-v1",
            run_id,
            action_type,
            source_path,
            target_path,
            detected_mime,
            evidence,
            apply_requested,
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return stable_sha256_128_hexdigest(payload)


def _require_json_object(value: str, *, label: str) -> None:
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{label} must be a JSON object")


def _require_persisted_absolute_path(value: object, *, label: str) -> Path:
    if value is None:
        raise InternalPathProtectionError(f"{label} is missing")
    serialized = str(value)
    if (
        not serialized
        or serialized.strip() != serialized
        or len(serialized.encode("utf-8")) > 32_768
    ):
        raise InternalPathProtectionError(f"{label} is malformed")
    candidate = Path(serialized)
    if not candidate.is_absolute():
        raise InternalPathProtectionError(f"{label} is not absolute")
    normalized = Path(os.path.abspath(os.path.normpath(serialized)))
    if os.path.normcase(os.fspath(candidate)) != os.path.normcase(os.fspath(normalized)):
        raise InternalPathProtectionError(f"{label} is not canonical")
    return normalized


def corpus_mutation_guard(
    connection: sqlite3.Connection,
    run_id: int,
) -> CorpusMutationGuard:
    """Rebuild and verify the exact durable boundary for one immutable run."""

    row = connection.execute(
        """SELECT run_kind,corpus_access_mode,root,root_device_id_hex,
        root_file_id_hex,root_birthtime_ns,state_directory,
        inventory_policy_signature FROM main.initial_runs WHERE run_id=?""",
        (run_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"framework run does not exist: {run_id}")
    run_kind = str(row[0])
    mode = str(row[1])
    if run_kind not in {"initial", "route_only", "resume"}:
        raise InternalPathProtectionError(
            f"run {run_id} has unsupported mutation provenance: {run_kind!r}"
        )
    if run_kind == "initial" and mode != "normal":
        raise InternalPathProtectionError(f"run {run_id} has an inconsistent kind/access mode")
    if mode not in {"normal", "analyze_only"}:
        raise InternalPathProtectionError(
            f"run {run_id} has unsupported corpus access mode: {mode!r}"
        )
    root = _require_persisted_absolute_path(
        row[2],
        label=f"run {run_id} corpus root",
    )
    if any(value is None for value in row[3:6]):
        raise InternalPathProtectionError(f"run {run_id} has an incomplete corpus root identity")
    state_directory = _require_persisted_absolute_path(
        row[6],
        label=f"run {run_id} state directory",
    )
    signature = _mutation_inventory_signature(run_id, row[7])
    access_policy = CorpusAccessPolicy.from_storage(
        mode,
        root,
        str(row[3]),
        str(row[4]),
        int(row[5]),
    )
    if os.path.normcase(os.fspath(access_policy.root)) != os.path.normcase(os.fspath(root)):
        raise InternalPathProtectionError(
            f"run {run_id} corpus root is not in canonical storage form"
        )
    access_policy.verify_root_identity()
    state_policy = _main_database_state_policy(connection, state_directory)
    internal_paths_policy = canonical_internal_paths_policy()
    internal_paths_policy.verify_identities()
    internal_paths_policy.validate_corpus_access(access_policy)
    protected_content_policy = canonical_protected_content_policy()
    protected_content_policy.validate_corpus_access(access_policy)
    expected_signature, protected_content_policy = _mutation_inventory_boundary(
        run_id,
        mode,
        access_policy,
        state_policy,
        internal_paths_policy,
        protected_content_policy,
    )
    if signature != expected_signature:
        raise InternalPathProtectionError(
            f"run {run_id} inventory policy signature does not match its boundary"
        )
    access_policy.verify_root_identity()
    state_policy.verify_root_identity()
    internal_paths_policy.verify_identities()
    protected_content_policy.verify_identities()
    return CorpusMutationGuard(
        access_policy,
        internal_paths_policy,
        protected_content_policy,
    )


def _mutation_inventory_signature(run_id: int, value: object) -> str:
    if value is None:
        raise InternalPathProtectionError(f"run {run_id} has no durable inventory policy signature")
    signature = str(value)
    if not signature or signature.strip() != signature or len(signature.encode("utf-8")) > 4096:
        raise InternalPathProtectionError(
            f"run {run_id} has a malformed inventory policy signature"
        )
    return signature


def _mutation_inventory_boundary(
    run_id: int,
    mode: str,
    access_policy: CorpusAccessPolicy,
    state_policy: CorpusAccessPolicy,
    internal_paths_policy: InternalPathsPolicy,
    protected_content_policy: ProtectedContentPolicy,
) -> tuple[str, ProtectedContentPolicy]:
    """Rebuild the mode-specific inventory boundary for a verified run."""

    if mode == "normal":
        boundary = build_normal_inventory_boundary(
            access_policy.root,
            state_policy.root,
            access_policy=access_policy,
            state_policy=state_policy,
            internal_paths_policy=internal_paths_policy,
            protected_content_policy=protected_content_policy,
        )
        return boundary.effective_signature, boundary.protected_content_policy
    raise InternalPathProtectionError(
        f"run {run_id} uses retired analyze-only inventory mode"
    )


def _main_database_state_policy(
    connection: sqlite3.Connection,
    persisted_state_directory: Path,
) -> CorpusAccessPolicy:
    """Bind persisted state to the physical parent of SQLite's main database."""

    rows = connection.execute("PRAGMA database_list").fetchall()
    main_rows = [row for row in rows if len(row) >= 3 and str(row[1]) == "main"]
    if len(main_rows) != 1:
        raise InternalPathProtectionError("framework database has no unique main owner")
    database_value = str(main_rows[0][2])
    if not database_value or database_value == ":memory:":
        raise InternalPathProtectionError("framework database main owner is not a durable file")
    if not Path(database_value).is_absolute():
        raise InternalPathProtectionError("framework database main owner is not absolute")
    try:
        database_path = Path(os.path.abspath(os.path.realpath(Path(database_value).expanduser())))
        if not database_path.is_file():
            raise FileNotFoundError(database_path)
        database_owner = database_path.parent
        requested_state = persisted_state_directory
        physical_state = Path(os.path.abspath(os.path.realpath(requested_state)))
    except (OSError, ValueError) as exc:
        raise InternalPathProtectionError("framework state ownership cannot be verified") from exc
    if os.path.normcase(os.fspath(requested_state)) != os.path.normcase(os.fspath(physical_state)):
        raise InternalPathProtectionError("persisted state directory is not canonical")
    if os.path.normcase(os.fspath(physical_state)) != os.path.normcase(os.fspath(database_owner)):
        raise InternalPathProtectionError(
            "persisted state directory does not own the main database"
        )
    try:
        state_policy = CorpusAccessPolicy.capture("normal", database_owner)
    except (OSError, ValueError) as exc:
        raise InternalPathProtectionError(
            "framework state directory identity cannot be captured"
        ) from exc
    if os.path.normcase(os.fspath(state_policy.root)) != os.path.normcase(
        os.fspath(database_owner)
    ):
        raise InternalPathProtectionError("framework database owner is not a canonical directory")
    return state_policy


def _file_action_mutation_guard(
    connection: sqlite3.Connection,
    action_id: int,
    *,
    guard: CorpusMutationGuard | None = None,
    validate_paths: bool = True,
) -> CorpusMutationGuard:
    row = connection.execute(
        """SELECT action.run_id,action.corpus_access_mode,action.protected_root,
        action.protected_root_device_id_hex,action.protected_root_file_id_hex,
        action.protected_root_birthtime_ns,action.apply_requested,
        action.source_path,action.target_path
        FROM main.file_actions AS action
        WHERE action.action_id=?""",
        (action_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"file action does not exist: {action_id}")
    effective_guard = (
        corpus_mutation_guard(connection, int(row[0]))
        if guard is None
        else guard
    )
    actual_policy_values = (
        str(row[1]),
        None if row[2] is None else str(row[2]),
        None if row[3] is None else str(row[3]),
        None if row[4] is None else str(row[4]),
        None if row[5] is None else int(row[5]),
    )
    if actual_policy_values != _action_policy_values(effective_guard.policy):
        raise InternalPathProtectionError(
            f"file action {action_id} policy snapshot does not match its run"
        )
    if type(row[6]) is not int or int(row[6]) != 1:
        raise InternalPathProtectionError(
            f"file action {action_id} was not explicitly authorized for apply"
        )
    if validate_paths:
        effective_guard.require_paths_allowed(
            str(row[7]),
            None if row[8] is None else str(row[8]),
        )
    return effective_guard


def _action_policy_values(
    policy: CorpusAccessPolicy,
) -> tuple[str, str | None, str | None, str | None, int | None]:
    if policy.mode == "normal":
        return ("normal", None, None, None, None)
    return (
        policy.mode,
        str(policy.root),
        policy.root_device_id_hex,
        policy.root_file_id_hex,
        policy.root_birthtime_ns,
    )


def _append_file_action_event(
    connection: sqlite3.Connection,
    action_id: int,
    *,
    occurred_ns: int,
    from_status: str | None,
    to_status: str,
    stage: str,
    detail: str | None = None,
    evidence_json: str | None = None,
) -> None:
    connection.execute(
        """INSERT INTO main.file_action_events(
        action_id,occurred_ns,from_status,to_status,stage,detail,evidence_json)
        VALUES(?,?,?,?,?,?,?)""",
        (
            action_id,
            occurred_ns,
            from_status,
            to_status,
            stage,
            detail,
            evidence_json,
        ),
    )


def begin_file_actions(
    connection: sqlite3.Connection,
    run_id: int,
    actions: Iterable[FileActionSpec],
) -> list[int]:
    """Insert an action batch through either framework connection view."""

    guard = corpus_mutation_guard(connection, run_id)
    guard.reject_run_mutation()
    prepared = tuple(actions)
    path_values = tuple(
        path
        for action in prepared
        for path in (action[1], action[2])
    )
    # Validate the complete batch before inserting its intents.  A final
    # validation below closes the same transaction, preserving the fail-closed
    # boundary while avoiding a full policy-identity walk for every member.
    guard.require_paths_allowed(*path_values)
    policy_values = _action_policy_values(guard.policy)
    action_ids: list[int] = []
    with connection:
        for (
            action_type,
            source_path,
            target_path,
            detected_mime,
            evidence,
            apply_requested,
        ) in prepared:
            started_ns = time.time_ns()
            idempotency_key = _action_idempotency_key(
                run_id,
                action_type,
                source_path,
                target_path,
                detected_mime,
                evidence,
                apply_requested,
            )
            expected = (
                run_id,
                action_type,
                source_path,
                target_path,
                detected_mime,
                evidence,
                int(apply_requested),
                *policy_values,
            )
            # Releases before the backend split stored XXH3-derived keys.  A
            # retry under the SHA-256 key must still resolve that same action
            # rather than create a second durable mutation intent.
            legacy = connection.execute(
                """SELECT action_id,run_id,action_type,source_path,target_path,
                detected_mime,evidence,apply_requested,corpus_access_mode,
                protected_root,protected_root_device_id_hex,
                protected_root_file_id_hex,protected_root_birthtime_ns
                FROM main.file_actions
                WHERE run_id=? AND action_type=? AND source_path=?
                  AND target_path IS ? AND detected_mime IS ? AND evidence IS ?
                  AND apply_requested=?
                ORDER BY action_id LIMIT 1""",
                (
                    run_id,
                    action_type,
                    source_path,
                    target_path,
                    detected_mime,
                    evidence,
                    int(apply_requested),
                ),
            ).fetchone()
            if legacy is not None:
                if tuple(legacy[1:]) != expected:
                    raise RuntimeError("file action identity collision")
                action_ids.append(int(legacy[0]))
                continue
            result = connection.execute(
                "INSERT INTO main.file_actions(run_id, action_type, source_path, target_path, "
                "detected_mime, evidence, apply_requested, status, started_ns, "
                "idempotency_key,corpus_access_mode,protected_root,"
                "protected_root_device_id_hex,protected_root_file_id_hex,"
                "protected_root_birthtime_ns) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, 'started', ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(idempotency_key) DO NOTHING",
                (
                    run_id,
                    action_type,
                    source_path,
                    target_path,
                    detected_mime,
                    evidence,
                    int(apply_requested),
                    started_ns,
                    idempotency_key,
                    *policy_values,
                ),
            )
            if result.rowcount == 0:
                existing = connection.execute(
                    """SELECT action_id,run_id,action_type,source_path,target_path,
                    detected_mime,evidence,apply_requested,corpus_access_mode,
                    protected_root,protected_root_device_id_hex,
                    protected_root_file_id_hex,protected_root_birthtime_ns
                    FROM main.file_actions
                    WHERE idempotency_key=?""",
                    (idempotency_key,),
                ).fetchone()
                if existing is None or tuple(existing[1:]) != expected:
                    raise RuntimeError("file action idempotency-key collision")
                action_ids.append(int(existing[0]))
                continue
            if result.lastrowid is None:
                raise RuntimeError("SQLite did not return a file-action identifier")
            action_id = int(result.lastrowid)
            _append_file_action_event(
                connection,
                action_id,
                occurred_ns=started_ns,
                from_status=None,
                to_status="started",
                stage="intent_recorded",
            )
            action_ids.append(action_id)
        guard.reject_run_mutation()
        guard.require_paths_allowed(*path_values)
    return action_ids


def mark_file_actions_applying(
    connection: sqlite3.Connection,
    actions: Iterable[tuple[int, str]],
) -> None:
    """Atomically persist expected identity and cross the mutation frontier."""

    prepared = tuple(actions)
    for _action_id, identity_json in prepared:
        _require_json_object(identity_json, label="expected identity evidence")
    if not prepared:
        return
    if connection.in_transaction:
        raise RuntimeError("file action mutation frontier requires transaction ownership")
    # SQLite WAL NORMAL can acknowledge COMMIT without syncing the frontier.
    # Enforce FULL before BEGIN; keep it for subsequent effect receipts as well.
    if int(connection.execute("PRAGMA main.synchronous").fetchone()[0]) < 2:
        connection.execute("PRAGMA main.synchronous=FULL")
    if int(connection.execute("PRAGMA main.synchronous").fetchone()[0]) < 2:
        raise RuntimeError("file action mutation frontier requires synchronous FULL")
    applying_ns = time.time_ns()
    try:
        connection.execute("BEGIN IMMEDIATE")
        # All actions in a normal Framework batch share one run.  Rehydrate
        # each run guard at most once, validate the per-row policy snapshot
        # without repeating expensive identity walks, and check all paths in
        # one bounded call before crossing the mutation frontier.
        guards: dict[int, CorpusMutationGuard] = {}
        action_runs: dict[int, int] = {}
        paths_by_run: dict[int, list[str | None]] = {}
        for action_id, _identity_json in prepared:
            row = connection.execute(
                "SELECT run_id,source_path,target_path FROM main.file_actions "
                "WHERE action_id=?",
                (action_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"file action does not exist: {action_id}")
            action_run_id = int(row[0])
            guard = guards.get(action_run_id)
            if guard is None:
                guard = corpus_mutation_guard(connection, action_run_id)
                guards[action_run_id] = guard
            _file_action_mutation_guard(
                connection,
                action_id,
                guard=guard,
                validate_paths=False,
            )
            action_runs[action_id] = action_run_id
            paths_by_run.setdefault(action_run_id, []).extend(
                (str(row[1]), None if row[2] is None else str(row[2]))
            )
        for action_run_id, guard in guards.items():
            guard.reject_run_mutation()
            guard.require_paths_allowed(*paths_by_run[action_run_id])
        for action_id, identity_json in prepared:
            updated = connection.execute(
                """UPDATE main.file_actions SET status='applying',
                expected_identity_json=?,applying_ns=?,completed_ns=NULL
                WHERE action_id=? AND status='started'""",
                (identity_json, applying_ns, action_id),
            )
            if updated.rowcount != 1:
                row = connection.execute(
                    "SELECT status FROM main.file_actions WHERE action_id=?",
                    (action_id,),
                ).fetchone()
                current = None if row is None else str(row[0])
                raise RuntimeError(
                    f"file action {action_id} cannot enter applying from {current!r}"
                )
            _append_file_action_event(
                connection,
                action_id,
                occurred_ns=applying_ns,
                from_status="started",
                to_status="applying",
                stage="mutation_frontier",
                evidence_json=identity_json,
            )
        for action_id, _identity_json in prepared:
            guard = guards[action_runs[action_id]]
            _file_action_mutation_guard(
                connection,
                action_id,
                guard=guard,
                validate_paths=False,
            )
        for action_run_id, guard in guards.items():
            guard.reject_run_mutation()
            guard.require_paths_allowed(*paths_by_run[action_run_id])
        # COMMIT is part of the protected frontier, not an unguarded else.
        connection.commit()
    except BaseException as exc:
        try:
            # sqlite3.rollback() is also a no-op when BEGIN did not succeed.
            connection.rollback()
        except BaseException as rollback_error:
            # A failed rollback must not leave a connection that can later
            # commit applying rows after this API has denied the effect.
            exc.add_note(f"mutation-frontier rollback failed: {rollback_error!r}")
            try:
                connection.close()
            except BaseException as close_error:
                exc.add_note(f"mutation-frontier connection close failed: {close_error!r}")
        exc.add_note("Mutation frontier was not acknowledged; do not apply or retry filesystem effects without reconciliation.")
        raise


def _transition_file_action(
    connection: sqlite3.Connection,
    action_id: int,
    status: str,
    detail: str | None,
    *,
    stage: str,
    evidence_json: str | None = None,
    effect_receipt_json: str | None = None,
) -> None:
    row = connection.execute(
        "SELECT status,detail,effect_receipt_json FROM main.file_actions WHERE action_id=?",
        (action_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"file action does not exist: {action_id}")
    current = str(row[0])
    if current == status:
        stored_detail = None if row[1] is None else str(row[1])
        stored_receipt = None if row[2] is None else str(row[2])
        expected_receipt = stored_receipt if effect_receipt_json is None else effect_receipt_json
        if stored_detail != detail or stored_receipt != expected_receipt:
            raise RuntimeError(
                f"conflicting repeated file action transition for {action_id}: status={status}"
            )
        return
    allowed = _FILE_ACTION_TRANSITIONS.get(current, frozenset())
    if status not in allowed:
        raise RuntimeError(f"invalid file action transition for {action_id}: {current} -> {status}")
    occurred_ns = time.time_ns()
    completed_ns = occurred_ns if status in _TERMINAL_FILE_ACTION_STATUSES else None
    updated = connection.execute(
        """UPDATE main.file_actions SET status=?,detail=?,completed_ns=?,
        effect_receipt_json=COALESCE(?,effect_receipt_json)
        WHERE action_id=? AND status=?""",
        (
            status,
            detail,
            completed_ns,
            effect_receipt_json,
            action_id,
            current,
        ),
    )
    if updated.rowcount != 1:
        raise RuntimeError(f"concurrent file action transition detected: {action_id}")
    _append_file_action_event(
        connection,
        action_id,
        occurred_ns=occurred_ns,
        from_status=current,
        to_status=status,
        stage=stage,
        detail=detail,
        evidence_json=evidence_json,
    )


def finish_file_actions(
    connection: sqlite3.Connection,
    action_ids: Iterable[int],
    status: str,
    detail: str | None,
) -> None:
    """Complete an action batch through either framework connection view."""

    if status not in {"planned", "applied", "skipped", "failed", "recovery_required"}:
        raise ValueError(f"invalid file action status: {status}")
    ids = tuple(action_ids)
    with connection:
        for action_id in ids:
            _transition_file_action(
                connection,
                action_id,
                status,
                detail,
                stage=(
                    "recovery_required"
                    if status == "recovery_required"
                    else "completed_without_effect"
                    if status in {"planned", "skipped", "failed"}
                    else "effect_confirmed"
                ),
            )


def confirm_file_actions_applied(
    connection: sqlite3.Connection,
    actions: Iterable[tuple[int, str]],
) -> None:
    """Durably confirm effects only from the explicit applying state."""

    confirmed = tuple(actions)
    for _action_id, receipt_json in confirmed:
        _require_json_object(receipt_json, label="file action effect receipt")
    with connection:
        for action_id, receipt_json in confirmed:
            _transition_file_action(
                connection,
                action_id,
                "applied",
                None,
                stage="effect_confirmed",
                evidence_json=receipt_json,
                effect_receipt_json=receipt_json,
            )


# endregion [02]
