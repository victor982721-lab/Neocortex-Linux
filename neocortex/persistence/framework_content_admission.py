"""Durable Semantic content-admission ledger inside Framework SQLite.

The Framework owner is already the single writer for corpus policy and
orchestration state.  This optional extension keeps admission policy,
correction events and diagnostic evidence in that same owner; it deliberately
does not create another database and stores fingerprints/identities rather
than source content.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import sqlite3
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from neocortex.persistence.sqlite_schema_contract import (
    SQLiteSchemaContract,
    SQLiteSchemaContractError,
    schema_contract_from_builder,
    validate_sqlite_schema_contract,
)


def _semantic_api():
    """Load the Semantic value objects lazily to keep the owner graph acyclic."""

    return importlib.import_module("neocortex.semantic.semantic_admission")


# region [01] Extension schema and bounded storage helpers


CONTENT_ADMISSION_EXTENSION_SCHEMA_VERSION = 1
CONTENT_ADMISSION_POLICIES_TABLE = "semantic_content_admission_policies"
CONTENT_ADMISSION_EVENTS_TABLE = "semantic_content_admission_events"
CONTENT_ADMISSION_POLICIES_INDEX = "semantic_content_admission_policies_head_idx"
CONTENT_ADMISSION_EVENTS_CURRENT_INDEX = "semantic_content_admission_events_current_idx"
CONTENT_ADMISSION_EVENTS_VISIBLE_INDEX = "semantic_content_admission_events_visible_idx"
CONTENT_ADMISSION_POLICIES_NO_UPDATE_TRIGGER = "semantic_content_admission_policies_no_update"
CONTENT_ADMISSION_POLICIES_NO_DELETE_TRIGGER = "semantic_content_admission_policies_no_delete"
CONTENT_ADMISSION_EVENTS_NO_UPDATE_TRIGGER = "semantic_content_admission_events_no_update"
CONTENT_ADMISSION_EVENTS_NO_DELETE_TRIGGER = "semantic_content_admission_events_no_delete"

CONTENT_ADMISSION_EXTENSION_OBJECTS = frozenset(
    {
        CONTENT_ADMISSION_POLICIES_TABLE,
        CONTENT_ADMISSION_EVENTS_TABLE,
        CONTENT_ADMISSION_POLICIES_NO_UPDATE_TRIGGER,
        CONTENT_ADMISSION_POLICIES_NO_DELETE_TRIGGER,
        CONTENT_ADMISSION_EVENTS_NO_UPDATE_TRIGGER,
        CONTENT_ADMISSION_EVENTS_NO_DELETE_TRIGGER,
    }
)
CONTENT_ADMISSION_EXTENSION_TABLES = frozenset(
    {CONTENT_ADMISSION_POLICIES_TABLE, CONTENT_ADMISSION_EVENTS_TABLE}
)

_POLICY_COLUMNS = (
    "corpus_key",
    "policy_version",
    "policy_signature",
    "corpus_identity_json",
    "policy_json",
    "recorded_ns",
)
_EVENT_COLUMNS = (
    "admission_id",
    "entry_key",
    "corpus_key",
    "subject_key",
    "source_kind",
    "source_identity",
    "policy_version",
    "policy_signature",
    "physical_identity_json",
    "virtual_identity_json",
    "content_identity_json",
    "work_identity_json",
    "eligible",
    "visible",
    "reason_code",
    "correction_of_id",
    "diagnostics_json",
    "recorded_ns",
)

CONTENT_ADMISSION_POLICIES_TABLE_STATEMENT = """
CREATE TABLE IF NOT EXISTS semantic_content_admission_policies (
    corpus_key TEXT NOT NULL CHECK(length(trim(corpus_key)) BETWEEN 1 AND 2048),
    policy_version INTEGER NOT NULL CHECK(policy_version>=1),
    policy_signature TEXT NOT NULL CHECK(length(trim(policy_signature)) BETWEEN 1 AND 1024),
    corpus_identity_json TEXT NOT NULL CHECK(
        json_valid(corpus_identity_json) AND
        json_type(corpus_identity_json)='object' AND
        length(CAST(corpus_identity_json AS BLOB)) BETWEEN 2 AND 65536
    ),
    policy_json TEXT NOT NULL CHECK(
        json_valid(policy_json) AND
        json_type(policy_json)='object' AND
        length(CAST(policy_json AS BLOB)) BETWEEN 2 AND 262144
    ),
    recorded_ns INTEGER NOT NULL CHECK(recorded_ns>=0),
    PRIMARY KEY(corpus_key,policy_version),
    UNIQUE(corpus_key,policy_signature)
) WITHOUT ROWID
"""

CONTENT_ADMISSION_EVENTS_TABLE_STATEMENT = """
CREATE TABLE IF NOT EXISTS semantic_content_admission_events (
    admission_id INTEGER PRIMARY KEY,
    entry_key TEXT NOT NULL UNIQUE CHECK(length(trim(entry_key)) BETWEEN 1 AND 1024),
    corpus_key TEXT NOT NULL CHECK(length(trim(corpus_key)) BETWEEN 1 AND 2048),
    subject_key TEXT NOT NULL CHECK(length(trim(subject_key)) BETWEEN 1 AND 2048),
    source_kind TEXT NOT NULL CHECK(length(trim(source_kind)) BETWEEN 1 AND 256),
    source_identity TEXT NOT NULL CHECK(length(trim(source_identity)) BETWEEN 1 AND 2048),
    policy_version INTEGER NOT NULL CHECK(policy_version>=1),
    policy_signature TEXT NOT NULL CHECK(length(trim(policy_signature)) BETWEEN 1 AND 1024),
    physical_identity_json TEXT CHECK(
        physical_identity_json IS NULL OR (
            json_valid(physical_identity_json) AND
            json_type(physical_identity_json)='object' AND
            length(CAST(physical_identity_json AS BLOB)) BETWEEN 2 AND 65536
        )
    ),
    virtual_identity_json TEXT CHECK(
        virtual_identity_json IS NULL OR (
            json_valid(virtual_identity_json) AND
            json_type(virtual_identity_json)='object' AND
            length(CAST(virtual_identity_json AS BLOB)) BETWEEN 2 AND 65536
        )
    ),
    content_identity_json TEXT NOT NULL CHECK(
        json_valid(content_identity_json) AND
        json_type(content_identity_json)='object' AND
        length(CAST(content_identity_json AS BLOB)) BETWEEN 2 AND 65536
    ),
    work_identity_json TEXT CHECK(
        work_identity_json IS NULL OR (
            json_valid(work_identity_json) AND
            json_type(work_identity_json)='object' AND
            length(CAST(work_identity_json AS BLOB)) BETWEEN 2 AND 65536
        )
    ),
    eligible INTEGER NOT NULL CHECK(eligible IN (0,1)),
    visible INTEGER NOT NULL CHECK(visible IN (0,1)),
    reason_code TEXT NOT NULL CHECK(length(trim(reason_code)) BETWEEN 1 AND 256),
    correction_of_id INTEGER,
    diagnostics_json TEXT NOT NULL CHECK(
        json_valid(diagnostics_json) AND
        json_type(diagnostics_json)='object' AND
        length(CAST(diagnostics_json AS BLOB)) BETWEEN 2 AND 262144
    ),
    recorded_ns INTEGER NOT NULL CHECK(recorded_ns>=0),
    FOREIGN KEY(corpus_key,policy_version)
        REFERENCES semantic_content_admission_policies(corpus_key,policy_version)
        ON DELETE RESTRICT,
    FOREIGN KEY(correction_of_id)
        REFERENCES semantic_content_admission_events(admission_id)
        ON DELETE RESTRICT,
    CHECK(visible=0 OR eligible=1)
)
"""

CONTENT_ADMISSION_POLICIES_INDEX_STATEMENT = """
CREATE INDEX IF NOT EXISTS semantic_content_admission_policies_head_idx
ON semantic_content_admission_policies(corpus_key,policy_version DESC)
"""

CONTENT_ADMISSION_EVENTS_CURRENT_INDEX_STATEMENT = """
CREATE INDEX IF NOT EXISTS semantic_content_admission_events_current_idx
ON semantic_content_admission_events(corpus_key,subject_key,recorded_ns DESC,admission_id DESC)
"""

CONTENT_ADMISSION_EVENTS_VISIBLE_INDEX_STATEMENT = """
CREATE INDEX IF NOT EXISTS semantic_content_admission_events_visible_idx
ON semantic_content_admission_events(corpus_key,visible,subject_key,admission_id DESC)
"""

CONTENT_ADMISSION_POLICIES_NO_UPDATE_TRIGGER_STATEMENT = """
CREATE TRIGGER IF NOT EXISTS semantic_content_admission_policies_no_update
BEFORE UPDATE ON semantic_content_admission_policies
BEGIN
    SELECT RAISE(ABORT, 'semantic content admission policies are append-only');
END
"""

CONTENT_ADMISSION_POLICIES_NO_DELETE_TRIGGER_STATEMENT = """
CREATE TRIGGER IF NOT EXISTS semantic_content_admission_policies_no_delete
BEFORE DELETE ON semantic_content_admission_policies
BEGIN
    SELECT RAISE(ABORT, 'semantic content admission policies are append-only');
END
"""

CONTENT_ADMISSION_EVENTS_NO_UPDATE_TRIGGER_STATEMENT = """
CREATE TRIGGER IF NOT EXISTS semantic_content_admission_events_no_update
BEFORE UPDATE ON semantic_content_admission_events
BEGIN
    SELECT RAISE(ABORT, 'semantic content admission events are append-only');
END
"""

CONTENT_ADMISSION_EVENTS_NO_DELETE_TRIGGER_STATEMENT = """
CREATE TRIGGER IF NOT EXISTS semantic_content_admission_events_no_delete
BEFORE DELETE ON semantic_content_admission_events
BEGIN
    SELECT RAISE(ABORT, 'semantic content admission events are append-only');
END
"""


def _build_content_admission_extension(connection: sqlite3.Connection) -> None:
    for statement in (
        CONTENT_ADMISSION_POLICIES_TABLE_STATEMENT,
        CONTENT_ADMISSION_EVENTS_TABLE_STATEMENT,
        CONTENT_ADMISSION_POLICIES_INDEX_STATEMENT,
        CONTENT_ADMISSION_EVENTS_CURRENT_INDEX_STATEMENT,
        CONTENT_ADMISSION_EVENTS_VISIBLE_INDEX_STATEMENT,
        CONTENT_ADMISSION_POLICIES_NO_UPDATE_TRIGGER_STATEMENT,
        CONTENT_ADMISSION_POLICIES_NO_DELETE_TRIGGER_STATEMENT,
        CONTENT_ADMISSION_EVENTS_NO_UPDATE_TRIGGER_STATEMENT,
        CONTENT_ADMISSION_EVENTS_NO_DELETE_TRIGGER_STATEMENT,
    ):
        connection.execute(statement)


@lru_cache(maxsize=1)
def content_admission_extension_schema_contract() -> SQLiteSchemaContract:
    return schema_contract_from_builder(_build_content_admission_extension)


def create_content_admission_extension(connection: sqlite3.Connection) -> None:
    """Create the extension in the existing Framework transaction."""

    _build_content_admission_extension(connection)


def content_admission_extension_present(connection: sqlite3.Connection) -> bool:
    row = connection.execute(
        "SELECT type FROM sqlite_master WHERE name=? LIMIT 1",
        (CONTENT_ADMISSION_POLICIES_TABLE,),
    ).fetchone()
    return row is not None and str(row[0]) == "table"


def validate_content_admission_extension(connection: sqlite3.Connection) -> None:
    """Validate the optional extension without creating or repairing it."""

    if not content_admission_extension_present(connection):
        return
    try:
        extra_tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
            if str(row[0]) not in CONTENT_ADMISSION_EXTENSION_TABLES
        }
        extra_objects = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('view','trigger') AND name NOT LIKE 'sqlite_%'"
            )
            if str(row[0]) not in CONTENT_ADMISSION_EXTENSION_OBJECTS
        }
        validate_sqlite_schema_contract(
            connection,
            content_admission_extension_schema_contract(),
            label="Framework Semantic content-admission extension",
            exact=True,
            allowed_extra_tables=extra_tables,
            allowed_extra_objects=extra_objects,
        )
    except SQLiteSchemaContractError as exc:
        raise RuntimeError(f"Framework Semantic content-admission extension is invalid: {exc}") from exc


# endregion [01]


# region [02] Typed ledger records and canonical persistence


@dataclass(frozen=True, slots=True)
class StoredContentAdmissionPolicy:
    corpus_key: str
    policy: Any
    corpus_identity: Mapping[str, object]
    recorded_ns: int


@dataclass(frozen=True, slots=True)
class StoredContentAdmission:
    admission_id: int
    entry_key: str
    corpus_key: str
    subject_key: str
    policy_signature: str
    policy_version: int
    identity: Any
    eligible: bool
    visible: bool
    reason_code: str
    diagnostics: Mapping[str, object]
    recorded_ns: int
    correction_of_id: int | None = None

    @property
    def excluded(self) -> bool:
        return not self.visible


def _json_object(value: object, *, label: str, maximum_bytes: int = 262_144) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} is not JSON-compatible") from exc
    if not encoded or len(encoded.encode("utf-8")) > maximum_bytes:
        raise ValueError(f"{label} exceeds its bound")
    return dict(value)


def _row_mapping(row: object, columns: tuple[str, ...]) -> Mapping[str, object]:
    """Normalize Framework's tuple rows and optional sqlite3.Row rows."""

    if isinstance(row, sqlite3.Row):
        return {column: row[column] for column in columns}
    if isinstance(row, Mapping):
        return row
    if not isinstance(row, (tuple, list)) or len(row) != len(columns):
        raise ValueError("stored admission row has an unexpected shape")
    return dict(zip(columns, row, strict=True))


def _policy_from_payload(payload: object) -> Any:
    if not isinstance(payload, Mapping):
        raise ValueError("persisted admission policy is not an object")
    values = dict(payload)
    values.pop("schema", None)
    persisted_signature = values.pop("policy_signature", None)
    policy = _semantic_api().ContentAdmissionPolicy(**values)
    if persisted_signature is not None and str(persisted_signature) != policy.signature:
        raise ValueError("persisted admission policy signature does not match its payload")
    return policy


def _identity_from_payload(payload: object) -> Any:
    """Decode a ledger identity with strict separation and no source content."""

    if not isinstance(payload, Mapping):
        raise ValueError("persisted admission identity is not an object")
    semantic = _semantic_api()

    def nested(name: str) -> Mapping[str, object] | None:
        value = payload.get(name)
        if value is None:
            return None
        if not isinstance(value, Mapping):
            raise ValueError(f"persisted {name} identity is malformed")
        return value

    raw_content = nested("content")
    if raw_content is None:
        raise ValueError("persisted content identity is missing")
    content = semantic.ContentIdentity(
        str(raw_content.get("xxh3_128", "")),
        int(raw_content.get("byte_count", -1)),
        str(raw_content.get("xxh3_64_guard", "")),
    )
    raw_physical = nested("physical")
    physical = (
        None
        if raw_physical is None
        else semantic.PhysicalIdentity(
            str(raw_physical.get("scheme", "")),
            str(raw_physical.get("value", "")),
            int(raw_physical.get("version", 1)),
        )
    )
    raw_virtual = nested("virtual")
    virtual = (
        None
        if raw_virtual is None
        else semantic.VirtualIdentity(
            str(raw_virtual.get("scheme", "")),
            str(raw_virtual.get("container", "")),
            str(raw_virtual.get("member", "")),
            int(raw_virtual.get("version", 1)),
        )
    )
    raw_work = nested("work")
    work = None
    if raw_work is not None:
        work_content = nested_from(raw_work, "content")
        if work_content is None:
            raise ValueError("persisted work identity content is missing")
        work_content_identity = semantic.ContentIdentity(
            str(work_content.get("xxh3_128", "")),
            int(work_content.get("byte_count", -1)),
            str(work_content.get("xxh3_64_guard", "")),
        )
        work = semantic.WorkIdentity(
            str(raw_work.get("model_signature", "")),
            str(raw_work.get("role", "")),
            work_content_identity,
            str(raw_work.get("processing_signature", "")),
        )
    return semantic.SemanticIdentity(
        str(payload.get("source_kind", "")),
        str(payload.get("source_identity", "")),
        str(payload.get("item_id", "")),
        content,
        physical,
        virtual,
        work,
    )


def nested_from(value: Mapping[str, object], name: str) -> Mapping[str, object] | None:
    selected = value.get(name)
    if selected is None:
        return None
    if not isinstance(selected, Mapping):
        raise ValueError(f"persisted nested identity {name} is malformed")
    return selected


def _policy_corpus_identity(corpus: object) -> tuple[str, dict[str, object]]:
    key = _semantic_api().corpus_key_for(corpus)
    if isinstance(corpus, str):
        return key, {"corpus_key": key}
    identity = {
        "root": str(corpus.root),
        "root_device_id": getattr(corpus, "root_device_id", None),
        "root_file_id": getattr(corpus, "root_file_id", None),
        "root_birthtime_ns": getattr(corpus, "root_birthtime_ns", None),
    }
    return key, identity


def _record_from_row(row: object) -> StoredContentAdmission:
    row = _row_mapping(row, _EVENT_COLUMNS)
    try:
        identity_payload = {
            "schema": "neocortex.semantic-identities/v1",
            "source_kind": str(row["source_kind"]),
            "source_identity": str(row["source_identity"]),
            "item_id": str(row["subject_key"]),
            "physical": json.loads(str(row["physical_identity_json"]))
            if row["physical_identity_json"] is not None
            else None,
            "virtual": json.loads(str(row["virtual_identity_json"]))
            if row["virtual_identity_json"] is not None
            else None,
            "content": json.loads(str(row["content_identity_json"])),
            "work": json.loads(str(row["work_identity_json"]))
            if row["work_identity_json"] is not None
            else None,
        }
        identity = _identity_from_payload(identity_payload)
        diagnostics = json.loads(str(row["diagnostics_json"]))
        if not isinstance(diagnostics, dict):
            raise ValueError("admission diagnostics are not an object")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("stored content admission is malformed") from exc
    return StoredContentAdmission(
        admission_id=int(row["admission_id"]),
        entry_key=str(row["entry_key"]),
        corpus_key=str(row["corpus_key"]),
        subject_key=str(row["subject_key"]),
        policy_signature=str(row["policy_signature"]),
        policy_version=int(row["policy_version"]),
        identity=identity,
        eligible=bool(row["eligible"]),
        visible=bool(row["visible"]),
        reason_code=str(row["reason_code"]),
        diagnostics=diagnostics,
        recorded_ns=int(row["recorded_ns"]),
        correction_of_id=(None if row["correction_of_id"] is None else int(row["correction_of_id"])),
    )


class ContentAdmissionLedger:
    """Single-writer ledger backed by an existing Framework owner connection."""

    def __init__(self, owner: object | sqlite3.Connection):
        if isinstance(owner, sqlite3.Connection):
            self._connection = owner
        else:
            connection = getattr(owner, "_connection", None)
            if not isinstance(connection, sqlite3.Connection):
                raise TypeError("content admission requires FrameworkState or sqlite3.Connection")
            self._connection = connection
        create_content_admission_extension(self._connection)

    @property
    def connection(self) -> sqlite3.Connection:
        """Expose the owner connection for root wiring, never a new database."""

        return self._connection

    def record_policy(
        self,
        corpus: object,
        policy: Any,
        *,
        recorded_ns: int | None = None,
    ) -> StoredContentAdmissionPolicy:
        if not isinstance(policy, _semantic_api().ContentAdmissionPolicy):
            raise TypeError("admission policy must be a ContentAdmissionPolicy")
        corpus_key, corpus_identity = _policy_corpus_identity(corpus)
        selected_ns = time.time_ns() if recorded_ns is None else recorded_ns
        if type(selected_ns) is not int or selected_ns < 0:
            raise ValueError("recorded_ns must be non-negative")
        payload = policy.as_payload()
        policy_json = canonical_json(payload)
        identity_json = canonical_json(corpus_identity)
        existing = self._connection.execute(
            """SELECT policy_json,policy_signature,recorded_ns
            FROM semantic_content_admission_policies
            WHERE corpus_key=? AND policy_version=?""",
            (corpus_key, policy.version),
        ).fetchone()
        if existing is not None:
            if str(existing[0]) != policy_json or str(existing[1]) != policy.signature:
                raise ValueError("admission policy version is already bound to another payload")
            return StoredContentAdmissionPolicy(
                corpus_key,
                policy,
                corpus_identity,
                int(existing[2]),
            )
        prior = self._connection.execute(
            """SELECT MAX(policy_version) FROM semantic_content_admission_policies
            WHERE corpus_key=?""",
            (corpus_key,),
        ).fetchone()
        if prior is not None and prior[0] is not None and policy.version != int(prior[0]) + 1:
            raise ValueError("admission policy versions must advance one step at a time")
        with self._connection:
            self._connection.execute(
                """INSERT INTO semantic_content_admission_policies(
                corpus_key,policy_version,policy_signature,corpus_identity_json,
                policy_json,recorded_ns) VALUES(?,?,?,?,?,?)""",
                (corpus_key, policy.version, policy.signature, identity_json, policy_json, selected_ns),
            )
        return StoredContentAdmissionPolicy(corpus_key, policy, corpus_identity, selected_ns)

    set_policy = record_policy

    def read_policy(self, corpus: object) -> StoredContentAdmissionPolicy | None:
        corpus_key = _semantic_api().corpus_key_for(corpus)
        row = self._connection.execute(
            f"""SELECT {','.join(_POLICY_COLUMNS)}
            FROM semantic_content_admission_policies
            WHERE corpus_key=? ORDER BY policy_version DESC LIMIT 1""",
            (corpus_key,),
        ).fetchone()
        if row is None:
            return None
        row = _row_mapping(row, _POLICY_COLUMNS)
        try:
            policy = _policy_from_payload(json.loads(str(row["policy_json"])))
            identity = _json_object(json.loads(str(row["corpus_identity_json"])), label="corpus identity")
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("stored admission policy is malformed") from exc
        if policy.version != int(row["policy_version"]) or policy.signature != str(row["policy_signature"]):
            raise ValueError("stored admission policy metadata contradicts its payload")
        return StoredContentAdmissionPolicy(str(row["corpus_key"]), policy, identity, int(row["recorded_ns"]))

    def record_admission(
        self,
        corpus: object,
        subject: Any,
        *,
        policy: Any = None,
        decision: Any = None,
        correction_of_id: int | None = None,
        entry_key: str | None = None,
        diagnostics: Mapping[str, object] | None = None,
        recorded_ns: int | None = None,
    ) -> StoredContentAdmission:
        semantic = _semantic_api()
        corpus_key = semantic.corpus_key_for(corpus)
        stored_policy = self.read_policy(corpus)
        selected_policy = policy or (None if stored_policy is None else stored_policy.policy)
        if selected_policy is None:
            raise ValueError("no admission policy is persisted for this corpus")
        if stored_policy is None or stored_policy.policy.signature != selected_policy.signature:
            self.record_policy(corpus, selected_policy, recorded_ns=recorded_ns)
        identity = (
            semantic.semantic_identity_for_item(subject)
            if isinstance(subject, semantic.SemanticItem)
            else subject
        )
        if not isinstance(identity, semantic.SemanticIdentity):
            raise TypeError("admission subject must be a SemanticItem or SemanticIdentity")
        selected_decision = decision or selected_policy.evaluate(identity)
        if selected_decision.identity != identity:
            raise ValueError("admission decision identity does not match subject")
        if selected_decision.policy_signature != selected_policy.signature:
            raise ValueError("admission decision policy does not match current policy")
        selected_ns = time.time_ns() if recorded_ns is None else recorded_ns
        if type(selected_ns) is not int or selected_ns < 0:
            raise ValueError("recorded_ns must be non-negative")
        selected_diagnostics = dict(selected_decision.diagnostics)
        if diagnostics is not None:
            selected_diagnostics.update(dict(diagnostics))
        selected_diagnostics = _json_object(selected_diagnostics, label="admission diagnostics")
        if entry_key is None:
            digest_input = canonical_json(
                {
                    "corpus_key": corpus_key,
                    "subject_key": identity.subject_key,
                    "policy_signature": selected_policy.signature,
                    "identity": identity.as_payload(),
                    "eligible": selected_decision.eligible,
                    "visible": selected_decision.visible,
                    "reason_code": selected_decision.reason_code,
                    "correction_of_id": correction_of_id,
                }
            ).encode("utf-8")
            entry_key = "semantic-admission-v1:sha256:" + hashlib.sha256(digest_input).hexdigest()
        if not isinstance(entry_key, str) or not entry_key.strip() or len(entry_key) > 1024:
            raise ValueError("admission entry_key is invalid")
        existing = self._connection.execute(
            f"SELECT {','.join(_EVENT_COLUMNS)} FROM semantic_content_admission_events WHERE entry_key=?",
            (entry_key,),
        ).fetchone()
        if existing is not None:
            return _record_from_row(existing)
        if correction_of_id is not None:
            if type(correction_of_id) is not int or correction_of_id < 1:
                raise ValueError("correction_of_id must be positive")
            prior = self._connection.execute(
                """SELECT corpus_key,subject_key FROM semantic_content_admission_events
                WHERE admission_id=?""",
                (correction_of_id,),
            ).fetchone()
            if prior is None or str(prior[0]) != corpus_key or str(prior[1]) != identity.subject_key:
                raise ValueError("correction boundary does not match the current subject")
            latest = self.current_admission(corpus, identity.subject_key)
            if latest is None or latest.admission_id != correction_of_id:
                raise ValueError("correction boundary is not the current admission")
        identity_payload = identity.as_payload()
        with self._connection:
            self._connection.execute(
                """INSERT INTO semantic_content_admission_events(
                entry_key,corpus_key,subject_key,policy_version,policy_signature,
                physical_identity_json,virtual_identity_json,content_identity_json,
                work_identity_json,eligible,visible,reason_code,correction_of_id,
                diagnostics_json,recorded_ns,source_kind,source_identity)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    entry_key,
                    corpus_key,
                    identity.subject_key,
                    selected_policy.version,
                    selected_policy.signature,
                    None if identity_payload["physical"] is None else canonical_json(identity_payload["physical"]),
                    None if identity_payload["virtual"] is None else canonical_json(identity_payload["virtual"]),
                    canonical_json(identity_payload["content"]),
                    None if identity_payload["work"] is None else canonical_json(identity_payload["work"]),
                    int(selected_decision.eligible),
                    int(selected_decision.visible),
                    selected_decision.reason_code,
                    correction_of_id,
                    canonical_json(selected_diagnostics),
                    selected_ns,
                    identity.source_kind,
                    identity.source_identity,
                ),
            )
        row = self._connection.execute(
            f"SELECT {','.join(_EVENT_COLUMNS)} FROM semantic_content_admission_events WHERE entry_key=?",
            (entry_key,),
        ).fetchone()
        if row is None:  # pragma: no cover - guarded by the insert above
            raise RuntimeError("admission event disappeared after insert")
        return _record_from_row(row)

    admit = record_admission

    def current_admission(
        self,
        corpus: object,
        subject_key: str,
    ) -> StoredContentAdmission | None:
        corpus_key = _semantic_api().corpus_key_for(corpus)
        if not isinstance(subject_key, str) or not subject_key.strip():
            raise ValueError("subject_key cannot be blank")
        row = self._connection.execute(
            f"""SELECT {','.join(_EVENT_COLUMNS)} FROM semantic_content_admission_events
            WHERE corpus_key=? AND subject_key=?
            ORDER BY recorded_ns DESC,admission_id DESC LIMIT 1""",
            (corpus_key, subject_key),
        ).fetchone()
        return None if row is None else _record_from_row(row)

    def iter_current(self, corpus: object) -> Iterable[StoredContentAdmission]:
        corpus_key = _semantic_api().corpus_key_for(corpus)
        rows = self._connection.execute(
            f"""SELECT {','.join('event.' + column for column in _EVENT_COLUMNS)}
            FROM semantic_content_admission_events event
            WHERE event.corpus_key=? AND NOT EXISTS(
                SELECT 1 FROM semantic_content_admission_events later
                WHERE later.corpus_key=event.corpus_key
                  AND later.subject_key=event.subject_key
                  AND (later.recorded_ns>event.recorded_ns OR
                       (later.recorded_ns=event.recorded_ns AND
                        later.admission_id>event.admission_id))
            ) ORDER BY event.subject_key""",
            (corpus_key,),
        )
        for row in rows:
            yield _record_from_row(row)

    def current_excluded_subject_keys(self, corpus: object) -> frozenset[str]:
        return frozenset(record.subject_key for record in self.iter_current(corpus) if not record.visible)


# Friendly aliases for root wiring and older naming experiments.
FrameworkContentAdmissionLedger = ContentAdmissionLedger
SemanticContentAdmissionLedger = ContentAdmissionLedger
StoredAdmission = StoredContentAdmission
StoredPolicy = StoredContentAdmissionPolicy


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


__all__ = (
    "CONTENT_ADMISSION_EVENTS_CURRENT_INDEX",
    "CONTENT_ADMISSION_EVENTS_TABLE",
    "CONTENT_ADMISSION_EXTENSION_OBJECTS",
    "CONTENT_ADMISSION_EXTENSION_SCHEMA_VERSION",
    "CONTENT_ADMISSION_POLICIES_TABLE",
    "ContentAdmissionLedger",
    "FrameworkContentAdmissionLedger",
    "SemanticContentAdmissionLedger",
    "StoredAdmission",
    "StoredContentAdmission",
    "StoredContentAdmissionPolicy",
    "StoredPolicy",
    "content_admission_extension_present",
    "content_admission_extension_schema_contract",
    "create_content_admission_extension",
    "validate_content_admission_extension",
)
