# region [00] Contexto del módulo
# Módulo: tests/test_sqlite_facades.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]
# region [01] Dependencias del módulo
from __future__ import annotations

from neocortex.persistence import sqlite_paths
from neocortex import sqlite_schema_contract as operational_contract
from neocortex import sqlite_schema_lifecycle as operational_lifecycle
from neocortex import sqlite_schema_contract as shared_contract
from neocortex import sqlite_schema_lifecycle as shared_lifecycle
# endregion [01]

# region [02] Implementación


def test_sqlite_contract_identity_is_single_and_canonical() -> None:
    assert (
        operational_contract.SQLiteSchemaContract
        is shared_contract.SQLiteSchemaContract
    )
    assert (
        operational_contract.SQLiteSchemaContractError
        is shared_contract.SQLiteSchemaContractError
    )
    assert (
        operational_contract.capture_sqlite_schema_contract
        is shared_contract.capture_sqlite_schema_contract
    )
    assert (
        operational_contract.validate_sqlite_schema_contract
        is shared_contract.validate_sqlite_schema_contract
    )


def test_sqlite_lifecycle_and_uri_boundaries_are_separate() -> None:
    assert (
        operational_lifecycle.initialize_versioned_sqlite_schema
        is shared_lifecycle.initialize_versioned_sqlite_schema
    )
    assert sqlite_paths.readonly_sqlite_uri.__module__ == "neocortex.persistence.sqlite_paths"
    assert sqlite_paths.existing_sqlite_uri.__module__ == "neocortex.persistence.sqlite_paths"
    assert not hasattr(operational_lifecycle, "readonly_sqlite_uri")
    assert not hasattr(operational_lifecycle, "existing_sqlite_uri")
# endregion [02]
