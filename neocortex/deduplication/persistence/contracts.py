"""Cached structural contracts for inventory schema versions."""

from __future__ import annotations

from functools import lru_cache

from neocortex.persistence.sqlite_schema_contract import SQLiteSchemaContract, schema_contract_from_builder

from .ddl import (
    build_current_schema,
    build_metadata_schema,
    build_v6_schema,
    build_v7_schema,
    build_v8_schema,
    build_v9_schema,
    build_v10_schema,
)


@lru_cache(maxsize=1)
def metadata_contract() -> SQLiteSchemaContract:
    return schema_contract_from_builder(build_metadata_schema)


@lru_cache(maxsize=1)
def inventory_schema_contract() -> SQLiteSchemaContract:
    """Return the exact structural contract for inventory schema v11."""

    return schema_contract_from_builder(build_current_schema)


@lru_cache(maxsize=1)
def inventory_v6_schema_contract() -> SQLiteSchemaContract:
    return schema_contract_from_builder(build_v6_schema)


@lru_cache(maxsize=1)
def inventory_v7_schema_contract() -> SQLiteSchemaContract:
    return schema_contract_from_builder(build_v7_schema)


@lru_cache(maxsize=1)
def inventory_v8_schema_contract() -> SQLiteSchemaContract:
    return schema_contract_from_builder(build_v8_schema)


@lru_cache(maxsize=1)
def inventory_v9_schema_contract() -> SQLiteSchemaContract:
    return schema_contract_from_builder(build_v9_schema)


@lru_cache(maxsize=1)
def inventory_v10_schema_contract() -> SQLiteSchemaContract:
    return schema_contract_from_builder(build_v10_schema)
