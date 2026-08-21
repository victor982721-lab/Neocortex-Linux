"""Persistent inventory schema API."""

from .connections import (
    configure_inventory_connection as configure_inventory_connection,
    connect_existing_inventory_database as connect_existing_inventory_database,
)
from .contracts import inventory_schema_contract as inventory_schema_contract
from .ddl import SCHEMA_VERSION as SCHEMA_VERSION
from .lifecycle import initialize_inventory_schema as initialize_inventory_schema
from .validation import (
    validate_inventory_schema as validate_inventory_schema,
)

__all__ = [
    "SCHEMA_VERSION",
    "configure_inventory_connection",
    "connect_existing_inventory_database",
    "initialize_inventory_schema",
    "inventory_schema_contract",
    "validate_inventory_schema",
]
