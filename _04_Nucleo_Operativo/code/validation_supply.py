"""Dependency-equivalent pip-audit replay for canonical Code validation."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from ..code_schema import readonly_code_database, validate_code_schema
from ..external_evidence_providers import (
    INSTALLED_PACKAGE_PROVIDER_ID,
    PIP_AUDIT_PROVIDER_ID,
)


PIP_AUDIT_DEPENDENCY_CONTRACT_FILES = (
    "constraints.txt",
    "constraints-linux-cp314.lock",
    "tools/quality_gate_supply_policy.json",
)


class SupplyValidationError(ValueError):
    """The dependency contract or historical audit cannot be interpreted safely."""


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _string_sequence(value: object, *, label: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise SupplyValidationError(f"pip_audit_dependency_{label}_invalid")
    return cast(list[str], value)


def _optional_dependency_table(value: object) -> dict[str, list[str]]:
    if not isinstance(value, dict) or any(not isinstance(name, str) for name in value):
        raise SupplyValidationError("pip_audit_optional_dependencies_invalid")
    return {
        cast(str, name): _string_sequence(items, label="optional_dependency_items")
        for name, items in value.items()
    }


def _optional_string(value: object, *, label: str) -> str | None:
    if value is not None and not isinstance(value, str):
        raise SupplyValidationError(f"pip_audit_dependency_{label}_invalid")
    return value


def _pyproject_dependency_projection(raw: str) -> dict[str, object]:
    try:
        payload = tomllib.loads(raw)
    except tomllib.TOMLDecodeError as error:
        raise SupplyValidationError("pip_audit_dependency_pyproject_invalid") from error
    build = payload.get("build-system")
    project = payload.get("project")
    if not isinstance(build, dict) or not isinstance(project, dict):
        raise SupplyValidationError("pip_audit_dependency_pyproject_sections_missing")
    requires = _string_sequence(build.get("requires"), label="build_requires")
    dependencies = _string_sequence(project.get("dependencies", []), label="dependencies")
    optional = _optional_dependency_table(project.get("optional-dependencies", {}))
    dynamic = _string_sequence(project.get("dynamic", []), label="dynamic")
    backend_path = _string_sequence(build.get("backend-path", []), label="backend_path")
    return {
        "build_backend": _optional_string(build.get("build-backend"), label="build_backend"),
        "build_requires": requires,
        "backend_path": backend_path,
        "requires_python": _optional_string(
            project.get("requires-python"), label="requires_python"
        ),
        "dependencies": dependencies,
        "optional_dependencies": optional,
        "dynamic_dependencies": sorted(
            item for item in dynamic if item in {"dependencies", "optional-dependencies"}
        ),
    }


def dependency_contract_payload(files: Mapping[str, str]) -> dict[str, object]:
    required = {*PIP_AUDIT_DEPENDENCY_CONTRACT_FILES, "pyproject.toml"}
    if set(files) != required:
        raise SupplyValidationError("pip_audit_dependency_contract_files_incomplete")
    try:
        supply_policy = json.loads(files["tools/quality_gate_supply_policy.json"])
    except json.JSONDecodeError as error:
        raise SupplyValidationError("pip_audit_supply_policy_invalid") from error
    if not isinstance(supply_policy, dict):
        raise SupplyValidationError("pip_audit_supply_policy_not_object")
    return {
        "schema": "neocortex.pip-audit-dependency-contract/v1",
        "pyproject": _pyproject_dependency_projection(files["pyproject.toml"]),
        "constraints_sha256": hashlib.sha256(files["constraints.txt"].encode()).hexdigest(),
        "runtime_lock_sha256": hashlib.sha256(
            files["constraints-linux-cp314.lock"].encode()
        ).hexdigest(),
        "supply_policy": supply_policy,
    }


def dependency_contract_comparison(
    current_files: Mapping[str, str],
    baseline_files: Mapping[str, str],
) -> dict[str, object]:
    current_signature = (
        "sha256:"
        + hashlib.sha256(
            _canonical_json(dependency_contract_payload(current_files)).encode()
        ).hexdigest()
    )
    baseline_signature = (
        "sha256:"
        + hashlib.sha256(
            _canonical_json(dependency_contract_payload(baseline_files)).encode()
        ).hexdigest()
    )
    return {
        "schema": "neocortex.pip-audit-dependency-comparison/v1",
        "current_signature": current_signature,
        "baseline_signature": baseline_signature,
        "semantics_identical": current_signature == baseline_signature,
    }


def effective_provider_projection_run(connection: Any, tool_run_id: int) -> int | None:
    row = connection.execute(
        """SELECT r.status,c.execution FROM external_tool_runs r
        JOIN external_run_contracts c USING(tool_run_id) WHERE r.tool_run_id=?""",
        (tool_run_id,),
    ).fetchone()
    if row is None:
        return None
    status = str(row["status"])
    execution = str(row["execution"])
    if status == "completed" and execution != "cache_replay":
        return tool_run_id
    if status != "skipped" or execution != "cache_replay":
        return None
    replay = connection.execute(
        "SELECT source_tool_run_id FROM external_run_replays WHERE tool_run_id=?",
        (tool_run_id,),
    ).fetchone()
    return None if replay is None else int(replay[0])


def _installed_version_row(row: Any) -> tuple[str, str]:
    if float(row["value"]) != 1.0:
        raise SupplyValidationError("installed distribution is not present")
    try:
        metadata = json.loads(str(row["metadata_json"]))
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise SupplyValidationError("installed distribution metadata is invalid") from error
    if not isinstance(metadata, dict):
        raise SupplyValidationError("installed distribution metadata is not an object")
    name = metadata.get("normalized_name")
    version = metadata.get("installed_version")
    if not isinstance(name, str) or not name or not isinstance(version, str) or not version:
        raise SupplyValidationError("installed distribution identity is invalid")
    if str(row["subject_key"]) != f"package:{name}":
        raise SupplyValidationError("installed distribution subject disagrees")
    return name, version


def installed_versions(connection: Any, tool_run_id: int) -> dict[str, str] | None:
    effective = effective_provider_projection_run(connection, tool_run_id)
    if effective is None:
        return None
    rows = connection.execute(
        """SELECT subject_key,value,metadata_json FROM external_metrics
        WHERE tool_run_id=? AND category='package_integrity'
        AND metric_name='distribution_present' ORDER BY subject_key LIMIT 2001""",
        (effective,),
    ).fetchall()
    if not rows or len(rows) > 2_000:
        return None
    versions: dict[str, str] = {}
    try:
        for row in rows:
            name, version = _installed_version_row(row)
            if name in versions:
                raise SupplyValidationError("installed distribution identity is duplicated")
            versions[name] = version
    except (TypeError, ValueError):
        return None
    return versions


def _installed_inventory_run(connection: Any, analysis_run_id: int | None) -> int | None:
    if analysis_run_id is None:
        row = connection.execute(
            """SELECT r.tool_run_id FROM external_tool_runs r
            JOIN external_run_contracts c USING(tool_run_id)
            WHERE c.provider_id=?
            ORDER BY r.analysis_run_id DESC,r.tool_run_id DESC LIMIT 1""",
            (INSTALLED_PACKAGE_PROVIDER_ID,),
        ).fetchone()
    else:
        row = connection.execute(
            """SELECT r.tool_run_id FROM external_tool_runs r
            JOIN external_run_contracts c USING(tool_run_id)
            WHERE r.analysis_run_id=? AND c.provider_id=?
            ORDER BY r.tool_run_id DESC LIMIT 1""",
            (analysis_run_id, INSTALLED_PACKAGE_PROVIDER_ID),
        ).fetchone()
    return None if row is None else int(row["tool_run_id"])


def _latest_fresh_pip_audit(connection: Any) -> Any | None:
    return connection.execute(
        """SELECT r.tool_run_id,r.analysis_run_id,c.result_digest,m.value AS fresh_until
        FROM external_tool_runs r JOIN external_run_contracts c USING(tool_run_id)
        JOIN external_metrics m ON m.tool_run_id=r.tool_run_id
        WHERE c.provider_id=? AND r.status='completed'
        AND c.coverage_complete=1 AND c.result_digest IS NOT NULL
        AND m.subject_kind='project'
        AND m.subject_key='project:installed-environment'
        AND m.category='known_vulnerability'
        AND m.metric_name='audit_fresh_until_unix_seconds'
        AND m.unit='unix_seconds' AND m.value>=?
        ORDER BY r.tool_run_id DESC LIMIT 1""",
        (PIP_AUDIT_PROVIDER_ID, time.time()),
    ).fetchone()


def _matching_installed_inventory_size(
    connection: Any,
    *,
    current_run: int,
    historical_analysis_run: int,
) -> int | None:
    historical_run = _installed_inventory_run(connection, historical_analysis_run)
    if historical_run is None:
        return None
    current_versions = installed_versions(connection, current_run)
    historical_versions = installed_versions(connection, historical_run)
    if current_versions is None or current_versions != historical_versions:
        return None
    return len(current_versions)


def pip_audit_run_is_clean(connection: Any, tool_run_id: int) -> bool:
    counts = connection.execute(
        """SELECT
        SUM(CASE WHEN metric_name='known_vulnerability_count'
            AND subject_key='project:installed-environment' THEN value ELSE 0 END)
            AS vulnerabilities,
        SUM(CASE WHEN metric_name='audit_current_at_observation'
            AND subject_key='project:installed-environment' THEN value ELSE 0 END)
            AS current_markers
        FROM external_metrics WHERE tool_run_id=?""",
        (tool_run_id,),
    ).fetchone()
    finding_count = connection.execute(
        "SELECT COUNT(*) FROM external_findings WHERE tool_run_id=?",
        (tool_run_id,),
    ).fetchone()[0]
    return bool(
        counts is not None
        and float(counts["vulnerabilities"] or 0) == 0.0
        and float(counts["current_markers"] or 0) == 1.0
        and finding_count == 0
    )


def historical_pip_audit_fallback(
    state_directory: Path,
    *,
    analysis_run_id: int | None,
    supply_paths: tuple[str, ...],
    dependency_comparison: Mapping[str, object] | None,
) -> Mapping[str, object] | None:
    """Resolve a fresh audit when dependencies and installed versions are unchanged."""

    database = Path(state_directory) / "code.sqlite3"
    if not database.is_file():
        return None
    try:
        with readonly_code_database(database) as connection:
            validate_code_schema(connection)
            current_inventory = _installed_inventory_run(connection, analysis_run_id)
            audit = _latest_fresh_pip_audit(connection)
            if current_inventory is None or audit is None:
                return None
            inventory_size = _matching_installed_inventory_size(
                connection,
                current_run=current_inventory,
                historical_analysis_run=int(audit["analysis_run_id"]),
            )
            audit_run = int(audit["tool_run_id"])
            if inventory_size is None or not pip_audit_run_is_clean(connection, audit_run):
                return None
            return {
                "tool_run_id": audit_run,
                "analysis_run_id": int(audit["analysis_run_id"]),
                "result_digest": str(audit["result_digest"]),
                "fresh_until_unix_seconds": float(audit["fresh_until"]),
                "installed_distributions": inventory_size,
                "inventory_versions_identical": True,
                "known_vulnerabilities": 0,
                "supply_chain_paths": list(supply_paths),
                "dependency_contract": dependency_comparison,
            }
    except (OSError, RuntimeError, sqlite3.DatabaseError, TypeError, ValueError):
        return None


__all__ = [
    "PIP_AUDIT_DEPENDENCY_CONTRACT_FILES",
    "SupplyValidationError",
    "dependency_contract_comparison",
    "dependency_contract_payload",
    "effective_provider_projection_run",
    "historical_pip_audit_fallback",
    "installed_versions",
    "pip_audit_run_is_clean",
]
