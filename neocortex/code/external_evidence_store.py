"""Normalized persistence and read models for external code providers."""

from __future__ import annotations

from neocortex.platform import preserve_legacy_module as _preserve_legacy_module

import json
import sqlite3
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, cast

from .code_external_evidence import (
    ExternalEvidenceStatus,
    read_external_evidence,
)
from .external_evidence_models import (
    AnalysisProfile,
    ExternalEvidenceProvider,
    ExternalEvidenceSuiteStatus,
    ExternalProviderAttestation,
    ExternalProviderBaseline,
    ExternalProviderEvidence,
    ExternalProviderFinding,
    ExternalProviderMetric,
    ExternalProviderPublication,
    ExternalProviderRelation,
    ExternalProviderStatus,
    ExternalRunInput,
    ExternalSubjectKind,
    ProviderGateEvaluation,
    TypeConsensusSummary,
    external_finding_identity,
    external_provider_result_digest,
    external_signature,
    normalize_external_finding_message,
)
from neocortex.semantic.semantic_models import canonical_json, fingerprint_chunks

_PROVIDER_STATUS_LIMIT = 32
_FINDING_LIMIT = 10_000
_METRIC_LIMIT = 100_000
_RELATION_LIMIT = 250_000
_COUNTER_LIMIT = 128
_ProviderStatusGate = Literal["passed", "failed", "baseline", "not_evaluated"]
_SuiteStatus = Literal["ready", "partial", "abstained", "not_recorded"]
_RuntimeGroupKey = tuple[str, str, str | None, str | None]
_ProviderStatusProjection = tuple[
    ExternalProviderStatus,
    tuple[ExternalProviderFinding, ...],
    int | None,
    tuple[ExternalProviderMetric, ...],
    tuple[ExternalProviderRelation, ...],
]


@dataclass(frozen=True, slots=True)
class _ProviderReadContext:
    tool_run_id: int
    limitations: tuple[str, ...]
    inputs: tuple[sqlite3.Row, ...]
    counters: Mapping[str, int]
    eligible: int
    covered: int


@dataclass(frozen=True, slots=True)
class _ProviderEvidenceProjection:
    effective_run_id: int
    findings: tuple[ExternalProviderFinding, ...]
    metrics: tuple[ExternalProviderMetric, ...]
    relations: tuple[ExternalProviderRelation, ...]
    digest: str


def _portable_provider_findings(
    connection: sqlite3.Connection,
    tool_run_id: int,
    provider_id: str,
) -> tuple[ExternalProviderFinding, ...]:
    findings = _normalized_provider_findings(connection, tool_run_id)
    if provider_id != "pyright-trusted-project":
        return findings
    normalized = (
        replace(
            item,
            portable_finding_id=external_finding_identity(
                provider_id,
                relative_path=item.relative_path,
                category=item.category,
                code=item.code,
                message=item.message,
                start_line=item.start_line,
                start_column=item.start_column,
                end_line=item.end_line,
                end_column=item.end_column,
            ),
            message=normalize_external_finding_message(provider_id, item.message),
        )
        for item in findings
    )
    return tuple(sorted(normalized, key=lambda item: item.portable_finding_id))


def _portable_finding_ids(
    connection: sqlite3.Connection,
    tool_run_id: int,
    provider_id: str,
) -> tuple[str, ...]:
    if provider_id != "pyright-trusted-project":
        rows = connection.execute(
            """SELECT portable_finding_id FROM external_findings
            WHERE tool_run_id=? ORDER BY portable_finding_id LIMIT ?""",
            (tool_run_id, _FINDING_LIMIT + 1),
        ).fetchall()
        if len(rows) > _FINDING_LIMIT:
            raise ValueError("external provider identities exceed their bound")
        return tuple(str(item[0]) for item in rows)
    return tuple(
        item.portable_finding_id
        for item in _portable_provider_findings(connection, tool_run_id, provider_id)
    )


def _lastrowid(cursor: sqlite3.Cursor) -> int:
    value = cursor.lastrowid
    if value is None:
        raise RuntimeError("external evidence insert returned no row identity")
    return int(value)


def _current_version_exists(connection: sqlite3.Connection, version_id: int) -> bool:
    row = connection.execute(
        """SELECT 1 FROM files f JOIN file_versions v
        ON v.version_id=f.current_version_id
        WHERE v.version_id=? AND f.status='current' AND v.invalidated_ns IS NULL""",
        (version_id,),
    ).fetchone()
    return row is not None


def _delete_provider_projection(
    connection: sqlite3.Connection,
    *,
    source: str,
) -> None:
    connection.execute(
        """DELETE FROM diagnostics WHERE source=? AND version_id IN(
        SELECT v.version_id FROM file_versions v
        JOIN files f ON f.current_version_id=v.version_id
        WHERE f.status='current' AND v.invalidated_ns IS NULL)""",
        (source,),
    )


def _insert_finding_projection(
    connection: sqlite3.Connection,
    *,
    tool_run_id: int,
    publication: ExternalProviderPublication,
    finding: ExternalProviderFinding,
) -> int:
    descriptor = publication.descriptor
    metadata = {
        "schema": "neocortex.external-diagnostic/v2",
        "external_tool_run_id": tool_run_id,
        "external_provider_id": descriptor.provider_id,
        "external_finding_id": finding.portable_finding_id,
        "relative_path": finding.relative_path,
        "category": finding.category,
        "claim_scope": "tool_reported",
        "observation_confirmed": finding.observation_confirmed,
        "tool_confidence": finding.tool_confidence,
        "calibrated_confidence": finding.calibrated_confidence,
        "gate_authority": finding.gate_authority,
        "authority": descriptor.authority,
        "mutation_authority": False,
        "fix_available": finding.fix_available,
        "url": finding.url,
        "details": dict(finding.metadata),
    }
    cursor = connection.execute(
        """INSERT INTO diagnostics(
        version_id,source,code,severity,message,tool_name,tool_version,
        confirmed,confidence,start_line,start_column,end_line,end_column,
        start_byte,end_byte,metadata_json)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            finding.version_id,
            descriptor.source,
            finding.code,
            finding.severity,
            finding.message,
            descriptor.tool_name,
            publication.publication.tool_version,
            int(finding.observation_confirmed),
            1.0 if finding.tool_confidence is None else finding.tool_confidence,
            finding.start_line,
            finding.start_column,
            finding.end_line,
            finding.end_column,
            None,
            None,
            canonical_json(metadata),
        ),
    )
    return _lastrowid(cursor)


def _normalized_finding_from_row(row: sqlite3.Row) -> ExternalProviderFinding:
    try:
        metadata = json.loads(str(row["metadata_json"]))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("external finding metadata is malformed") from exc
    if (
        not isinstance(metadata, dict)
        or row["relative_path"] is None
        or row["version_id"] is None
        or bool(row["mutation_authority"])
    ):
        raise ValueError("external finding owner projection is incomplete")
    details = metadata.get("details")
    if not isinstance(details, dict):
        raise ValueError("external finding details are malformed")
    return ExternalProviderFinding(
        str(row["portable_finding_id"]),
        int(row["version_id"]),
        str(row["relative_path"]),
        str(row["category"]),
        str(row["code"]),
        str(row["severity"]),
        str(row["message"]),
        bool(row["observation_confirmed"]),
        None if row["tool_confidence"] is None else float(row["tool_confidence"]),
        (None if row["calibrated_confidence"] is None else float(row["calibrated_confidence"])),
        str(row["gate_authority"]),
        int(row["start_line"]),
        int(row["start_column"]),
        int(row["end_line"]),
        int(row["end_column"]),
        metadata.get("url") if isinstance(metadata.get("url"), str) else None,
        bool(metadata.get("fix_available", False)),
        details,
    )


def _normalized_provider_findings(
    connection: sqlite3.Connection,
    tool_run_id: int,
) -> tuple[ExternalProviderFinding, ...]:
    rows = connection.execute(
        """SELECT f.*,i.relative_path FROM external_findings f
        LEFT JOIN external_run_inputs i ON i.tool_run_id=f.tool_run_id
        AND i.version_id=f.version_id
        WHERE f.tool_run_id=? ORDER BY f.portable_finding_id LIMIT ?""",
        (tool_run_id, _FINDING_LIMIT + 1),
    ).fetchall()
    if len(rows) > _FINDING_LIMIT:
        raise ValueError("external provider findings exceed their read bound")
    return tuple(_normalized_finding_from_row(row) for row in rows)


def _rematerialize_replay_projection(
    connection: sqlite3.Connection,
    publication: ExternalProviderPublication,
) -> None:
    source_tool_run_id = publication.replay_source_tool_run_id
    if source_tool_run_id is None:
        raise ValueError("external provider replay source is missing")
    descriptor = publication.descriptor
    source = connection.execute(
        """SELECT r.status,r.tool_name,r.tool_version,r.configuration_signature,
        c.provider_id,c.provider_schema,c.source,c.profile,c.root_identity,
        c.project_configuration_digest,c.environment_signature,c.input_signature,
        c.comparability_signature,c.execution,c.result_digest,c.coverage_complete
        FROM external_tool_runs r JOIN external_run_contracts c
        ON c.tool_run_id=r.tool_run_id JOIN analysis_runs a
        ON a.analysis_run_id=r.analysis_run_id
        WHERE r.tool_run_id=? AND a.status='completed'""",
        (source_tool_run_id,),
    ).fetchone()
    expected_source = (
        "completed",
        descriptor.tool_name,
        publication.publication.tool_version,
        descriptor.configuration_signature,
        descriptor.provider_id,
        descriptor.provider_schema,
        descriptor.source,
        descriptor.profile,
        publication.root_identity,
        descriptor.project_configuration_digest,
        descriptor.environment_signature,
        publication.input_signature,
        descriptor.comparability_signature,
        "full",
        publication.result_digest,
        1,
    )
    if source is None or tuple(source) != expected_source:
        raise ValueError("external provider replay source is incompatible")

    findings = _normalized_provider_findings(connection, source_tool_run_id)
    metrics = _provider_metrics(connection, source_tool_run_id)
    relations = _provider_relations(connection, source_tool_run_id)
    if publication.result_digest != external_provider_result_digest(
        findings,
        metrics,
        relations,
    ):
        raise ValueError("external provider replay source digest is inconsistent")
    for counter, evidence in (
        ("findings", findings),
        ("metrics", metrics),
        ("relations", relations),
    ):
        if publication.counters.get(counter, len(evidence)) != len(evidence):
            raise ValueError(f"external provider replay {counter} counter is inconsistent")
    for finding in findings:
        if not _current_version_exists(connection, finding.version_id):
            raise RuntimeError("external replay finding version is no longer current")
    for metric in metrics:
        if metric.version_id is not None and not _current_version_exists(
            connection, metric.version_id
        ):
            raise RuntimeError("external replay metric version is no longer current")
    for relation in relations:
        for version_id in (relation.source_version_id, relation.target_version_id):
            if version_id is not None and not _current_version_exists(connection, version_id):
                raise RuntimeError("external replay relation version is no longer current")
    if not _provider_projection_versions_are_current(connection, source_tool_run_id):
        raise RuntimeError("external replay source projection is no longer current")

    _delete_provider_projection(connection, source=descriptor.source)
    for finding in findings:
        diagnostic_id = _insert_finding_projection(
            connection,
            tool_run_id=source_tool_run_id,
            publication=publication,
            finding=finding,
        )
        connection.execute(
            """UPDATE external_findings SET projected_diagnostic_id=?
            WHERE tool_run_id=? AND portable_finding_id=?""",
            (diagnostic_id, source_tool_run_id, finding.portable_finding_id),
        )
    if _provider_findings(connection, source_tool_run_id) != findings:
        raise ValueError("external provider replay projection verification failed")


def _publish_provider_run_and_contract(
    connection: sqlite3.Connection,
    analysis_run_id: int,
    publication: ExternalProviderPublication,
) -> int:
    """Create the provider run and its immutable publication contract."""
    owner = connection.execute(
        "SELECT status FROM analysis_runs WHERE analysis_run_id=?",
        (analysis_run_id,),
    ).fetchone()
    if owner is None or str(owner["status"]) != "running":
        raise RuntimeError("external provider requires one running Code owner")
    descriptor = publication.descriptor
    cursor = connection.execute(
        """INSERT INTO external_tool_runs(
        analysis_run_id,project_id,tool_name,tool_version,
        configuration_signature,status,started_ns,completed_ns,provenance_json)
        VALUES(?,NULL,?,?,?,?,?,?,?)""",
        (
            analysis_run_id,
            descriptor.tool_name,
            publication.publication.tool_version,
            descriptor.configuration_signature,
            publication.publication.status,
            publication.publication.started_ns,
            publication.publication.completed_ns,
            canonical_json(publication.publication.provenance),
        ),
    )
    tool_run_id = _lastrowid(cursor)
    connection.execute(
        """INSERT INTO external_run_contracts(
        tool_run_id,provider_id,provider_schema,source,profile,trust_requirement,scope,
        observed_root,root_identity,project_configuration_digest,
        environment_signature,input_signature,comparability_signature,
        execution_strategy,invalidation_strategy,cache_policy,execution,
        result_digest,portable_publication_id,authority,mutation_authority,
        loads_project_configuration,loads_plugins,imports_content,
        executes_content,uses_network,coverage_complete,limitations_json)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            tool_run_id,
            descriptor.provider_id,
            descriptor.provider_schema,
            descriptor.source,
            descriptor.profile,
            descriptor.trust_requirement,
            descriptor.scope,
            publication.observed_root,
            publication.root_identity,
            descriptor.project_configuration_digest,
            descriptor.environment_signature,
            publication.input_signature,
            descriptor.comparability_signature,
            descriptor.execution_strategy,
            descriptor.invalidation_strategy,
            descriptor.cache_policy,
            publication.execution,
            publication.result_digest,
            publication.portable_publication_id,
            descriptor.authority,
            0,
            int(descriptor.loads_project_configuration),
            int(descriptor.loads_plugins),
            int(descriptor.imports_content),
            int(descriptor.executes_content),
            int(descriptor.uses_network),
            int(publication.coverage_complete),
            json.dumps(
                list(publication.limitations),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        ),
    )
    return tool_run_id


def _publish_provider_inputs_and_counters(
    connection: sqlite3.Connection,
    tool_run_id: int,
    publication: ExternalProviderPublication,
) -> None:
    """Persist bounded inputs and counters for one provider run."""
    if len(publication.inputs) > 2_000:
        raise ValueError("external provider input normalization exceeds its bound")
    for item in publication.inputs:
        if not _current_version_exists(connection, item.version_id):
            raise RuntimeError("external provider input version is no longer current")
        connection.execute(
            """INSERT INTO external_run_inputs(
            tool_run_id,version_id,portable_input_id,relative_path,eligible,covered,
            coverage_reason,size,content_digest) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                tool_run_id,
                item.version_id,
                item.portable_input_id,
                item.relative_path,
                int(item.eligible),
                int(item.covered),
                item.coverage_reason,
                item.size,
                item.content_digest,
            ),
        )
    if len(publication.counters) > _COUNTER_LIMIT:
        raise ValueError("external provider counter normalization exceeds its bound")
    for name, value in sorted(publication.counters.items()):
        if not name or not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError("external provider counter is invalid")
        connection.execute(
            "INSERT INTO external_run_counters(tool_run_id,name,value) VALUES(?,?,?)",
            (tool_run_id, name, value),
        )


def _publish_provider_replay(
    connection: sqlite3.Connection,
    tool_run_id: int,
    publication: ExternalProviderPublication,
) -> bool:
    """Validate and rematerialize a replay publication when requested."""
    if publication.replay_source_tool_run_id is not None:
        if publication.execution != "cache_replay" or not publication.verification_signature:
            raise ValueError("external provider replay contract is incomplete")
        _rematerialize_replay_projection(connection, publication)
        connection.execute(
            """INSERT INTO external_run_replays(
            tool_run_id,source_tool_run_id,verification_signature,
            files_verified,bytes_verified) VALUES(?,?,?,?,?)""",
            (
                tool_run_id,
                publication.replay_source_tool_run_id,
                publication.verification_signature,
                int(publication.counters.get("files_verified", 0)),
                int(publication.counters.get("bytes_verified", 0)),
            ),
        )
        return True
    return False


def _publish_provider_findings(
    connection: sqlite3.Connection,
    tool_run_id: int,
    publication: ExternalProviderPublication,
) -> None:
    """Persist bounded findings and their diagnostic projections."""
    if len(publication.findings) > _FINDING_LIMIT:
        raise ValueError("external provider findings exceed their bound")
    for finding in publication.findings:
        if not _current_version_exists(connection, finding.version_id):
            raise RuntimeError("external finding version is no longer current")
        diagnostic_id = _insert_finding_projection(
            connection,
            tool_run_id=tool_run_id,
            publication=publication,
            finding=finding,
        )
        connection.execute(
            """INSERT INTO external_findings(
            tool_run_id,portable_finding_id,version_id,symbol_id,project_id,
            category,code,severity,message,observation_confirmed,tool_confidence,
            calibrated_confidence,gate_authority,mutation_authority,start_line,
            start_column,end_line,end_column,metadata_json,projected_diagnostic_id)
            VALUES(?,?,?,NULL,NULL,?,?,?,?,?,?,?,?,0,?,?,?,?,?,?)""",
            (
                tool_run_id,
                finding.portable_finding_id,
                finding.version_id,
                finding.category,
                finding.code,
                finding.severity,
                finding.message,
                int(finding.observation_confirmed),
                finding.tool_confidence,
                finding.calibrated_confidence,
                finding.gate_authority,
                finding.start_line,
                finding.start_column,
                finding.end_line,
                finding.end_column,
                canonical_json(
                    {
                        "url": finding.url,
                        "fix_available": finding.fix_available,
                        "details": dict(finding.metadata),
                    }
                ),
                diagnostic_id,
            ),
        )


def _publish_provider_metrics(
    connection: sqlite3.Connection,
    tool_run_id: int,
    publication: ExternalProviderPublication,
) -> None:
    """Persist bounded metrics with portable identity checks."""
    if len(publication.metrics) > _METRIC_LIMIT:
        raise ValueError("external provider metrics exceed their bound")
    if len({item.portable_metric_id for item in publication.metrics}) != len(publication.metrics):
        raise ValueError("external provider produced duplicate metric identities")
    for metric in publication.metrics:
        if metric.version_id is not None and not _current_version_exists(
            connection, metric.version_id
        ):
            raise RuntimeError("external metric version is no longer current")
        connection.execute(
            """INSERT INTO external_metrics(
            tool_run_id,portable_metric_id,subject_kind,subject_key,category,
            metric_name,value,unit,version_id,symbol_id,project_id,metadata_json)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                tool_run_id,
                metric.portable_metric_id,
                metric.subject_kind,
                metric.subject_key,
                metric.category,
                metric.metric_name,
                metric.value,
                metric.unit,
                metric.version_id,
                metric.symbol_id,
                metric.project_id,
                canonical_json(dict(metric.metadata)),
            ),
        )


def _publish_provider_relations(
    connection: sqlite3.Connection,
    tool_run_id: int,
    publication: ExternalProviderPublication,
) -> None:
    """Persist bounded relations with portable identity and version checks."""
    if len(publication.relations) > _RELATION_LIMIT:
        raise ValueError("external provider relations exceed their bound")
    if len({item.portable_relation_id for item in publication.relations}) != len(
        publication.relations
    ):
        raise ValueError("external provider produced duplicate relation identities")
    for relation in publication.relations:
        for version_id in (relation.source_version_id, relation.target_version_id):
            if version_id is not None and not _current_version_exists(connection, version_id):
                raise RuntimeError("external relation version is no longer current")
        connection.execute(
            """INSERT INTO external_relations(
            tool_run_id,portable_relation_id,relation_kind,source_kind,source_key,
            target_kind,target_key,directed,confidence,source_version_id,
            source_symbol_id,source_project_id,target_version_id,target_symbol_id,
            target_project_id,metadata_json)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                tool_run_id,
                relation.portable_relation_id,
                relation.relation_kind,
                relation.source_kind,
                relation.source_key,
                relation.target_kind,
                relation.target_key,
                int(relation.directed),
                relation.confidence,
                relation.source_version_id,
                relation.source_symbol_id,
                relation.source_project_id,
                relation.target_version_id,
                relation.target_symbol_id,
                relation.target_project_id,
                canonical_json(dict(relation.metadata)),
            ),
        )


def _publish_external_provider(
    connection: sqlite3.Connection,
    analysis_run_id: int,
    publication: ExternalProviderPublication,
) -> int:
    """Publish one provider beneath a running Code owner transaction."""
    descriptor = publication.descriptor
    tool_run_id = _publish_provider_run_and_contract(
        connection,
        analysis_run_id,
        publication,
    )
    _publish_provider_inputs_and_counters(connection, tool_run_id, publication)
    if _publish_provider_replay(connection, tool_run_id, publication):
        return tool_run_id
    _delete_provider_projection(connection, source=descriptor.source)
    if publication.publication.status != "completed":
        return tool_run_id
    if publication.result_digest != external_provider_result_digest(
        publication.findings,
        publication.metrics,
        publication.relations,
    ):
        raise ValueError("external provider result digest is inconsistent")
    _publish_provider_findings(connection, tool_run_id, publication)
    _publish_provider_metrics(connection, tool_run_id, publication)
    _publish_provider_relations(connection, tool_run_id, publication)
    return tool_run_id


def publish_external_provider(
    connection: sqlite3.Connection,
    analysis_run_id: int,
    publication: ExternalProviderPublication,
) -> int:
    """Atomically publish one provider beneath a running Code transaction."""

    connection.execute("SAVEPOINT external_provider_publication")
    try:
        tool_run_id = _publish_external_provider(connection, analysis_run_id, publication)
    except BaseException as error:
        # SQLITE_FULL, SQLITE_IOERR, SQLITE_INTERRUPT and similar failures may
        # roll back the whole transaction automatically.  Preserve that primary
        # cause instead of masking it with a secondary "no such savepoint".
        if connection.in_transaction:
            try:
                connection.execute("ROLLBACK TO external_provider_publication")
                connection.execute("RELEASE external_provider_publication")
            except sqlite3.Error as cleanup_error:
                error.add_note(
                    "external provider savepoint cleanup failed: "
                    f"{type(cleanup_error).__name__}: {cleanup_error}"
                )
        raise
    try:
        connection.execute("RELEASE external_provider_publication")
    except sqlite3.Error as error:
        raise RuntimeError(
            "external provider savepoint disappeared before release: "
            f"{publication.descriptor.provider_id}; "
            f"transaction_active={int(connection.in_transaction)}"
        ) from error
    return tool_run_id


def _provider_baseline_fresh_until(
    connection: sqlite3.Connection,
    tool_run_id: int,
    provider_id: str,
) -> float | None:
    """Return an explicitly published freshness fence for time-bound evidence."""

    if provider_id != "pip-audit-known-vulnerabilities":
        return None
    rows = connection.execute(
        """SELECT value FROM external_metrics
        WHERE tool_run_id=? AND subject_kind='project'
        AND subject_key='project:installed-environment'
        AND category='known_vulnerability'
        AND metric_name='audit_fresh_until_unix_seconds'
        AND unit='unix_seconds' ORDER BY portable_metric_id LIMIT 2""",
        (tool_run_id,),
    ).fetchall()
    if len(rows) != 1:
        return None
    value = float(rows[0][0])
    return value if value > 0 else None


def _provider_projection_versions_are_current(
    connection: sqlite3.Connection,
    tool_run_id: int,
) -> bool:
    """Return whether every version-bound input and fact still belongs to the live graph.

    Portable identities from an older publication remain useful as a comparable
    baseline after Code creates new file versions.  Its physical projection does
    not: replaying those rows would reuse superseded inputs or attach current
    diagnostics/metrics/relations to superseded versions.  Reject only exact
    replay here so providers can run again while still calculating
    added/resolved deltas from portable IDs.
    """

    stale = connection.execute(
        """SELECT 1 FROM (
        SELECT version_id FROM external_run_inputs WHERE tool_run_id=?
        UNION
        SELECT version_id FROM external_findings WHERE tool_run_id=?
        UNION
        SELECT version_id FROM external_metrics
        WHERE tool_run_id=? AND version_id IS NOT NULL
        UNION
        SELECT source_version_id FROM external_relations
        WHERE tool_run_id=? AND source_version_id IS NOT NULL
        UNION
        SELECT target_version_id FROM external_relations
        WHERE tool_run_id=? AND target_version_id IS NOT NULL
        ) refs
        WHERE NOT EXISTS(
            SELECT 1 FROM files f JOIN file_versions v
            ON v.version_id=f.current_version_id
            WHERE v.version_id=refs.version_id
            AND f.status='current' AND v.invalidated_ns IS NULL
        ) LIMIT 1""",
        (tool_run_id, tool_run_id, tool_run_id, tool_run_id, tool_run_id),
    ).fetchone()
    return stale is None


def read_external_provider_baselines(
    connection: sqlite3.Connection,
    *,
    provider_id: str,
    profile: str,
    tool_version: str,
    configuration_signature: str,
    environment_signature: str,
    root_identity: str,
    input_signature: str,
    comparability_signature: str,
) -> tuple[ExternalProviderBaseline | None, ExternalProviderBaseline | None]:
    rows = connection.execute(
        """SELECT r.tool_run_id,r.tool_version,c.provider_id,c.input_signature,
        c.comparability_signature,c.result_digest
        FROM external_tool_runs r JOIN external_run_contracts c
        ON c.tool_run_id=r.tool_run_id JOIN analysis_runs a
        ON a.analysis_run_id=r.analysis_run_id
        WHERE c.provider_id=? AND c.profile=? AND r.tool_version=?
        AND r.configuration_signature=? AND c.environment_signature=?
        AND c.root_identity=? AND r.status='completed'
        AND a.status='completed' AND c.coverage_complete=1
        ORDER BY r.tool_run_id DESC LIMIT 128""",
        (
            provider_id,
            profile,
            tool_version,
            configuration_signature,
            environment_signature,
            root_identity,
        ),
    ).fetchall()
    exact: ExternalProviderBaseline | None = None
    comparable: ExternalProviderBaseline | None = None
    for row in rows:
        digest = row["result_digest"]
        if not isinstance(digest, str):
            continue
        ids = _portable_finding_ids(
            connection,
            int(row["tool_run_id"]),
            str(row["provider_id"]),
        )
        metric_ids = tuple(
            str(item[0])
            for item in connection.execute(
                """SELECT portable_metric_id FROM external_metrics
                WHERE tool_run_id=? ORDER BY portable_metric_id""",
                (int(row["tool_run_id"]),),
            ).fetchall()
        )
        relation_ids = tuple(
            str(item[0])
            for item in connection.execute(
                """SELECT portable_relation_id FROM external_relations
                WHERE tool_run_id=? ORDER BY portable_relation_id""",
                (int(row["tool_run_id"]),),
            ).fetchall()
        )
        baseline = ExternalProviderBaseline(
            int(row["tool_run_id"]),
            str(row["provider_id"]),
            str(row["tool_version"]),
            str(row["input_signature"]),
            str(row["comparability_signature"]),
            digest,
            ids,
            metric_ids,
            relation_ids,
            _provider_baseline_fresh_until(
                connection,
                int(row["tool_run_id"]),
                str(row["provider_id"]),
            ),
        )
        if comparable is None and baseline.comparability_signature == comparability_signature:
            comparable = baseline
        if (
            baseline.comparability_signature == comparability_signature
            and baseline.input_signature == input_signature
            and _provider_projection_versions_are_current(
                connection,
                baseline.tool_run_id,
            )
        ):
            exact = replace(baseline, reuse_mode="exact_replay")
            break
    return exact, comparable


def _counter_map(connection: sqlite3.Connection, tool_run_id: int) -> dict[str, int]:
    rows = connection.execute(
        """SELECT name,value FROM external_run_counters
        WHERE tool_run_id=? ORDER BY name LIMIT ?""",
        (tool_run_id, _COUNTER_LIMIT + 1),
    ).fetchall()
    if len(rows) > _COUNTER_LIMIT:
        raise ValueError("external provider counters exceed their read bound")
    return {str(row["name"]): int(row["value"]) for row in rows}


def _provider_findings(
    connection: sqlite3.Connection,
    tool_run_id: int,
) -> tuple[ExternalProviderFinding, ...]:
    rows = connection.execute(
        """SELECT f.*,i.relative_path,d.metadata_json AS diagnostic_metadata,
        d.source AS diagnostic_source,d.tool_name AS diagnostic_tool_name,
        d.tool_version AS diagnostic_tool_version,d.code AS diagnostic_code,
        d.severity AS diagnostic_severity,d.message AS diagnostic_message,
        d.version_id AS diagnostic_version_id,d.start_line AS diagnostic_start_line,
        d.start_column AS diagnostic_start_column,d.end_line AS diagnostic_end_line,
        d.end_column AS diagnostic_end_column,c.provider_id AS expected_provider_id,
        c.source AS expected_source,
        r.tool_name AS expected_tool_name,r.tool_version AS expected_tool_version
        FROM external_findings f
        JOIN external_run_contracts c ON c.tool_run_id=f.tool_run_id
        JOIN external_tool_runs r ON r.tool_run_id=f.tool_run_id
        LEFT JOIN external_run_inputs i ON i.tool_run_id=f.tool_run_id
        AND i.version_id=f.version_id
        LEFT JOIN diagnostics d ON d.diagnostic_id=f.projected_diagnostic_id
        WHERE f.tool_run_id=? ORDER BY f.portable_finding_id LIMIT ?""",
        (tool_run_id, _FINDING_LIMIT + 1),
    ).fetchall()
    if len(rows) > _FINDING_LIMIT:
        raise ValueError("external provider findings exceed their read bound")
    return tuple(_provider_finding_from_projection_row(row) for row in rows)


def _provider_finding_from_projection_row(row: sqlite3.Row) -> ExternalProviderFinding:
    try:
        diagnostic_metadata = json.loads(str(row["diagnostic_metadata"]))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("external diagnostic projection metadata is malformed") from exc
    if not isinstance(diagnostic_metadata, dict):
        raise ValueError("external diagnostic projection metadata is not an object")
    expected_projection = (
        row["diagnostic_source"] == row["expected_source"]
        and row["diagnostic_tool_name"] == row["expected_tool_name"]
        and row["diagnostic_tool_version"] == row["expected_tool_version"]
        and row["diagnostic_code"] == row["code"]
        and row["diagnostic_severity"] == row["severity"]
        and row["diagnostic_message"] == row["message"]
        and row["diagnostic_version_id"] == row["version_id"]
        and row["diagnostic_start_line"] == row["start_line"]
        and row["diagnostic_start_column"] == row["start_column"]
        and row["diagnostic_end_line"] == row["end_line"]
        and row["diagnostic_end_column"] == row["end_column"]
        and diagnostic_metadata.get("external_provider_id") == row["expected_provider_id"]
        and diagnostic_metadata.get("external_finding_id") == row["portable_finding_id"]
        and diagnostic_metadata.get("mutation_authority") is False
    )
    if not expected_projection:
        raise ValueError("external diagnostic projection is inconsistent")
    return _normalized_finding_from_row(row)


def _metric_from_row(row: sqlite3.Row) -> ExternalProviderMetric:
    try:
        metadata = json.loads(str(row["metadata_json"]))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("external metric metadata is malformed") from exc
    if not isinstance(metadata, dict):
        raise ValueError("external metric metadata is not an object")
    return ExternalProviderMetric(
        str(row["portable_metric_id"]),
        cast(ExternalSubjectKind, str(row["subject_kind"])),
        str(row["subject_key"]),
        str(row["category"]),
        str(row["metric_name"]),
        float(row["value"]),
        str(row["unit"]),
        None if row["version_id"] is None else int(row["version_id"]),
        None if row["symbol_id"] is None else int(row["symbol_id"]),
        None if row["project_id"] is None else int(row["project_id"]),
        metadata,
    )


def _provider_metrics(
    connection: sqlite3.Connection,
    tool_run_id: int,
) -> tuple[ExternalProviderMetric, ...]:
    rows = connection.execute(
        """SELECT portable_metric_id,subject_kind,subject_key,category,
        metric_name,value,unit,version_id,symbol_id,project_id,metadata_json
        FROM external_metrics WHERE tool_run_id=?
        ORDER BY portable_metric_id LIMIT ?""",
        (tool_run_id, _METRIC_LIMIT + 1),
    ).fetchall()
    if len(rows) > _METRIC_LIMIT:
        raise ValueError("external provider metrics exceed their read bound")
    return tuple(_metric_from_row(row) for row in rows)


def _relation_from_row(row: sqlite3.Row) -> ExternalProviderRelation:
    try:
        metadata = json.loads(str(row["metadata_json"]))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("external relation metadata is malformed") from exc
    if not isinstance(metadata, dict):
        raise ValueError("external relation metadata is not an object")
    return ExternalProviderRelation(
        str(row["portable_relation_id"]),
        str(row["relation_kind"]),
        cast(ExternalSubjectKind, str(row["source_kind"])),
        str(row["source_key"]),
        cast(ExternalSubjectKind, str(row["target_kind"])),
        str(row["target_key"]),
        bool(row["directed"]),
        None if row["confidence"] is None else float(row["confidence"]),
        None if row["source_version_id"] is None else int(row["source_version_id"]),
        None if row["source_symbol_id"] is None else int(row["source_symbol_id"]),
        None if row["source_project_id"] is None else int(row["source_project_id"]),
        None if row["target_version_id"] is None else int(row["target_version_id"]),
        None if row["target_symbol_id"] is None else int(row["target_symbol_id"]),
        None if row["target_project_id"] is None else int(row["target_project_id"]),
        metadata,
    )


def _provider_relations(
    connection: sqlite3.Connection,
    tool_run_id: int,
) -> tuple[ExternalProviderRelation, ...]:
    rows = connection.execute(
        """SELECT portable_relation_id,relation_kind,source_kind,source_key,
        target_kind,target_key,directed,confidence,source_version_id,
        source_symbol_id,source_project_id,target_version_id,target_symbol_id,
        target_project_id,metadata_json FROM external_relations
        WHERE tool_run_id=? ORDER BY portable_relation_id LIMIT ?""",
        (tool_run_id, _RELATION_LIMIT + 1),
    ).fetchall()
    if len(rows) > _RELATION_LIMIT:
        raise ValueError("external provider relations exceed their read bound")
    return tuple(_relation_from_row(row) for row in rows)


def _provider_projection_counts(
    connection: sqlite3.Connection,
    tool_run_id: int,
) -> tuple[int, int, int]:
    row = connection.execute(
        """SELECT
        (SELECT COUNT(*) FROM external_findings WHERE tool_run_id=?) AS findings,
        (SELECT COUNT(*) FROM external_metrics WHERE tool_run_id=?) AS metrics,
        (SELECT COUNT(*) FROM external_relations WHERE tool_run_id=?) AS relations""",
        (tool_run_id, tool_run_id, tool_run_id),
    ).fetchone()
    if row is None:
        raise ValueError("external provider projection counts are unavailable")
    counts = (int(row["findings"]), int(row["metrics"]), int(row["relations"]))
    if counts[0] > _FINDING_LIMIT or counts[1] > _METRIC_LIMIT or counts[2] > _RELATION_LIMIT:
        raise ValueError("external provider projection exceeds its read bound")
    return counts


def _provider_finding_digest_payloads(
    connection: sqlite3.Connection,
    tool_run_id: int,
) -> Iterable[Mapping[str, object]]:
    rows = connection.execute(
        """SELECT f.*,i.relative_path,d.metadata_json AS diagnostic_metadata,
        d.source AS diagnostic_source,d.tool_name AS diagnostic_tool_name,
        d.tool_version AS diagnostic_tool_version,d.code AS diagnostic_code,
        d.severity AS diagnostic_severity,d.message AS diagnostic_message,
        d.version_id AS diagnostic_version_id,d.start_line AS diagnostic_start_line,
        d.start_column AS diagnostic_start_column,d.end_line AS diagnostic_end_line,
        d.end_column AS diagnostic_end_column,c.provider_id AS expected_provider_id,
        c.source AS expected_source,
        r.tool_name AS expected_tool_name,r.tool_version AS expected_tool_version
        FROM external_findings f
        JOIN external_run_contracts c ON c.tool_run_id=f.tool_run_id
        JOIN external_tool_runs r ON r.tool_run_id=f.tool_run_id
        LEFT JOIN external_run_inputs i ON i.tool_run_id=f.tool_run_id
        AND i.version_id=f.version_id
        LEFT JOIN diagnostics d ON d.diagnostic_id=f.projected_diagnostic_id
        WHERE f.tool_run_id=? ORDER BY f.portable_finding_id""",
        (tool_run_id,),
    )
    for row in rows:
        yield _provider_finding_from_projection_row(row).digest_payload()


def _provider_metric_digest_payloads(
    connection: sqlite3.Connection,
    tool_run_id: int,
) -> Iterable[Mapping[str, object]]:
    rows = connection.execute(
        """SELECT portable_metric_id,subject_kind,subject_key,category,
        metric_name,value,unit,version_id,symbol_id,project_id,metadata_json
        FROM external_metrics WHERE tool_run_id=? ORDER BY portable_metric_id""",
        (tool_run_id,),
    )
    for row in rows:
        yield _metric_from_row(row).digest_payload()


def _provider_relation_digest_payloads(
    connection: sqlite3.Connection,
    tool_run_id: int,
) -> Iterable[Mapping[str, object]]:
    rows = connection.execute(
        """SELECT portable_relation_id,relation_kind,source_kind,source_key,
        target_kind,target_key,directed,confidence,source_version_id,
        source_symbol_id,source_project_id,target_version_id,target_symbol_id,
        target_project_id,metadata_json FROM external_relations
        WHERE tool_run_id=? ORDER BY portable_relation_id""",
        (tool_run_id,),
    )
    for row in rows:
        yield _relation_from_row(row).digest_payload()


def _canonical_array_chunks(
    values: Iterable[Mapping[str, object]],
) -> Iterable[bytes]:
    first = True
    for value in values:
        if not first:
            yield b","
        yield canonical_json(value).encode("utf-8")
        first = False


def _streamed_provider_result_digest(
    connection: sqlite3.Connection,
    tool_run_id: int,
    *,
    counts: tuple[int, int, int],
) -> str:
    findings, metrics, relations = counts

    def chunks() -> Iterable[bytes]:
        yield b'{"findings":['
        yield from _canonical_array_chunks(
            _provider_finding_digest_payloads(connection, tool_run_id)
        )
        if metrics == 0 and relations == 0:
            yield b"]}"
            return
        yield b'],"metrics":['
        yield from _canonical_array_chunks(
            _provider_metric_digest_payloads(connection, tool_run_id)
        )
        yield b'],"relations":['
        yield from _canonical_array_chunks(
            _provider_relation_digest_payloads(connection, tool_run_id)
        )
        yield b"]}"

    digest = fingerprint_chunks(chunks()).xxh3_128
    prefix = (
        "external-findings-v1" if metrics == 0 and relations == 0 else "external-provider-result-v2"
    )
    if findings < 0:  # pragma: no cover - counts are constrained by SQLite
        raise AssertionError("external provider finding count cannot be negative")
    return f"{prefix}:xxh3_128:{digest}"


def _provider_inputs_are_current(
    connection: sqlite3.Connection,
    tool_run_id: int,
) -> bool:
    row = connection.execute(
        """SELECT COUNT(*) FROM external_run_inputs i
        WHERE i.tool_run_id=? AND NOT EXISTS(
            SELECT 1 FROM files f JOIN file_versions v
            ON v.version_id=f.current_version_id
            WHERE v.version_id=i.version_id AND f.status='current'
            AND v.invalidated_ns IS NULL
        )""",
        (tool_run_id,),
    ).fetchone()
    return row is not None and int(row[0]) == 0


def _abstained_provider(
    row: sqlite3.Row | Mapping[str, object],
    reason: str,
    *,
    eligible_files: int = 0,
    covered_files: int = 0,
    findings: int = 0,
    limitations: tuple[str, ...] = (),
    counters: Mapping[str, int] | None = None,
) -> ExternalProviderStatus:
    return ExternalProviderStatus(
        str(row["provider_id"]),
        str(row["provider_schema"]),
        cast(AnalysisProfile, str(row["profile"])),
        str(row["tool_name"]),
        str(row["tool_version"]),
        "abstained",
        reason,
        str(row["execution"]),
        eligible_files,
        covered_files,
        findings,
        None,
        None,
        False,
        None,
        str(row["comparability_signature"]),
        "not_evaluated",
        limitations,
        content_executed=bool(row["executes_content"]),
        counters={} if counters is None else counters,
    )


def _effective_provider_run_id(
    connection: sqlite3.Connection,
    row: sqlite3.Row | Mapping[str, object],
) -> int:
    tool_run_id = int(str(row["tool_run_id"]))
    owner = connection.execute(
        """SELECT a.status FROM external_tool_runs r
        JOIN analysis_runs a ON a.analysis_run_id=r.analysis_run_id
        WHERE r.tool_run_id=?""",
        (tool_run_id,),
    ).fetchone()
    if owner is None or str(owner["status"]) != "completed":
        raise ValueError("external_provider_owner_not_completed")
    if str(row["execution"]) != "cache_replay":
        return tool_run_id
    replay = connection.execute(
        """SELECT source_tool_run_id,files_verified,bytes_verified
        FROM external_run_replays WHERE tool_run_id=?""",
        (tool_run_id,),
    ).fetchone()
    if replay is None:
        raise ValueError("replay_missing")
    effective_run_id = int(replay["source_tool_run_id"])
    source = connection.execute(
        """SELECT r.status,c.provider_id,c.result_digest,c.input_signature,
        c.comparability_signature FROM external_tool_runs r
        JOIN external_run_contracts c ON c.tool_run_id=r.tool_run_id
        JOIN analysis_runs a ON a.analysis_run_id=r.analysis_run_id
        WHERE r.tool_run_id=? AND a.status='completed'""",
        (effective_run_id,),
    ).fetchone()
    if (
        source is None
        or str(source["status"]) != "completed"
        or str(source["provider_id"]) != str(row["provider_id"])
        or source["result_digest"] != row["result_digest"]
        or source["input_signature"] != row["input_signature"]
        or source["comparability_signature"] != row["comparability_signature"]
    ):
        raise ValueError("replay_source_invalid")
    return effective_run_id


def _provider_read_context(
    connection: sqlite3.Connection,
    row: sqlite3.Row | Mapping[str, object],
) -> _ProviderReadContext:
    tool_run_id = int(str(row["tool_run_id"]))
    limitations_value = json.loads(str(row["limitations_json"]))
    limitations = tuple(str(item) for item in limitations_value)
    inputs = tuple(
        connection.execute(
            """SELECT version_id,portable_input_id,relative_path,eligible,covered,
            coverage_reason,size,content_digest FROM external_run_inputs
            WHERE tool_run_id=? ORDER BY portable_input_id LIMIT 2001""",
            (tool_run_id,),
        ).fetchall()
    )
    if len(inputs) > 2_000:
        raise ValueError("input_bound")
    counters = _counter_map(connection, tool_run_id)
    eligible = sum(int(item["eligible"]) for item in inputs)
    covered = sum(int(item["covered"]) for item in inputs)
    if counters.get("eligible_files", eligible) != eligible:
        raise ValueError("eligible_counter")
    if counters.get("covered_files", covered) != covered:
        raise ValueError("covered_counter")
    return _ProviderReadContext(
        tool_run_id,
        limitations,
        inputs,
        counters,
        eligible,
        covered,
    )


def _terminal_provider_reason(
    row: sqlite3.Row | Mapping[str, object],
    tool_status: str,
) -> str:
    reason = "provider_abstained" if tool_status == "skipped" else f"provider_{tool_status}"
    if tool_status not in {"skipped", "failed"}:
        return reason
    try:
        provenance = json.loads(str(row["provenance_json"]))
        error = provenance.get("error") if isinstance(provenance, dict) else None
        detail = error.get("reason") if isinstance(error, dict) else None
    except (TypeError, ValueError, json.JSONDecodeError):
        return reason
    if isinstance(detail, str) and detail:
        return f"{reason}:{detail[:4096]}"
    return reason


def _terminal_provider_projection(
    row: sqlite3.Row | Mapping[str, object],
    context: _ProviderReadContext,
) -> _ProviderStatusProjection | None:
    tool_status = str(row["status"])
    if tool_status == "skipped" and str(row["execution"]) != "cache_replay":
        status = _abstained_provider(
            row,
            _terminal_provider_reason(row, tool_status),
            eligible_files=context.eligible,
            covered_files=context.covered,
            findings=context.counters.get("findings", 0),
            limitations=context.limitations,
            counters=context.counters,
        )
        return replace(status, content_executed=False), (), None, (), ()
    if tool_status in {"completed", "skipped"}:
        return None
    return (
        _abstained_provider(
            row,
            _terminal_provider_reason(row, tool_status),
            eligible_files=context.eligible,
            covered_files=context.covered,
            findings=context.counters.get("findings", 0),
            limitations=context.limitations,
            counters=context.counters,
        ),
        (),
        None,
        (),
        (),
    )


def _ready_provider_evidence(
    connection: sqlite3.Connection,
    row: sqlite3.Row | Mapping[str, object],
    context: _ProviderReadContext,
) -> _ProviderEvidenceProjection:
    effective_run_id = _effective_provider_run_id(connection, row)
    findings = _provider_findings(connection, effective_run_id)
    metrics = _provider_metrics(connection, effective_run_id)
    relations = _provider_relations(connection, effective_run_id)
    digest = external_provider_result_digest(findings, metrics, relations)
    if row["result_digest"] != digest:
        raise ValueError("result_digest")
    if context.counters.get("findings", len(findings)) != len(findings):
        raise ValueError("finding_counter")
    if context.counters.get("metrics", len(metrics)) != len(metrics):
        raise ValueError("metric_counter")
    if context.counters.get("relations", len(relations)) != len(relations):
        raise ValueError("relation_counter")
    for item in context.inputs:
        if not _current_version_exists(connection, int(item["version_id"])):
            raise ValueError("input_not_current")
    return _ProviderEvidenceProjection(
        effective_run_id,
        findings,
        metrics,
        relations,
        digest,
    )


def _provider_gate(comparable: bool, added: int | None) -> _ProviderStatusGate:
    if not comparable:
        return "baseline"
    return "passed" if added == 0 else "failed"


def _ready_provider_projection(
    row: sqlite3.Row | Mapping[str, object],
    context: _ProviderReadContext,
    evidence: _ProviderEvidenceProjection,
) -> _ProviderStatusProjection:
    comparable = context.counters.get("comparable", 0) == 1
    added = context.counters.get("added") if comparable else None
    resolved = context.counters.get("resolved") if comparable else None
    status = ExternalProviderStatus(
        str(row["provider_id"]),
        str(row["provider_schema"]),
        cast(AnalysisProfile, str(row["profile"])),
        str(row["tool_name"]),
        str(row["tool_version"]),
        "ready",
        None,
        str(row["execution"]),
        context.eligible,
        context.covered,
        len(evidence.findings),
        added,
        resolved,
        comparable,
        evidence.digest,
        str(row["comparability_signature"]),
        _provider_gate(comparable, added),
        context.limitations,
        content_executed=bool(row["executes_content"]),
        counters=context.counters,
        metrics=len(evidence.metrics),
        relations=len(evidence.relations),
    )
    return (
        status,
        evidence.findings,
        evidence.effective_run_id,
        evidence.metrics,
        evidence.relations,
    )


def _verified_streamed_provider_projection(
    connection: sqlite3.Connection,
    row: sqlite3.Row | Mapping[str, object],
    context: _ProviderReadContext,
) -> tuple[tuple[int, int, int], str]:
    effective_run_id = _effective_provider_run_id(connection, row)
    counts = _provider_projection_counts(connection, effective_run_id)
    for name, observed in zip(("findings", "metrics", "relations"), counts, strict=True):
        if context.counters.get(name, observed) != observed:
            raise ValueError(f"{name}_counter")
    digest = _streamed_provider_result_digest(
        connection,
        effective_run_id,
        counts=counts,
    )
    if row["result_digest"] != digest:
        raise ValueError("result_digest")
    if not _provider_inputs_are_current(connection, context.tool_run_id):
        raise ValueError("input_not_current")
    return counts, digest


def _ready_validation_provider_status(
    row: sqlite3.Row | Mapping[str, object],
    context: _ProviderReadContext,
    counts: tuple[int, int, int],
    digest: str,
) -> ExternalProviderStatus:
    findings, metrics, relations = counts
    comparable = context.counters.get("comparable", 0) == 1
    added = context.counters.get("added") if comparable else None
    resolved = context.counters.get("resolved") if comparable else None
    return ExternalProviderStatus(
        str(row["provider_id"]),
        str(row["provider_schema"]),
        cast(AnalysisProfile, str(row["profile"])),
        str(row["tool_name"]),
        str(row["tool_version"]),
        "ready",
        None,
        str(row["execution"]),
        context.eligible,
        context.covered,
        findings,
        added,
        resolved,
        comparable,
        digest,
        str(row["comparability_signature"]),
        _provider_gate(comparable, added),
        context.limitations,
        content_executed=bool(row["executes_content"]),
        counters=context.counters,
        metrics=metrics,
        relations=relations,
    )


def _validation_provider_status(
    connection: sqlite3.Connection,
    row: sqlite3.Row | Mapping[str, object],
) -> ExternalProviderStatus:
    """Verify one immutable provider without retaining its evidence graph."""

    try:
        tool_run_id = int(str(row["tool_run_id"]))
        owner = connection.execute(
            """SELECT a.status FROM external_tool_runs r
            JOIN analysis_runs a ON a.analysis_run_id=r.analysis_run_id
            WHERE r.tool_run_id=?""",
            (tool_run_id,),
        ).fetchone()
        if owner is None or str(owner["status"]) != "completed":
            return _abstained_provider(row, "external_provider_owner_not_completed")
        context = _provider_read_context(connection, row)
        terminal = _terminal_provider_projection(row, context)
        if terminal is not None:
            return terminal[0]
        counts, digest = _verified_streamed_provider_projection(connection, row, context)
        return _ready_validation_provider_status(row, context, counts, digest)
    except (KeyError, TypeError, ValueError, sqlite3.DatabaseError):
        return _abstained_provider(row, "external_provider_projection_invalid")


def _provider_status(
    connection: sqlite3.Connection,
    row: sqlite3.Row | Mapping[str, object],
) -> _ProviderStatusProjection:
    try:
        tool_run_id = int(str(row["tool_run_id"]))
    except (KeyError, TypeError, ValueError):
        return (
            _abstained_provider(row, "external_provider_projection_invalid"),
            (),
            None,
            (),
            (),
        )
    owner = connection.execute(
        """SELECT a.status FROM external_tool_runs r
        JOIN analysis_runs a ON a.analysis_run_id=r.analysis_run_id
        WHERE r.tool_run_id=?""",
        (tool_run_id,),
    ).fetchone()
    if owner is None or str(owner["status"]) != "completed":
        return (
            _abstained_provider(row, "external_provider_owner_not_completed"),
            (),
            None,
            (),
            (),
        )
    try:
        context = _provider_read_context(connection, row)
        terminal = _terminal_provider_projection(row, context)
        if terminal is not None:
            return terminal
        evidence = _ready_provider_evidence(connection, row, context)
        return _ready_provider_projection(row, context, evidence)
    except (KeyError, TypeError, ValueError, sqlite3.DatabaseError):
        return (
            _abstained_provider(row, "external_provider_projection_invalid"),
            (),
            None,
            (),
            (),
        )


def _legacy_provider_status(status: ExternalEvidenceStatus) -> ExternalProviderStatus:
    return ExternalProviderStatus(
        "ruff-protected-basic",
        "neocortex.ruff-protected-basic/v1",
        "protected",
        "ruff",
        status.tool_version,
        status.status,
        status.reason,
        status.execution,
        status.eligible_files,
        status.covered_files,
        status.diagnostics,
        status.added,
        status.resolved,
        status.comparable,
        status.result_digest,
        status.configuration_signature,
        status.gate,
    )


def _runtime_group_configuration(
    row: sqlite3.Row | Mapping[str, object],
) -> tuple[Mapping[str, object] | None, str | None, str | None]:
    if str(row["profile"]) != "trusted-deep":
        return None, None, None
    try:
        provenance = json.loads(str(row["provenance_json"]))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None, None, "external_provider_deep_configuration_invalid"
    recorded = provenance.get("deep_configuration") if isinstance(provenance, dict) else None
    if not isinstance(recorded, dict) or set(recorded) != {"payload", "signature"}:
        return None, None, "external_provider_deep_configuration_invalid"
    payload = recorded.get("payload")
    signature = recorded.get("signature")
    if not isinstance(payload, dict) or not isinstance(signature, str) or not signature:
        return None, None, "external_provider_deep_configuration_invalid"
    return payload, signature, None


def _runtime_group(
    row: sqlite3.Row | Mapping[str, object],
) -> tuple[
    _RuntimeGroupKey | None,
    Mapping[str, object] | None,
    str | None,
    str | None,
]:
    profile_value = str(row["profile"])
    if profile_value not in {"protected", "trusted-static", "trusted-deep"}:
        return None, None, None, "external_provider_profile_unsupported"
    configuration, signature, reason = _runtime_group_configuration(row)
    if reason is not None:
        return None, None, None, reason
    try:
        configuration_json = None if configuration is None else canonical_json(configuration)
    except (TypeError, ValueError):
        return None, None, None, "external_provider_deep_configuration_invalid"
    return (
        (profile_value, str(row["observed_root"]), signature, configuration_json),
        configuration,
        signature,
        None,
    )


def _runtime_provider_reason(
    row: sqlite3.Row | Mapping[str, object],
    provider: ExternalEvidenceProvider | None,
) -> str | None:
    if provider is None:
        return "external_provider_not_registered"
    try:
        version = provider.tool_version()
    except (OSError, RuntimeError, TypeError, ValueError):
        return "external_provider_runtime_probe_failed"
    if version is None:
        return "external_provider_runtime_unavailable"
    descriptor = provider.descriptor
    if (
        version != str(row["tool_version"])
        or descriptor.provider_schema != str(row["provider_schema"])
        or descriptor.configuration_signature != str(row["configuration_signature"])
        or descriptor.environment_signature != str(row["environment_signature"])
        or descriptor.comparability_signature != str(row["comparability_signature"])
    ):
        return "external_provider_runtime_stale"
    return None


def _current_runtime_reasons(
    rows: Sequence[sqlite3.Row | Mapping[str, object]],
) -> dict[int, str | None]:
    """Probe each distinct provider runtime once for one immutable suite."""

    from .external_evidence_providers import providers_for_profile

    reasons: dict[int, str | None] = {}
    groups: dict[_RuntimeGroupKey, list[sqlite3.Row | Mapping[str, object]]] = {}
    configurations: dict[
        _RuntimeGroupKey,
        tuple[Mapping[str, object] | None, str | None],
    ] = {}
    for row in rows:
        tool_run_id = int(str(row["tool_run_id"]))
        key, configuration, signature, reason = _runtime_group(row)
        if key is None:
            reasons[tool_run_id] = reason or "external_provider_runtime_probe_failed"
            continue
        groups.setdefault(key, []).append(row)
        configurations[key] = (configuration, signature)
    for key, grouped_rows in groups.items():
        profile_value, observed_root, _signature_key, _configuration_json = key
        configuration, signature = configurations[key]
        try:
            providers = providers_for_profile(
                cast(AnalysisProfile, profile_value),
                Path(observed_root),
                deep_configuration=configuration,
                deep_configuration_signature=signature,
            )
            providers_by_id = {item.descriptor.provider_id: item for item in providers}
        except (OSError, RuntimeError, TypeError, ValueError):
            for row in grouped_rows:
                reasons[int(str(row["tool_run_id"]))] = "external_provider_runtime_probe_failed"
            continue
        for row in grouped_rows:
            tool_run_id = int(str(row["tool_run_id"]))
            reasons[tool_run_id] = _runtime_provider_reason(
                row,
                providers_by_id.get(str(row["provider_id"])),
            )
    return reasons


def _current_runtime_reason(
    row: sqlite3.Row | Mapping[str, object],
) -> str | None:
    """Return why one historical provider cannot represent the current runtime."""

    try:
        tool_run_id = int(str(row["tool_run_id"]))
        selected = row
    except (KeyError, TypeError, ValueError, IndexError):
        tool_run_id = 0
        selected = {**dict(row), "tool_run_id": tool_run_id}
    return _current_runtime_reasons((selected,)).get(tool_run_id)


def _type_consensus(
    provider_findings: Mapping[str, Sequence[ExternalProviderFinding]],
    provider_statuses: Mapping[str, ExternalProviderStatus],
) -> TypeConsensusSummary:
    mypy_id = "mypy-trusted-project"
    pyright_id = "pyright-trusted-project"
    mypy_status = provider_statuses.get(mypy_id)
    pyright_status = provider_statuses.get(pyright_id)
    if (
        mypy_status is None
        or pyright_status is None
        or mypy_status.status != "ready"
        or pyright_status.status != "ready"
        or mypy_status.covered_files != mypy_status.eligible_files
        or pyright_status.covered_files != pyright_status.eligible_files
    ):
        return TypeConsensusSummary("not_comparable", not_comparable=1)

    def keys(provider_id: str) -> set[tuple[str, int, str]]:
        return {
            (item.relative_path.casefold(), item.start_line, item.category)
            for item in provider_findings.get(provider_id, ())
        }

    mypy_keys = keys(mypy_id)
    pyright_keys = keys(pyright_id)
    both = mypy_keys & pyright_keys
    return TypeConsensusSummary(
        "both_report",
        both_report=len(both),
        mypy_only=len(mypy_keys - pyright_keys),
        pyright_only=len(pyright_keys - mypy_keys),
    )


def _gate_evaluations(
    statuses: Mapping[str, ExternalProviderStatus],
) -> tuple[ProviderGateEvaluation, ...]:
    definitions = (
        ("ruff-protected-basic", "no_added_ruff_basic_diagnostics"),
        ("ruff-trusted-project", "no_added_ruff_project_diagnostics"),
        ("mypy-trusted-project", "no_added_mypy_errors"),
        ("pyright-trusted-project", "no_added_pyright_errors"),
    )
    gates: list[ProviderGateEvaluation] = []
    for provider_id, gate_name in definitions:
        status = statuses.get(provider_id)
        if status is None:
            gates.append(
                ProviderGateEvaluation(
                    gate_name, provider_id, "not_evaluated", "provider_not_recorded"
                )
            )
        elif status.status != "ready":
            gates.append(
                ProviderGateEvaluation(
                    gate_name,
                    provider_id,
                    "abstained",
                    status.reason or "provider_not_ready",
                )
            )
        else:
            gates.append(
                ProviderGateEvaluation(
                    gate_name,
                    provider_id,
                    status.gate,
                    "no_added_findings" if status.gate == "passed" else status.gate,
                )
            )
    for gate_name in ("public_type_surface_not_degraded", "type_coverage_not_degraded"):
        gates.append(
            ProviderGateEvaluation(
                gate_name,
                "type-consensus",
                "not_evaluated",
                "comparable_type_metric_not_recorded",
            )
        )
    return tuple(gates)


def _normalized_provider_filter(
    provider_ids: Collection[str] | None,
) -> tuple[str, ...] | None:
    if provider_ids is None:
        return None
    if isinstance(provider_ids, (str, bytes)):
        raise TypeError("external provider filter must be a collection of identities")
    supplied = tuple(provider_ids)
    if any(not isinstance(provider_id, str) for provider_id in supplied):
        raise TypeError("external provider filter identities must be strings")
    requested = tuple(sorted(set(supplied)))
    if len(requested) > _PROVIDER_STATUS_LIMIT:
        raise ValueError("external provider filter exceeds its provider bound")
    if any(not provider_id or len(provider_id) > 256 for provider_id in requested):
        raise ValueError("external provider filter contains an invalid identity")
    return requested


def _provider_run_rows(
    connection: sqlite3.Connection,
    analysis_run_id: int,
    provider_ids: Collection[str] | None,
) -> list[sqlite3.Row]:
    requested = _normalized_provider_filter(provider_ids)
    if requested == ():
        return []
    if requested is None:
        return connection.execute(
            """SELECT r.tool_run_id,r.tool_name,r.tool_version,r.status,
            r.configuration_signature,r.provenance_json,c.* FROM external_tool_runs r
            JOIN external_run_contracts c ON c.tool_run_id=r.tool_run_id
            WHERE r.analysis_run_id=?
            ORDER BY c.provider_id,r.tool_run_id DESC LIMIT ?""",
            (analysis_run_id, _PROVIDER_STATUS_LIMIT + 1),
        ).fetchall()
    placeholders = ",".join("?" for _item in requested)
    return connection.execute(
        f"""SELECT r.tool_run_id,r.tool_name,r.tool_version,r.status,
        r.configuration_signature,r.provenance_json,c.* FROM external_tool_runs r
        JOIN external_run_contracts c ON c.tool_run_id=r.tool_run_id
        WHERE r.analysis_run_id=? AND c.provider_id IN ({placeholders})
        ORDER BY c.provider_id,r.tool_run_id DESC LIMIT ?""",
        (analysis_run_id, *requested, _PROVIDER_STATUS_LIMIT + 1),
    ).fetchall()


def _latest_provider_rows(rows: Sequence[sqlite3.Row]) -> dict[str, sqlite3.Row]:
    latest: dict[str, sqlite3.Row] = {}
    for row in rows:
        latest.setdefault(str(row["provider_id"]), row)
    return latest


def _read_provider_suite_status(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    runtime_reason: str | None,
) -> tuple[ExternalProviderStatus, tuple[ExternalProviderFinding, ...]]:
    status, findings, _effective_run_id, _metrics, _relations = _provider_status(
        connection,
        row,
    )
    if runtime_reason is None:
        return status, findings
    return (
        replace(
            status,
            status="abstained",
            reason=runtime_reason,
            gate="not_evaluated",
        ),
        (),
    )


def _read_provider_suite_statuses(
    connection: sqlite3.Connection,
    latest: Mapping[str, sqlite3.Row],
    *,
    enforce_current_runtime: bool,
) -> tuple[
    list[ExternalProviderStatus],
    dict[str, tuple[ExternalProviderFinding, ...]],
]:
    statuses: list[ExternalProviderStatus] = []
    findings: dict[str, tuple[ExternalProviderFinding, ...]] = {}
    runtime_reasons = (
        _current_runtime_reasons(
            tuple(row for row in latest.values() if str(row["status"]) in {"completed", "skipped"})
        )
        if enforce_current_runtime
        else {}
    )
    for provider_id in sorted(latest):
        row = latest[provider_id]
        runtime_reason = (
            runtime_reasons.get(int(row["tool_run_id"]))
            if enforce_current_runtime and str(row["status"]) in {"completed", "skipped"}
            else None
        )
        status, provider_findings = _read_provider_suite_status(
            connection,
            row,
            runtime_reason=runtime_reason,
        )
        statuses.append(status)
        findings[provider_id] = provider_findings
    return statuses, findings


def _append_legacy_provider_status(
    connection: sqlite3.Connection,
    analysis_run_id: int,
    requested: tuple[str, ...] | None,
    latest: Mapping[str, sqlite3.Row],
    statuses: list[ExternalProviderStatus],
    *,
    enforce_current_runtime: bool,
) -> None:
    legacy_requested = requested is None or "ruff-protected-basic" in requested
    if "ruff-protected-basic" in latest or not legacy_requested:
        return
    legacy, _ids, _row = read_external_evidence(
        connection,
        analysis_run_id,
        enforce_current_runtime=enforce_current_runtime,
    )
    if legacy.status != "not_recorded":
        statuses.append(_legacy_provider_status(legacy))


def _suite_profile(statuses: Sequence[ExternalProviderStatus]) -> AnalysisProfile:
    if any(item.profile == "trusted-deep" for item in statuses):
        return "trusted-deep"
    if any(item.profile == "trusted-static" for item in statuses):
        return "trusted-static"
    return "protected"


def _suite_status(statuses: Sequence[ExternalProviderStatus]) -> _SuiteStatus:
    if not statuses:
        return "not_recorded"
    if all(item.status == "ready" for item in statuses):
        return "ready"
    if any(item.status == "ready" for item in statuses):
        return "partial"
    return "abstained"


def _provider_bound_abstention() -> ExternalEvidenceSuiteStatus:
    return ExternalEvidenceSuiteStatus(
        "protected",
        "abstained",
        (),
        TypeConsensusSummary("not_comparable", not_comparable=1),
        (),
    )


def read_external_evidence_suite(
    connection: sqlite3.Connection,
    analysis_run_id: int,
    *,
    enforce_current_runtime: bool,
    provider_ids: Collection[str] | None = None,
) -> ExternalEvidenceSuiteStatus:
    """Read every normalized provider while preserving the legacy Ruff fallback."""

    requested = _normalized_provider_filter(provider_ids)
    rows = _provider_run_rows(connection, analysis_run_id, requested)
    if len(rows) > _PROVIDER_STATUS_LIMIT:
        return _provider_bound_abstention()
    latest = _latest_provider_rows(rows)
    statuses, findings = _read_provider_suite_statuses(
        connection,
        latest,
        enforce_current_runtime=enforce_current_runtime,
    )
    _append_legacy_provider_status(
        connection,
        analysis_run_id,
        requested,
        latest,
        statuses,
        enforce_current_runtime=enforce_current_runtime,
    )
    statuses.sort(key=lambda item: item.provider_id)
    status_map = {item.provider_id: item for item in statuses}
    return ExternalEvidenceSuiteStatus(
        _suite_profile(statuses),
        _suite_status(statuses),
        tuple(statuses),
        _type_consensus(findings, status_map),
        _gate_evaluations(status_map),
    )


def read_external_validation_provider_statuses(
    connection: sqlite3.Connection,
    analysis_run_id: int,
    *,
    enforce_current_runtime: bool,
) -> tuple[AnalysisProfile, tuple[ExternalProviderStatus, ...]]:
    """Verify the receipt-bound provider suite with bounded resident memory."""

    rows = _provider_run_rows(connection, analysis_run_id, None)
    if len(rows) > _PROVIDER_STATUS_LIMIT:
        return "protected", ()
    latest = _latest_provider_rows(rows)
    runtime_reasons = (
        _current_runtime_reasons(
            tuple(row for row in latest.values() if str(row["status"]) in {"completed", "skipped"})
        )
        if enforce_current_runtime
        else {}
    )
    statuses: list[ExternalProviderStatus] = []
    for provider_id in sorted(latest):
        row = latest[provider_id]
        status = _validation_provider_status(connection, row)
        runtime_reason = (
            runtime_reasons.get(int(row["tool_run_id"]))
            if str(row["status"]) in {"completed", "skipped"}
            else None
        )
        if runtime_reason is not None:
            status = replace(
                status,
                status="abstained",
                reason=runtime_reason,
                gate="not_evaluated",
            )
        statuses.append(status)
    _append_legacy_provider_status(
        connection,
        analysis_run_id,
        None,
        latest,
        statuses,
        enforce_current_runtime=enforce_current_runtime,
    )
    statuses.sort(key=lambda item: item.provider_id)
    return _suite_profile(statuses), tuple(statuses)


def read_external_provider_evidence(
    connection: sqlite3.Connection,
    analysis_run_id: int,
    *,
    provider_ids: Collection[str] | None = None,
) -> dict[str, ExternalProviderEvidence]:
    """Read latest provider evidence, resolving exact replays to their source.

    ``provider_ids`` restricts the projection at the SQL boundary.  The
    selected providers retain the same status, digest and exact-replay
    validation as an unfiltered read; unrequested provider payloads are never
    deserialized.  Omitting the filter preserves the historical all-provider
    contract.
    """

    rows = _provider_run_rows(connection, analysis_run_id, provider_ids)
    if len(rows) > _PROVIDER_STATUS_LIMIT:
        raise ValueError("external provider evidence exceeds its provider bound")
    latest: dict[str, sqlite3.Row] = {}
    for row in rows:
        latest.setdefault(str(row["provider_id"]), row)
    result: dict[str, ExternalProviderEvidence] = {}
    for provider_id, row in sorted(latest.items()):
        status, findings, effective_run_id, metrics, relations = _provider_status(
            connection,
            row,
        )
        tool_run_id = int(row["tool_run_id"])
        if status.status != "ready":
            result[provider_id] = ExternalProviderEvidence(
                provider_id,
                tool_run_id,
                None,
                "abstained",
                status.reason,
            )
            continue
        if effective_run_id is None:
            result[provider_id] = ExternalProviderEvidence(
                provider_id,
                tool_run_id,
                None,
                "abstained",
                "external_provider_projection_invalid",
            )
            continue
        result[provider_id] = ExternalProviderEvidence(
            provider_id,
            tool_run_id,
            effective_run_id,
            "ready",
            None,
            findings,
            metrics,
            relations,
        )
    return result


def read_external_provider_attestation(
    connection: sqlite3.Connection,
    *,
    analysis_run_id: int,
    tool_run_id: int,
    expected_processing_signature: str,
    expected_provider_id: str,
    expected_provider_schema: str,
    enforce_current_runtime: bool = True,
) -> ExternalProviderAttestation:
    """Read one ready publication and its exact persisted provider contract.

    The projection resolves an exact cache replay to its immutable source
    evidence, verifies the current runtime contract when requested and rejects
    partial, stale or unrecorded publications.  It is the canonical boundary
    for consumers that need to attest existing evidence instead of relaunching
    a provider.
    """

    if (
        isinstance(analysis_run_id, bool)
        or not isinstance(analysis_run_id, int)
        or analysis_run_id < 1
    ):
        raise ValueError("external provider attestation analysis run is invalid")
    if isinstance(tool_run_id, bool) or not isinstance(tool_run_id, int) or tool_run_id < 1:
        raise ValueError("external provider attestation tool run is invalid")
    for label, value in (
        ("processing signature", expected_processing_signature),
        ("provider identity", expected_provider_id),
        ("provider schema", expected_provider_schema),
    ):
        if not isinstance(value, str) or not value or value.strip() != value:
            raise ValueError(f"external provider attestation {label} is invalid")
    owner = connection.execute(
        "SELECT status,processing_signature FROM analysis_runs WHERE analysis_run_id=?",
        (analysis_run_id,),
    ).fetchone()
    if (
        owner is None
        or str(owner["status"]) != "completed"
        or str(owner["processing_signature"]) != expected_processing_signature
    ):
        raise ValueError("external_provider_attestation_owner_mismatch")
    requested = _normalized_provider_filter((expected_provider_id,))
    assert requested is not None
    rows = _provider_run_rows(connection, analysis_run_id, requested)
    if len(rows) > _PROVIDER_STATUS_LIMIT:
        raise ValueError("external provider attestation exceeds its provider bound")
    matches = tuple(row for row in rows if int(row["tool_run_id"]) == tool_run_id)
    if len(matches) != 1:
        raise ValueError("external_provider_attestation_exact_run_not_recorded")
    row = matches[0]
    if (
        str(row["provider_id"]) != expected_provider_id
        or str(row["provider_schema"]) != expected_provider_schema
    ):
        raise ValueError("external_provider_attestation_provider_mismatch")
    if enforce_current_runtime and (runtime_reason := _current_runtime_reason(row)) is not None:
        raise ValueError(runtime_reason)
    status, findings, effective_run_id, metrics, relations = _provider_status(connection, row)
    if status.status != "ready" or effective_run_id is None:
        raise ValueError(status.reason or "external_provider_attestation_not_ready")
    if status.result_digest is None or status.execution not in {"full", "cache_replay"}:
        raise ValueError("external_provider_attestation_contract_incomplete")
    tool_status = str(row["status"])
    if tool_status not in {"completed", "skipped"}:
        raise ValueError("external_provider_attestation_tool_not_terminal")
    context = _provider_read_context(connection, row)
    inputs = tuple(
        ExternalRunInput(
            int(item["version_id"]),
            str(item["portable_input_id"]),
            str(item["relative_path"]),
            bool(item["eligible"]),
            bool(item["covered"]),
            None if item["coverage_reason"] is None else str(item["coverage_reason"]),
            int(item["size"]),
            str(item["content_digest"]),
        )
        for item in context.inputs
    )
    expected_publication_id = external_signature(
        "external-publication-v1",
        {
            "provider_id": status.provider_id,
            "provider_schema": status.provider_schema,
            "profile": status.profile,
            "configuration_signature": str(row["configuration_signature"]),
            "environment_signature": str(row["environment_signature"]),
            "input_signature": str(row["input_signature"]),
            "result_digest": status.result_digest,
        },
    )
    if str(row["portable_publication_id"]) != expected_publication_id:
        raise ValueError("external_provider_attestation_publication_identity_invalid")
    return ExternalProviderAttestation(
        analysis_run_id=analysis_run_id,
        processing_signature=expected_processing_signature,
        provider_id=status.provider_id,
        provider_schema=status.provider_schema,
        profile=status.profile,
        tool_run_id=tool_run_id,
        effective_tool_run_id=effective_run_id,
        tool_name=str(row["tool_name"]),
        tool_version=str(row["tool_version"]),
        tool_status=cast(Literal["completed", "skipped"], tool_status),
        execution=cast(Literal["full", "cache_replay"], status.execution),
        observed_root=str(row["observed_root"]),
        root_identity=str(row["root_identity"]),
        input_signature=str(row["input_signature"]),
        descriptor_configuration_signature=str(row["configuration_signature"]),
        environment_signature=str(row["environment_signature"]),
        comparability_signature=str(row["comparability_signature"]),
        result_digest=status.result_digest,
        portable_publication_id=expected_publication_id,
        coverage_complete=bool(row["coverage_complete"]),
        content_executed=status.content_executed,
        eligible_files=status.eligible_files,
        covered_files=status.covered_files,
        counters=dict(status.counters),
        inputs=inputs,
        limitations=status.limitations,
        findings=findings,
        metrics=metrics,
        relations=relations,
    )


def read_external_provider_finding_ids(
    connection: sqlite3.Connection,
    analysis_run_id: int,
) -> dict[str, frozenset[str]]:
    """Return portable finding identities for each latest normalized provider."""

    rows = _provider_run_rows(connection, analysis_run_id, None)
    if len(rows) > _PROVIDER_STATUS_LIMIT:
        raise ValueError("external provider identity read exceeds its bound")
    latest: dict[str, sqlite3.Row] = {}
    for row in rows:
        latest.setdefault(str(row["provider_id"]), row)
    result: dict[str, frozenset[str]] = {}
    for provider_id, row in latest.items():
        try:
            effective = _effective_provider_run_id(connection, row)
        except (TypeError, ValueError):
            result[provider_id] = frozenset()
            continue
        result[provider_id] = frozenset(_portable_finding_ids(connection, effective, provider_id))
    return result


def read_external_provider_findings(
    connection: sqlite3.Connection,
    analysis_run_id: int,
    *,
    provider_ids: Collection[str] | None = None,
) -> dict[str, tuple[ExternalProviderFinding, ...]]:
    """Return bounded finding details for selected latest normalized providers."""

    rows = _provider_run_rows(connection, analysis_run_id, provider_ids)
    if len(rows) > _PROVIDER_STATUS_LIMIT:
        raise ValueError("external provider finding read exceeds its provider bound")
    latest: dict[str, sqlite3.Row] = {}
    for row in rows:
        latest.setdefault(str(row["provider_id"]), row)
    result: dict[str, tuple[ExternalProviderFinding, ...]] = {}
    for provider_id, row in sorted(latest.items()):
        try:
            effective = _effective_provider_run_id(connection, row)
        except (TypeError, ValueError):
            result[provider_id] = ()
            continue
        result[provider_id] = _portable_provider_findings(
            connection,
            effective,
            provider_id,
        )
    return result


__all__ = [
    "publish_external_provider",
    "read_external_evidence_suite",
    "read_external_provider_attestation",
    "read_external_provider_baselines",
    "read_external_provider_evidence",
    "read_external_provider_finding_ids",
    "read_external_provider_findings",
    "read_external_validation_provider_statuses",
]


_preserve_legacy_module(globals(), "_04_Nucleo_Operativo.external_evidence_store")
