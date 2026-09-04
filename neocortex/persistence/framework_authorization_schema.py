"""Optional AuthorizationGrant extension for the Framework owner.

The extension is deliberately kept inside ``framework.sqlite3`` rather than
introducing another SQLite owner. It has its own schema version and is created
only by the explicit authorization operation; ordinary reads remain side-effect
free. Grants are immutable rows and physical attempts remain the
``file_actions``/verification responsibility of a later lifecycle slice.
"""

from __future__ import annotations

import sqlite3
from functools import lru_cache

from neocortex.persistence.sqlite_schema_contract import (
    SQLiteSchemaContract,
    SQLiteSchemaContractError,
    schema_contract_from_builder,
    validate_sqlite_schema_contract,
)


AUTHORIZATION_EXTENSION_SCHEMA_VERSION = 1
AUTHORIZATION_GRANTS_TABLE = "curation_authorization_grants"
AUTHORIZATION_GRANTS_NO_UPDATE_TRIGGER = "curation_authorization_grants_no_update"
AUTHORIZATION_GRANTS_NO_DELETE_TRIGGER = "curation_authorization_grants_no_delete"
AUTHORIZATION_EXTENSION_OBJECTS = frozenset(
    {
        AUTHORIZATION_GRANTS_TABLE,
        AUTHORIZATION_GRANTS_NO_UPDATE_TRIGGER,
        AUTHORIZATION_GRANTS_NO_DELETE_TRIGGER,
    }
)

AUTHORIZATION_GRANTS_TABLE_STATEMENT = """
CREATE TABLE IF NOT EXISTS curation_authorization_grants (
    grant_id TEXT PRIMARY KEY CHECK(length(grant_id) BETWEEN 1 AND 256),
    authorization_key TEXT NOT NULL UNIQUE CHECK(
        length(trim(authorization_key)) BETWEEN 1 AND 512
    ),
    scope TEXT NOT NULL CHECK(length(trim(scope)) BETWEEN 1 AND 128),
    task_type TEXT NOT NULL CHECK(length(trim(task_type)) BETWEEN 1 AND 128),
    selector_signature TEXT NOT NULL CHECK(
        length(trim(selector_signature)) BETWEEN 1 AND 512
    ),
    plan_digest TEXT NOT NULL CHECK(
        length(plan_digest)=71 AND substr(plan_digest,1,7)='sha256:' AND
        substr(plan_digest,8) NOT GLOB '*[^0-9a-f]*'
    ),
    snapshot_id TEXT NOT NULL CHECK(
        length(snapshot_id)=71 AND substr(snapshot_id,1,7)='sha256:' AND
        substr(snapshot_id,8) NOT GLOB '*[^0-9a-f]*'
    ),
    source_snapshot_fingerprint TEXT NOT NULL CHECK(
        length(source_snapshot_fingerprint)=102 AND
        substr(source_snapshot_fingerprint,1,38)=
            'review-task-source-snapshot-v1:sha256:' AND
        substr(source_snapshot_fingerprint,39) NOT GLOB '*[^0-9a-f]*'
    ),
    root TEXT NOT NULL CHECK(length(trim(root)) BETWEEN 1 AND 4096),
    actor TEXT NOT NULL CHECK(length(trim(actor)) BETWEEN 1 AND 256),
    action TEXT NOT NULL CHECK(action IN ('trash','move','rename')),
    backend TEXT NOT NULL CHECK(backend='linux'),
    item_ids_json TEXT NOT NULL CHECK(
        json_valid(item_ids_json) AND json_type(item_ids_json)='array' AND
        length(CAST(item_ids_json AS BLOB)) BETWEEN 2 AND 65536
    ),
    task_ids_json TEXT NOT NULL CHECK(
        json_valid(task_ids_json) AND json_type(task_ids_json)='array' AND
        length(CAST(task_ids_json AS BLOB)) BETWEEN 2 AND 65536
    ),
    max_actions INTEGER NOT NULL CHECK(max_actions BETWEEN 1 AND 100),
    max_bytes INTEGER NOT NULL CHECK(max_bytes>=0),
    issued_ns INTEGER NOT NULL CHECK(issued_ns>0),
    expires_ns INTEGER NOT NULL CHECK(expires_ns>issued_ns),
    receipt_json TEXT NOT NULL CHECK(
        json_valid(receipt_json) AND json_type(receipt_json)='object' AND
        length(CAST(receipt_json AS BLOB)) BETWEEN 1 AND 65536
    ),
    extension_schema_version INTEGER NOT NULL CHECK(
        extension_schema_version=1
    )
) WITHOUT ROWID
"""

AUTHORIZATION_GRANTS_NO_UPDATE_TRIGGER_STATEMENT = """
CREATE TRIGGER IF NOT EXISTS curation_authorization_grants_no_update
BEFORE UPDATE ON curation_authorization_grants
BEGIN
    SELECT RAISE(ABORT, 'curation_authorization_grants is append-only');
END
"""

AUTHORIZATION_GRANTS_NO_DELETE_TRIGGER_STATEMENT = """
CREATE TRIGGER IF NOT EXISTS curation_authorization_grants_no_delete
BEFORE DELETE ON curation_authorization_grants
BEGIN
    SELECT RAISE(ABORT, 'curation_authorization_grants is append-only');
END
"""


def _build_authorization_extension(connection: sqlite3.Connection) -> None:
    connection.execute(AUTHORIZATION_GRANTS_TABLE_STATEMENT)
    connection.execute(AUTHORIZATION_GRANTS_NO_UPDATE_TRIGGER_STATEMENT)
    connection.execute(AUTHORIZATION_GRANTS_NO_DELETE_TRIGGER_STATEMENT)


@lru_cache(maxsize=1)
def authorization_extension_schema_contract() -> SQLiteSchemaContract:
    return schema_contract_from_builder(_build_authorization_extension)


def create_authorization_extension(connection: sqlite3.Connection) -> None:
    """Create the explicit extension objects inside an existing transaction."""

    _build_authorization_extension(connection)


def authorization_extension_present(connection: sqlite3.Connection) -> bool:
    row = connection.execute(
        "SELECT type FROM sqlite_master WHERE name=? LIMIT 1",
        (AUTHORIZATION_GRANTS_TABLE,),
    ).fetchone()
    return row is not None and str(row[0]) == "table"


def validate_authorization_extension(connection: sqlite3.Connection) -> None:
    """Validate the extension without creating, migrating, or repairing it."""

    if not authorization_extension_present(connection):
        return
    try:
        extra_tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
            if str(row[0]) != AUTHORIZATION_GRANTS_TABLE
        }
        extra_objects = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('view','trigger') AND name NOT LIKE 'sqlite_%'"
            )
            if str(row[0]) not in {
                AUTHORIZATION_GRANTS_NO_UPDATE_TRIGGER,
                AUTHORIZATION_GRANTS_NO_DELETE_TRIGGER,
            }
        }
        validate_sqlite_schema_contract(
            connection,
            authorization_extension_schema_contract(),
            label="Framework AuthorizationGrant extension",
            exact=True,
            allowed_extra_tables=extra_tables,
            allowed_extra_objects=extra_objects,
        )
    except SQLiteSchemaContractError as exc:
        raise RuntimeError(f"Framework AuthorizationGrant extension is invalid: {exc}") from exc


__all__ = (
    "AUTHORIZATION_EXTENSION_OBJECTS",
    "AUTHORIZATION_EXTENSION_SCHEMA_VERSION",
    "AUTHORIZATION_GRANTS_NO_DELETE_TRIGGER",
    "AUTHORIZATION_GRANTS_NO_UPDATE_TRIGGER",
    "AUTHORIZATION_GRANTS_TABLE",
    "authorization_extension_present",
    "authorization_extension_schema_contract",
    "create_authorization_extension",
    "validate_authorization_extension",
)
