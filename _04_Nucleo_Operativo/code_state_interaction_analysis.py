"""Static SQL, state-store, transaction, and workflow evidence for Code.

The analyzer resolves exact source blobs from one immutable Code publication,
uses Python's AST only to recover literal SQL call arguments and enclosing
symbols, and delegates SQL syntax/table semantics to SQLGlot's SQLite dialect.
Dynamic SQL, unresolved calls, owner ambiguity, and bounded samples are kept as
missing evidence.  Name spellings such as ``execute`` or ``commit`` never prove
database ownership by themselves.

This module performs no repository imports, source execution, database writes,
or semantic decisions.  Its output is an advisory observation that can be
joined to explicit logical-owner, state-store, and workflow contracts.
"""

from __future__ import annotations

import ast
import json
import logging
import sqlite3
import zlib
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence, cast

try:  # The analysis extra is optional in the product runtime.
    import sqlglot
    from sqlglot import exp
except ImportError:  # pragma: no cover - exercised in an isolated import test
    sqlglot = None
    exp = None

from .code_analysis_epistemics import (
    AnalysisEvidenceRef,
    AnalysisEvidenceRequirementSpec,
    AnalysisFact,
    AnalysisNextActionSpec,
    AnalysisQuestionEvaluation,
    AnalysisQuestionSpec,
    AnalysisRequirementEvaluation,
    AnalysisSubjectRef,
    analysis_identity,
    analysis_question_spec_fingerprint,
    validate_analysis_question_evaluation,
)
from .code_schema import CODE_SCHEMA_VERSION, validate_code_schema
from .logical_owner_contracts import (
    LOGICAL_OWNER_CONTRACT_SCHEMA,
    LOGICAL_OWNER_SPECS,
    logical_owner_registry_fingerprint,
    matching_logical_owners,
)
from .semantic_models import fingerprint_text
from .sqlite_immutable import ImmutableSQLiteUnavailable, immutable_sqlite_database
from .state_topology_contracts import (
    DURABLE_WORKFLOW_BINDING_SCHEMA,
    STATE_STORE_REGISTRY,
    STATE_STORE_REGISTRY_SCHEMA,
    TEXT_DERIVATION_IMPLEMENTATION_BINDING,
    TEXT_DERIVATION_WORKFLOW,
)

CODE_STATE_INTERACTION_SCHEMA = "neocortex.code-state-interaction/v1"
CODE_STATE_INTERACTION_POLICY = "literal-sql-sqlglot-sqlite-explicit-ownership-v1"
CODE_STATE_INTERACTION_EXAMPLE_LIMIT = 100
CODE_STATE_INTERACTION_MAX_STATEMENTS = 50_000

SQL_INTERACTION_QUESTION = AnalysisQuestionSpec(
    question_id="state.static_sql_interactions_are_resolved",
    version="v1",
    subject_kinds=("project",),
    requirements=(
        AnalysisEvidenceRequirementSpec(
            "literal_sql_projection",
            "question",
            "supporting",
            ("internal_relation",),
            accepted_completeness=("complete", "partial"),
            allow_truncated=True,
        ),
        AnalysisEvidenceRequirementSpec(
            "dynamic_sql_and_runtime_calls_resolved",
            "decision",
            "supporting",
            ("runtime_observation", "experiment_result"),
        ),
        AnalysisEvidenceRequirementSpec(
            "state_store_mapping_counterevidence_evaluated",
            "decision",
            "counterevidence",
            ("contract", "runtime_observation", "experiment_result"),
        ),
        AnalysisEvidenceRequirementSpec(
            "bounded_sql_runtime_trace_result",
            "decision",
            "experiment_result",
            ("experiment_result",),
        ),
    ),
    hypotheses=(
        "literal_sql_and_explicit_ownership_describe_the_relevant_state_interactions",
        "dynamic_sql_runtime_dispatch_or_ambiguous_ownership_hide_material_interactions",
    ),
    counterevidence_rules=(
        "execute_spelling_without_a_literal_argument_is_not_sql_evidence",
        "a_table_name_does_not_identify_a_state_store_without_an_explicit_owner_contract",
        "static_transaction_calls_do_not_prove_runtime_commit_order_or_atomicity",
    ),
    next_actions=(
        AnalysisNextActionSpec(
            "characterize_unresolved_dynamic_sql",
            "characterization",
            "Resolve bounded dynamic SQL sites without guessing their tables or operations.",
        ),
        AnalysisNextActionSpec(
            "seek_state_store_mapping_counterevidence",
            "counterevidence_search",
            "Verify connection factories and explicit state-store contracts for ambiguous sites.",
        ),
        AnalysisNextActionSpec(
            "trace_sql_and_transaction_events_in_isolation",
            "experiment",
            "Trace queries, connections, commits, rollbacks, and correlation IDs in an isolated scenario.",
        ),
    ),
)

WORKFLOW_SQL_QUESTION = AnalysisQuestionSpec(
    question_id="state.declared_workflow_sql_matches_implementation",
    version="v1",
    subject_kinds=("workflow",),
    requirements=(
        AnalysisEvidenceRequirementSpec(
            "workflow_implementation_binding",
            "question",
            "supporting",
            ("contract",),
        ),
        AnalysisEvidenceRequirementSpec(
            "bound_symbol_sql_projection",
            "question",
            "supporting",
            ("internal_relation",),
            accepted_completeness=("complete", "partial"),
            allow_truncated=True,
        ),
        AnalysisEvidenceRequirementSpec(
            "runtime_transaction_order_observed",
            "decision",
            "supporting",
            ("runtime_observation", "experiment_result"),
        ),
        AnalysisEvidenceRequirementSpec(
            "indirect_helper_and_dynamic_sql_counterevidence_evaluated",
            "decision",
            "counterevidence",
            ("internal_relation", "runtime_observation", "experiment_result"),
        ),
        AnalysisEvidenceRequirementSpec(
            "workflow_fault_boundary_experiment_result",
            "decision",
            "experiment_result",
            ("experiment_result",),
        ),
    ),
    hypotheses=(
        "declared_workflow_tables_are_present_in_the_bound_implementation",
        "indirect_helpers_dynamic_sql_or_runtime_order_leave_the_boundary_incompletely_observed",
    ),
    counterevidence_rules=(
        "required_tables_can_be_accessed_by_indirect_helpers_outside_the_bound_symbol",
        "a_static_begin_or_commit_call_does_not_observe_the_runtime_happens_before_relation",
        "extra_tables_can_be_deliberate_conditional_writes",
    ),
    next_actions=(
        AnalysisNextActionSpec(
            "inspect_workflow_sql_delta",
            "characterization",
            "Inspect missing, conditional, and extra tables against exact bound symbols.",
        ),
        AnalysisNextActionSpec(
            "resolve_indirect_workflow_helpers",
            "counterevidence_search",
            "Resolve bounded helper calls before interpreting a missing table as a contract drift.",
        ),
        AnalysisNextActionSpec(
            "inject_failure_at_each_durable_boundary",
            "experiment",
            "Terminate after each durable boundary in an isolated state and verify restart convergence.",
        ),
    ),
)

_LIMITATIONS = (
    "only_literal_sql_arguments_are_parsed",
    "python_ast_does_not_resolve_connection_object_identity",
    "state_store_mapping_requires_one_explicit_logical_owner_with_one_declared_state_owner",
    "static_calls_do_not_observe_runtime_transaction_order",
    "bound_workflow_symbols_do_not_include_unresolved_indirect_helper_effects",
    "sqlglot_parse_success_is_syntax_and_table_evidence_not_behavioral_correctness",
    "human_decision_and_mutation_authority_are_not_owned_by_this_analysis",
)


def _required_text(label: str, value: object, *, maximum: int = 32_768) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"{label} must be non-empty trimmed text")
    if len(value) > maximum:
        raise ValueError(f"{label} exceeds its bound")
    return value


def _text_tuple(label: str, value: object, *, maximum: int = 32_768) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError(f"{label} must be a sequence")
    result = tuple(_required_text(label, item, maximum=maximum) for item in value)
    if len(set(result)) != len(result):
        raise ValueError(f"{label} cannot repeat")
    return result


def _non_negative(label: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


@dataclass(frozen=True, slots=True)
class StaticSqlInteraction:
    interaction_id: str
    version_id: int
    path: str
    module_id: str
    symbol: str
    line: int
    call_name: Literal["execute", "executemany", "executescript"]
    operation: str
    read_tables: tuple[str, ...]
    write_tables: tuple[str, ...]
    ddl_tables: tuple[str, ...]
    sql_digest: str
    statement_count: int
    logical_owner_ids: tuple[str, ...]
    state_owner_ids: tuple[str, ...]
    state_store_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        for label, value in (
            ("SQL interaction id", self.interaction_id),
            ("SQL path", self.path),
            ("SQL module", self.module_id),
            ("SQL symbol", self.symbol),
            ("SQL operation", self.operation),
            ("SQL digest", self.sql_digest),
        ):
            _required_text(label, value)
        if self.call_name not in {"execute", "executemany", "executescript"}:
            raise ValueError("SQL call name is invalid")
        if self.version_id < 1 or self.line < 1 or self.statement_count < 1:
            raise ValueError("SQL source identity and statement count must be positive")
        for label, values in (
            ("SQL read table", self.read_tables),
            ("SQL write table", self.write_tables),
            ("SQL DDL table", self.ddl_tables),
            ("SQL logical owner", self.logical_owner_ids),
            ("SQL state owner", self.state_owner_ids),
            ("SQL state store", self.state_store_ids),
        ):
            _text_tuple(label, values)


@dataclass(frozen=True, slots=True)
class StaticTransactionEvent:
    event_id: str
    version_id: int
    path: str
    module_id: str
    symbol: str
    line: int
    event_kind: Literal["begin", "commit", "rollback", "savepoint", "release"]
    evidence_kind: Literal["method_call", "literal_sql"]
    connection_expression: str
    logical_owner_ids: tuple[str, ...]
    state_owner_ids: tuple[str, ...]
    state_store_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        for label, value in (
            ("transaction event id", self.event_id),
            ("transaction path", self.path),
            ("transaction module", self.module_id),
            ("transaction symbol", self.symbol),
            ("transaction connection expression", self.connection_expression),
        ):
            _required_text(label, value)
        if self.version_id < 1 or self.line < 1:
            raise ValueError("transaction source identity must be positive")
        if self.event_kind not in {"begin", "commit", "rollback", "savepoint", "release"}:
            raise ValueError("transaction event kind is invalid")
        if self.evidence_kind not in {"method_call", "literal_sql"}:
            raise ValueError("transaction evidence kind is invalid")
        for label, values in (
            ("transaction logical owner", self.logical_owner_ids),
            ("transaction state owner", self.state_owner_ids),
            ("transaction state store", self.state_store_ids),
        ):
            _text_tuple(label, values)


@dataclass(frozen=True, slots=True)
class WorkflowBoundarySqlObservation:
    boundary_id: str
    bound_symbols: tuple[str, ...]
    resolved_symbols: tuple[str, ...]
    observed_read_tables: tuple[str, ...]
    observed_write_tables: tuple[str, ...]
    observed_ddl_tables: tuple[str, ...]
    missing_required_read_tables: tuple[str, ...]
    missing_required_write_tables: tuple[str, ...]
    observed_conditional_write_tables: tuple[str, ...]
    unexpected_write_tables: tuple[str, ...]
    transaction_event_kinds: tuple[str, ...]
    unresolved_dynamic_sql_sites: int
    status: Literal["observed", "partial", "abstained"]

    def __post_init__(self) -> None:
        _required_text("workflow boundary", self.boundary_id)
        for label, values in (
            ("bound symbol", self.bound_symbols),
            ("resolved symbol", self.resolved_symbols),
            ("observed read table", self.observed_read_tables),
            ("observed write table", self.observed_write_tables),
            ("observed DDL table", self.observed_ddl_tables),
            ("missing read table", self.missing_required_read_tables),
            ("missing write table", self.missing_required_write_tables),
            ("conditional write table", self.observed_conditional_write_tables),
            ("unexpected write table", self.unexpected_write_tables),
            ("transaction event", self.transaction_event_kinds),
        ):
            _text_tuple(label, values)
        _non_negative("unresolved workflow SQL sites", self.unresolved_dynamic_sql_sites)
        if self.status not in {"observed", "partial", "abstained"}:
            raise ValueError("workflow SQL observation status is invalid")
        if not set(self.resolved_symbols).issubset(self.bound_symbols):
            raise ValueError("resolved workflow symbols must be declared")


@dataclass(frozen=True, slots=True)
class CodeStateInteractionAnalysis:
    analysis_id: str
    status: Literal["ready", "partial", "abstained"]
    reason: str | None
    policy_id: str
    database: str
    analysis_run_id: int | None
    source_processing_signature: str | None
    source_schema_version: int | None
    source_files: int
    source_files_with_text: int
    source_files_without_text: int
    literal_sql_sites: int
    parsed_sql_sites: int
    dynamic_sql_sites: int
    parse_error_sites: int
    statement_count: int
    interactions_count: int
    interactions: tuple[StaticSqlInteraction, ...]
    interactions_truncated: bool
    transaction_events_count: int
    transaction_events: tuple[StaticTransactionEvent, ...]
    transaction_events_truncated: bool
    dynamic_sql_examples: tuple[str, ...]
    parse_error_examples: tuple[str, ...]
    examples_truncated: bool
    workflow_boundaries: tuple[WorkflowBoundarySqlObservation, ...]
    sql_parser: str
    sql_parser_version: str | None
    logical_owner_contract_schema: str
    logical_owner_registry_fingerprint: str
    state_store_registry_schema: str
    workflow_contract_schema: str
    workflow_binding_schema: str
    limitations: tuple[str, ...] = _LIMITATIONS
    authority: Literal["advisory"] = "advisory"
    mutation_authority: Literal[False] = False

    def __post_init__(self) -> None:
        for label, value in (
            ("state interaction analysis id", self.analysis_id),
            ("state interaction policy", self.policy_id),
            ("state interaction database", self.database),
            ("SQL parser", self.sql_parser),
            ("logical-owner schema", self.logical_owner_contract_schema),
            ("logical-owner fingerprint", self.logical_owner_registry_fingerprint),
            ("state-store schema", self.state_store_registry_schema),
            ("workflow schema", self.workflow_contract_schema),
            ("workflow binding schema", self.workflow_binding_schema),
        ):
            _required_text(label, value)
        if self.status not in {"ready", "partial", "abstained"}:
            raise ValueError("state interaction status is invalid")
        if self.policy_id != CODE_STATE_INTERACTION_POLICY:
            raise ValueError("state interaction policy is invalid")
        if self.authority != "advisory" or self.mutation_authority:
            raise ValueError("state interaction analysis must remain advisory and non-mutating")
        for label, value in (
            ("source files", self.source_files),
            ("source files with text", self.source_files_with_text),
            ("source files without text", self.source_files_without_text),
            ("literal SQL sites", self.literal_sql_sites),
            ("parsed SQL sites", self.parsed_sql_sites),
            ("dynamic SQL sites", self.dynamic_sql_sites),
            ("SQL parse error sites", self.parse_error_sites),
            ("SQL statements", self.statement_count),
            ("SQL interactions", self.interactions_count),
            ("transaction events", self.transaction_events_count),
        ):
            _non_negative(label, value)
        if self.source_files_with_text + self.source_files_without_text != self.source_files:
            raise ValueError("state interaction source-file partition is invalid")
        if self.parsed_sql_sites + self.parse_error_sites != self.literal_sql_sites:
            raise ValueError("state interaction literal-SQL partition is invalid")
        if len(self.interactions) != min(
            self.interactions_count, CODE_STATE_INTERACTION_EXAMPLE_LIMIT
        ):
            raise ValueError("state interaction examples disagree with exact count")
        if self.interactions_truncated != (
            self.interactions_count > CODE_STATE_INTERACTION_EXAMPLE_LIMIT
        ):
            raise ValueError("state interaction truncation is invalid")
        if len(self.transaction_events) != min(
            self.transaction_events_count, CODE_STATE_INTERACTION_EXAMPLE_LIMIT
        ):
            raise ValueError("transaction event examples disagree with exact count")
        if self.transaction_events_truncated != (
            self.transaction_events_count > CODE_STATE_INTERACTION_EXAMPLE_LIMIT
        ):
            raise ValueError("transaction event truncation is invalid")
        _text_tuple("dynamic SQL example", self.dynamic_sql_examples)
        _text_tuple("SQL parse error example", self.parse_error_examples)
        expected_example_truncation = (
            self.dynamic_sql_sites > CODE_STATE_INTERACTION_EXAMPLE_LIMIT
            or self.parse_error_sites > CODE_STATE_INTERACTION_EXAMPLE_LIMIT
        )
        if self.examples_truncated != expected_example_truncation:
            raise ValueError("state interaction diagnostic truncation is invalid")
        if self.status == "abstained":
            if (
                self.reason is None
                or self.analysis_run_id is not None
                or self.source_processing_signature is not None
                or self.source_schema_version is not None
                or self.source_files
                or self.source_files_with_text
                or self.source_files_without_text
                or self.literal_sql_sites
                or self.parsed_sql_sites
                or self.dynamic_sql_sites
                or self.parse_error_sites
                or self.statement_count
                or self.interactions_count
                or self.interactions
                or self.transaction_events_count
                or self.transaction_events
                or self.dynamic_sql_examples
                or self.parse_error_examples
                or self.workflow_boundaries
            ):
                raise ValueError("abstained state interaction analysis requires a reason")
        elif (
            self.reason is not None
            or self.analysis_run_id is None
            or self.analysis_run_id < 1
            or self.source_processing_signature is None
            or self.source_schema_version != CODE_SCHEMA_VERSION
            or self.source_files < 1
        ):
            raise ValueError("observed state interaction analysis has invalid readiness")
        if self.sql_parser == "sqlglot" and self.sql_parser_version is None:
            raise ValueError("SQLGlot analysis requires its version")

    def digest_payload(self) -> dict[str, object]:
        return {
            "schema": CODE_STATE_INTERACTION_SCHEMA,
            **asdict(self),
        }

    def as_payload(self) -> dict[str, object]:
        return self.digest_payload()


@dataclass(frozen=True, slots=True)
class _Source:
    version_id: int
    path: str
    module_id: str
    text: str
    text_digest: str


def _module_id(path: str) -> str:
    normalized = path.replace("\\", "/")
    marker = "/Repository/"
    relative = normalized.split(marker, 1)[1] if marker in normalized else normalized.lstrip("/")
    if relative.endswith("/__init__.py"):
        relative = relative[: -len("/__init__.py")]
    elif relative.endswith(".py"):
        relative = relative[:-3]
    return relative.replace("/", ".")


def _literal_string(node: ast.AST) -> str | None:
    try:
        value = ast.literal_eval(node)
    except (ValueError, TypeError, MemoryError, RecursionError):
        return None
    return value if isinstance(value, str) else None


def _attribute_name(node: ast.AST) -> str | None:
    return node.attr if isinstance(node, ast.Attribute) else None


def _connection_expression(node: ast.AST) -> str:
    if isinstance(node, ast.Attribute):
        try:
            return ast.unparse(node.value)[:256]
        except (ValueError, RecursionError):
            return "unresolved_expression"
    return "literal_sql"


def _state_binding(module_id: str) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    logical = matching_logical_owners(module_id)
    if len(logical) != 1:
        return logical, (), ()
    spec = next(item for item in LOGICAL_OWNER_SPECS if item.owner_id == logical[0])
    owners = spec.state_owner_ids
    if len(owners) != 1:
        return logical, owners, ()
    try:
        store = STATE_STORE_REGISTRY.by_owner(owners[0])
    except ValueError:
        return logical, owners, ()
    return logical, owners, (store.state_store_id,)


def _write_target(expression: Any) -> str | None:
    if exp is None:
        return None
    target = getattr(expression, "this", None)
    if isinstance(target, exp.Schema):
        target = target.this
    return target.name if isinstance(target, exp.Table) and target.name else None


def _classify_sql(expression: Any) -> tuple[str, tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    if exp is None:
        raise RuntimeError("SQLGlot expressions are unavailable")
    operation = str(getattr(expression, "key", type(expression).__name__.lower()))
    tables = tuple(sorted({table.name for table in expression.find_all(exp.Table) if table.name}))
    write_target = _write_target(expression)
    writes: set[str] = set()
    ddl: set[str] = set()
    if isinstance(expression, (exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Replace)):
        if write_target:
            writes.add(write_target)
    elif isinstance(expression, (exp.Create, exp.Alter, exp.Drop, exp.TruncateTable)):
        if write_target:
            ddl.add(write_target)
            writes.add(write_target)
    reads = set(tables) - writes
    return operation, tuple(sorted(reads)), tuple(sorted(writes)), tuple(sorted(ddl))


def _transaction_kind(expression: Any) -> str | None:
    if exp is None:
        return None
    if isinstance(expression, exp.Transaction):
        return "begin"
    if isinstance(expression, exp.Commit):
        return "commit"
    if isinstance(expression, exp.Rollback):
        return "rollback"
    return None


class _Visitor(ast.NodeVisitor):
    def __init__(self, source: _Source) -> None:
        self.source = source
        self.symbol_stack: list[str] = [source.module_id.rsplit(".", 1)[-1]]
        self.interactions: list[StaticSqlInteraction] = []
        self.transaction_events: list[StaticTransactionEvent] = []
        self.dynamic_sites: list[str] = []
        self.parse_errors: list[str] = []
        self.literal_sites = 0
        self.parsed_sites = 0
        self.statement_count = 0

    @property
    def symbol(self) -> str:
        module = self.source.module_id.rsplit(".", 1)[-1]
        suffix = ".".join(self.symbol_stack[1:])
        return module if not suffix else f"{module}.{suffix}"

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.symbol_stack.append(node.name)
        self.generic_visit(node)
        self.symbol_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.symbol_stack.append(node.name)
        self.generic_visit(node)
        self.symbol_stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def _event(
        self,
        node: ast.Call,
        kind: Literal["begin", "commit", "rollback", "savepoint", "release"],
        evidence_kind: Literal["method_call", "literal_sql"],
    ) -> None:
        logical, owners, stores = _state_binding(self.source.module_id)
        payload = {
            "version": self.source.version_id,
            "path": self.source.path,
            "symbol": self.symbol,
            "line": node.lineno,
            "kind": kind,
            "evidence": evidence_kind,
        }
        self.transaction_events.append(
            StaticTransactionEvent(
                event_id=analysis_identity("static-transaction-event-v1", payload),
                version_id=self.source.version_id,
                path=self.source.path,
                module_id=self.source.module_id,
                symbol=self.symbol,
                line=node.lineno,
                event_kind=kind,
                evidence_kind=evidence_kind,
                connection_expression=_connection_expression(node.func),
                logical_owner_ids=logical,
                state_owner_ids=owners,
                state_store_ids=stores,
            )
        )

    def visit_Call(self, node: ast.Call) -> None:
        name = _attribute_name(node.func)
        if name in {"commit", "rollback"}:
            self._event(node, cast(Any, name), "method_call")
        if name not in {"execute", "executemany", "executescript"}:
            self.generic_visit(node)
            return
        location = f"{self.source.path}:{node.lineno}:{self.symbol}"
        sql = _literal_string(node.args[0]) if node.args else None
        if sql is None:
            self.dynamic_sites.append(location)
            self.generic_visit(node)
            return
        self.literal_sites += 1
        if sqlglot is None:
            self.parse_errors.append(f"{location}:sqlglot_unavailable")
            self.generic_visit(node)
            return
        try:
            expressions = tuple(sqlglot.parse(sql, read="sqlite"))
        except Exception as error:  # SQLGlot exposes several parse/token error types.
            self.parse_errors.append(f"{location}:{type(error).__name__}")
            self.generic_visit(node)
            return
        self.parsed_sites += 1
        self.statement_count += len(expressions)
        if self.statement_count > CODE_STATE_INTERACTION_MAX_STATEMENTS:
            raise ValueError("static SQL statement hard limit exceeded")
        logical, owners, stores = _state_binding(self.source.module_id)
        read_tables: set[str] = set()
        write_tables: set[str] = set()
        ddl_tables: set[str] = set()
        operations: list[str] = []
        for expression in expressions:
            transaction = _transaction_kind(expression)
            if transaction is not None:
                self._event(node, cast(Any, transaction), "literal_sql")
                continue
            operation, reads, writes, ddl = _classify_sql(expression)
            operations.append(operation)
            read_tables.update(reads)
            write_tables.update(writes)
            ddl_tables.update(ddl)
        if operations or read_tables or write_tables or ddl_tables:
            payload = {
                "version": self.source.version_id,
                "path": self.source.path,
                "symbol": self.symbol,
                "line": node.lineno,
                "sql_digest": fingerprint_text(sql).xxh3_128,
            }
            self.interactions.append(
                StaticSqlInteraction(
                    interaction_id=analysis_identity("static-sql-interaction-v1", payload),
                    version_id=self.source.version_id,
                    path=self.source.path,
                    module_id=self.source.module_id,
                    symbol=self.symbol,
                    line=node.lineno,
                    call_name=cast(Any, name),
                    operation="+".join(operations) or "transaction_control",
                    read_tables=tuple(sorted(read_tables)),
                    write_tables=tuple(sorted(write_tables)),
                    ddl_tables=tuple(sorted(ddl_tables)),
                    sql_digest="xxh3_128:" + fingerprint_text(sql).xxh3_128,
                    statement_count=len(expressions),
                    logical_owner_ids=logical,
                    state_owner_ids=owners,
                    state_store_ids=stores,
                )
            )
        self.generic_visit(node)


def _read_sources(connection: sqlite3.Connection) -> tuple[int, str, tuple[_Source, ...], int]:
    run = connection.execute(
        """SELECT analysis_run_id,processing_signature FROM analysis_runs
        WHERE status='completed' ORDER BY analysis_run_id DESC LIMIT 1"""
    ).fetchone()
    if run is None:
        raise ValueError("completed_code_publication_missing")
    rows = connection.execute(
        """SELECT version_id,path_observed,text_zlib,text_xxh3_128,encoding,text_truncated,
        analysis_status,processing_signature FROM file_versions
        WHERE invalidated_ns IS NULL AND language='python' ORDER BY path_observed,version_id"""
    ).fetchall()
    sources: list[_Source] = []
    missing = 0
    for row in rows:
        if (
            row["text_zlib"] is None
            or int(row["text_truncated"]) != 0
            or str(row["analysis_status"]) != "complete"
            or str(row["processing_signature"]) != str(run["processing_signature"])
        ):
            missing += 1
            continue
        encoding = str(row["encoding"] or "utf-8")
        text = zlib.decompress(bytes(row["text_zlib"])).decode(encoding)
        digest = fingerprint_text(text).xxh3_128
        if digest != str(row["text_xxh3_128"]):
            raise ValueError("source_text_digest_mismatch")
        path = str(row["path_observed"])
        sources.append(_Source(int(row["version_id"]), path, _module_id(path), text, digest))
    return int(run["analysis_run_id"]), str(run["processing_signature"]), tuple(sources), missing


def _workflow_observations(
    interactions: Sequence[StaticSqlInteraction],
    transactions: Sequence[StaticTransactionEvent],
    dynamic_sites: Sequence[str],
    resolved_symbols: set[str],
) -> tuple[WorkflowBoundarySqlObservation, ...]:
    result: list[WorkflowBoundarySqlObservation] = []
    for binding in TEXT_DERIVATION_IMPLEMENTATION_BINDING.boundaries:
        contract = TEXT_DERIVATION_WORKFLOW.boundary(binding.boundary_id)
        bound = set(binding.qualified_symbols)
        selected = tuple(item for item in interactions if item.symbol in bound)
        selected_events = tuple(item for item in transactions if item.symbol in bound)
        reads = {table for item in selected for table in item.read_tables}
        writes = {table for item in selected for table in item.write_tables}
        ddl = {table for item in selected for table in item.ddl_tables}
        missing_reads = set(contract.required_read_tables) - reads
        missing_writes = set(contract.required_write_tables) - writes
        conditional = set(contract.conditional_write_tables) & writes
        expected_writes = set(contract.required_write_tables) | set(
            contract.conditional_write_tables
        )
        unexpected = writes - expected_writes
        dynamic = sum(
            any(site.endswith(f":{symbol}") for symbol in bound) for site in dynamic_sites
        )
        resolved = bound & resolved_symbols
        status: Literal["observed", "partial", "abstained"]
        if not resolved:
            status = "abstained"
        elif dynamic or resolved != bound or missing_reads or missing_writes:
            status = "partial"
        else:
            status = "observed"
        result.append(
            WorkflowBoundarySqlObservation(
                boundary_id=binding.boundary_id,
                bound_symbols=tuple(sorted(bound)),
                resolved_symbols=tuple(sorted(resolved)),
                observed_read_tables=tuple(sorted(reads)),
                observed_write_tables=tuple(sorted(writes)),
                observed_ddl_tables=tuple(sorted(ddl)),
                missing_required_read_tables=tuple(sorted(missing_reads)),
                missing_required_write_tables=tuple(sorted(missing_writes)),
                observed_conditional_write_tables=tuple(sorted(conditional)),
                unexpected_write_tables=tuple(sorted(unexpected)),
                transaction_event_kinds=tuple(
                    sorted({item.event_kind for item in selected_events})
                ),
                unresolved_dynamic_sql_sites=dynamic,
                status=status,
            )
        )
    return tuple(result)


def _abstained(database: Path, reason: str) -> CodeStateInteractionAnalysis:
    payload = {"database": str(database), "reason": reason, "policy": CODE_STATE_INTERACTION_POLICY}
    return CodeStateInteractionAnalysis(
        analysis_id=analysis_identity("code-state-interaction-v1", payload),
        status="abstained",
        reason=reason,
        policy_id=CODE_STATE_INTERACTION_POLICY,
        database=str(database),
        analysis_run_id=None,
        source_processing_signature=None,
        source_schema_version=None,
        source_files=0,
        source_files_with_text=0,
        source_files_without_text=0,
        literal_sql_sites=0,
        parsed_sql_sites=0,
        dynamic_sql_sites=0,
        parse_error_sites=0,
        statement_count=0,
        interactions_count=0,
        interactions=(),
        interactions_truncated=False,
        transaction_events_count=0,
        transaction_events=(),
        transaction_events_truncated=False,
        dynamic_sql_examples=(),
        parse_error_examples=(),
        examples_truncated=False,
        workflow_boundaries=(),
        sql_parser="sqlglot" if sqlglot is not None else "unavailable",
        sql_parser_version=None if sqlglot is None else str(sqlglot.__version__),
        logical_owner_contract_schema=LOGICAL_OWNER_CONTRACT_SCHEMA,
        logical_owner_registry_fingerprint=logical_owner_registry_fingerprint(),
        state_store_registry_schema=STATE_STORE_REGISTRY_SCHEMA,
        workflow_contract_schema=TEXT_DERIVATION_WORKFLOW.schema,
        workflow_binding_schema=DURABLE_WORKFLOW_BINDING_SCHEMA,
    )


def analyze_code_state_interactions(state_directory: Path) -> CodeStateInteractionAnalysis:
    """Analyze the latest exact Code publication without touching live state."""

    database = Path(state_directory) / "code.sqlite3"
    if sqlglot is None:
        return _abstained(database, "sqlglot_unavailable")
    if not database.is_file():
        return _abstained(database, "code_state_missing")
    try:
        with immutable_sqlite_database(database) as connection:
            validate_code_schema(connection)
            schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if schema_version != CODE_SCHEMA_VERSION:
                raise ValueError("code_schema_version_incompatible")
            run_id, signature, sources, missing = _read_sources(connection)
            resolved_symbols = {
                str(row[0])
                for row in connection.execute(
                    """SELECT s.qualified_name FROM symbols s
                    JOIN file_versions v USING(version_id)
                    WHERE v.invalidated_ns IS NULL AND s.confirmed=1"""
                )
            }
    except (ImmutableSQLiteUnavailable, OSError, sqlite3.Error, ValueError, zlib.error) as error:
        return _abstained(
            database, f"state_interaction_source_unavailable:{type(error).__name__}:{error}"
        )

    visitors: list[_Visitor] = []
    syntax_errors: list[str] = []
    try:
        sqlglot_logger = logging.getLogger("sqlglot")
        prior_level = sqlglot_logger.level
        sqlglot_logger.setLevel(logging.ERROR)
        try:
            for source in sources:
                try:
                    tree = ast.parse(source.text, filename=source.path)
                except (SyntaxError, ValueError, MemoryError) as error:
                    syntax_errors.append(f"{source.path}:{type(error).__name__}")
                    continue
                visitor = _Visitor(source)
                visitor.visit(tree)
                visitors.append(visitor)
        finally:
            sqlglot_logger.setLevel(prior_level)
    except (ValueError, MemoryError, RecursionError) as error:
        return _abstained(
            database, f"state_interaction_analysis_failed:{type(error).__name__}:{error}"
        )

    interactions = tuple(
        sorted(
            (item for visitor in visitors for item in visitor.interactions),
            key=lambda item: (item.path, item.line, item.interaction_id),
        )
    )
    transactions = tuple(
        sorted(
            (item for visitor in visitors for item in visitor.transaction_events),
            key=lambda item: (item.path, item.line, item.event_id),
        )
    )
    dynamic = tuple(sorted(item for visitor in visitors for item in visitor.dynamic_sites))
    parse_errors = tuple(
        sorted((*syntax_errors, *(item for visitor in visitors for item in visitor.parse_errors)))
    )
    literal_sites = sum(item.literal_sites for item in visitors)
    parsed_sites = sum(item.parsed_sites for item in visitors)
    statement_count = sum(item.statement_count for item in visitors)
    workflow = _workflow_observations(interactions, transactions, dynamic, resolved_symbols)
    status: Literal["ready", "partial", "abstained"] = (
        "partial" if missing or syntax_errors or dynamic or parse_errors else "ready"
    )
    payload = {
        "run": run_id,
        "signature": signature,
        "sources": tuple((item.version_id, item.text_digest) for item in sources),
        "interactions": tuple(item.interaction_id for item in interactions),
        "transactions": tuple(item.event_id for item in transactions),
        "dynamic": dynamic,
        "parse_errors": parse_errors,
        "workflow": tuple(asdict(item) for item in workflow),
        "policy": CODE_STATE_INTERACTION_POLICY,
        "sqlglot": str(sqlglot.__version__),
    }
    return CodeStateInteractionAnalysis(
        analysis_id=analysis_identity("code-state-interaction-v1", payload),
        status=status,
        reason=None,
        policy_id=CODE_STATE_INTERACTION_POLICY,
        database=str(database),
        analysis_run_id=run_id,
        source_processing_signature=signature,
        source_schema_version=CODE_SCHEMA_VERSION,
        source_files=len(sources) + missing,
        source_files_with_text=len(sources),
        source_files_without_text=missing,
        literal_sql_sites=literal_sites,
        parsed_sql_sites=parsed_sites,
        dynamic_sql_sites=len(dynamic),
        parse_error_sites=len(parse_errors),
        statement_count=statement_count,
        interactions_count=len(interactions),
        interactions=interactions[:CODE_STATE_INTERACTION_EXAMPLE_LIMIT],
        interactions_truncated=len(interactions) > CODE_STATE_INTERACTION_EXAMPLE_LIMIT,
        transaction_events_count=len(transactions),
        transaction_events=transactions[:CODE_STATE_INTERACTION_EXAMPLE_LIMIT],
        transaction_events_truncated=len(transactions) > CODE_STATE_INTERACTION_EXAMPLE_LIMIT,
        dynamic_sql_examples=dynamic[:CODE_STATE_INTERACTION_EXAMPLE_LIMIT],
        parse_error_examples=parse_errors[:CODE_STATE_INTERACTION_EXAMPLE_LIMIT],
        examples_truncated=(
            len(dynamic) > CODE_STATE_INTERACTION_EXAMPLE_LIMIT
            or len(parse_errors) > CODE_STATE_INTERACTION_EXAMPLE_LIMIT
        ),
        workflow_boundaries=workflow,
        sql_parser="sqlglot",
        sql_parser_version=str(sqlglot.__version__),
        logical_owner_contract_schema=LOGICAL_OWNER_CONTRACT_SCHEMA,
        logical_owner_registry_fingerprint=logical_owner_registry_fingerprint(),
        state_store_registry_schema=STATE_STORE_REGISTRY_SCHEMA,
        workflow_contract_schema=TEXT_DERIVATION_WORKFLOW.schema,
        workflow_binding_schema=DURABLE_WORKFLOW_BINDING_SCHEMA,
    )


def _subject(
    analysis: CodeStateInteractionAnalysis,
    *,
    snapshot_id: str,
    snapshot_freshness: Literal["current", "publication_only", "unknown"],
    kind: Literal["project", "workflow"],
) -> AnalysisSubjectRef:
    return AnalysisSubjectRef(
        subject_kind=kind,
        subject_key=(
            f"state-interactions:{analysis.analysis_id}"
            if kind == "project"
            else f"workflow:{TEXT_DERIVATION_WORKFLOW.workflow_id}:{analysis.analysis_id}"
        ),
        display_name=(
            "NeoCortex static SQL and transaction interaction projection"
            if kind == "project"
            else TEXT_DERIVATION_WORKFLOW.workflow_id
        ),
        source_owner_id="code",
        snapshot_id=snapshot_id,
        snapshot_freshness=snapshot_freshness,
        revision_id=analysis.source_processing_signature,
    )


def _evidence(
    analysis: CodeStateInteractionAnalysis,
    subject: AnalysisSubjectRef,
    *,
    workflow: bool,
) -> tuple[AnalysisEvidenceRef, ...]:
    if analysis.status == "abstained":
        return ()
    projection = analysis_identity(
        "state-interaction-evidence-projection-v1",
        {
            "analysis": analysis.analysis_id,
            "workflow": workflow,
            "interactions": tuple(item.interaction_id for item in analysis.interactions),
            "transactions": tuple(item.event_id for item in analysis.transaction_events),
            "boundaries": tuple(asdict(item) for item in analysis.workflow_boundaries),
        },
    )
    if workflow:
        contract_payload = {
            "workflow": TEXT_DERIVATION_WORKFLOW.as_payload(),
            "binding": TEXT_DERIVATION_IMPLEMENTATION_BINDING.as_payload(),
        }
        contract_digest = analysis_identity("workflow-binding-evidence-v1", contract_payload)
        contract = AnalysisEvidenceRef(
            evidence_id=analysis_identity(
                "workflow-binding-ref-v1",
                {"subject": subject.subject_key, "digest": contract_digest},
            ),
            subject_key=subject.subject_key,
            role="supporting",
            evidence_kind="contract",
            source_owner_id="code",
            producer_id="durable-workflow-contract-registry",
            producer_version=DURABLE_WORKFLOW_BINDING_SCHEMA,
            source_schema=DURABLE_WORKFLOW_BINDING_SCHEMA,
            source_record_kind="durable_workflow_implementation_binding",
            source_record_id=TEXT_DERIVATION_WORKFLOW.workflow_id,
            source_projection_digest=contract_digest,
            snapshot_id=subject.snapshot_id,
            revision_id=subject.revision_id,
            facts=(
                AnalysisFact(
                    "declared_boundaries", len(TEXT_DERIVATION_WORKFLOW.boundaries), "count"
                ),
                AnalysisFact(
                    "bound_symbols",
                    sum(
                        len(item.qualified_symbols)
                        for item in TEXT_DERIVATION_IMPLEMENTATION_BINDING.boundaries
                    ),
                    "count",
                ),
                AnalysisFact("workflow_id", TEXT_DERIVATION_WORKFLOW.workflow_id),
            ),
            completeness="complete",
            bounded=False,
            truncated=False,
            resolver_id="durable-workflow-implementation-binding-resolver",
            resolver_version="v1",
            limitations=("binding_is_source_declared_and_snapshot_resolved_separately",),
        )
        relation_facts = (
            AnalysisFact("workflow_boundaries", len(analysis.workflow_boundaries), "count"),
            AnalysisFact(
                "observed_boundaries",
                sum(item.status == "observed" for item in analysis.workflow_boundaries),
                "count",
            ),
            AnalysisFact(
                "partial_boundaries",
                sum(item.status == "partial" for item in analysis.workflow_boundaries),
                "count",
            ),
            AnalysisFact(
                "missing_required_tables",
                sum(
                    len(item.missing_required_read_tables) + len(item.missing_required_write_tables)
                    for item in analysis.workflow_boundaries
                ),
                "count",
            ),
            AnalysisFact(
                "boundary_projection_json",
                json.dumps(
                    tuple(asdict(item) for item in analysis.workflow_boundaries),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            ),
        )
    else:
        contract = None
        relation_facts = (
            AnalysisFact("source_files", analysis.source_files, "count"),
            AnalysisFact("source_files_with_text", analysis.source_files_with_text, "count"),
            AnalysisFact("literal_sql_sites", analysis.literal_sql_sites, "count"),
            AnalysisFact("parsed_sql_sites", analysis.parsed_sql_sites, "count"),
            AnalysisFact("dynamic_sql_sites", analysis.dynamic_sql_sites, "count"),
            AnalysisFact("parse_error_sites", analysis.parse_error_sites, "count"),
            AnalysisFact("statement_count", analysis.statement_count, "count"),
            AnalysisFact("transaction_events", analysis.transaction_events_count, "count"),
            AnalysisFact("sql_parser_version", analysis.sql_parser_version),
        )
    relation = AnalysisEvidenceRef(
        evidence_id=analysis_identity(
            "state-interaction-relation-ref-v1",
            {"subject": subject.subject_key, "projection": projection},
        ),
        subject_key=subject.subject_key,
        role="supporting",
        evidence_kind="internal_relation",
        source_owner_id="code",
        producer_id="code-state-interaction-analyzer",
        producer_version=CODE_STATE_INTERACTION_SCHEMA,
        source_schema=CODE_STATE_INTERACTION_SCHEMA,
        source_record_kind=(
            "bound_workflow_sql_projection" if workflow else "static_sql_interaction_projection"
        ),
        source_record_id=str(analysis.analysis_run_id),
        source_projection_digest=projection,
        snapshot_id=subject.snapshot_id,
        revision_id=subject.revision_id,
        facts=relation_facts,
        completeness="complete" if analysis.status == "ready" else "partial",
        bounded=True,
        truncated=(
            analysis.interactions_truncated
            or analysis.transaction_events_truncated
            or analysis.examples_truncated
        ),
        resolver_id="code-state-interaction-projection-resolver",
        resolver_version="v1",
        limitations=_LIMITATIONS[:5],
    )
    return (relation,) if contract is None else (contract, relation)


def _evaluation(
    spec: AnalysisQuestionSpec,
    analysis: CodeStateInteractionAnalysis,
    subject: AnalysisSubjectRef,
    *,
    rank: int,
    workflow: bool,
) -> AnalysisQuestionEvaluation:
    evidence = _evidence(analysis, subject, workflow=workflow)
    ready = bool(evidence)
    evidence_ids = tuple(item.evidence_id for item in evidence)
    if workflow:
        requirements = (
            AnalysisRequirementEvaluation(
                "workflow_implementation_binding",
                "satisfied" if ready else "missing",
                evidence_ids[:1],
                "workflow_binding_resolved" if ready else analysis.reason,
            ),
            AnalysisRequirementEvaluation(
                "bound_symbol_sql_projection",
                "satisfied" if ready else "missing",
                evidence_ids[1:] if ready else (),
                "bound_symbol_sql_projection_resolved" if ready else analysis.reason,
            ),
            AnalysisRequirementEvaluation(
                "runtime_transaction_order_observed",
                "missing",
                (),
                "runtime_transaction_order_not_recorded",
            ),
            AnalysisRequirementEvaluation(
                "indirect_helper_and_dynamic_sql_counterevidence_evaluated",
                "not_evaluated",
                (),
                "indirect_helper_and_dynamic_sql_counterevidence_not_evaluated",
            ),
            AnalysisRequirementEvaluation(
                "workflow_fault_boundary_experiment_result",
                "missing",
                (),
                "workflow_fault_boundary_experiment_not_recorded",
            ),
        )
    else:
        requirements = (
            AnalysisRequirementEvaluation(
                "literal_sql_projection",
                "satisfied" if ready else "missing",
                evidence_ids,
                "literal_sql_projection_resolved" if ready else analysis.reason,
            ),
            AnalysisRequirementEvaluation(
                "dynamic_sql_and_runtime_calls_resolved",
                "missing",
                (),
                "dynamic_sql_and_runtime_calls_not_resolved",
            ),
            AnalysisRequirementEvaluation(
                "state_store_mapping_counterevidence_evaluated",
                "not_evaluated",
                (),
                "state_store_mapping_counterevidence_not_evaluated",
            ),
            AnalysisRequirementEvaluation(
                "bounded_sql_runtime_trace_result",
                "missing",
                (),
                "bounded_sql_runtime_trace_not_recorded",
            ),
        )
    result = AnalysisQuestionEvaluation(
        evaluation_id=analysis_identity(
            "code-state-interaction-question-evaluation-v1",
            {
                "analysis": analysis.analysis_id,
                "question": spec.question_id,
                "subject": subject.subject_key,
                "requirements": tuple(asdict(item) for item in requirements),
            },
        ),
        question_id=spec.question_id,
        question_version=spec.version,
        question_spec_fingerprint=analysis_question_spec_fingerprint(spec),
        rank=rank,
        subject=subject,
        evidence=evidence,
        requirements=requirements,
        observation_status="confirmed" if ready else "abstained",
        inference_status="abstained",
        inferences=(),
        hypotheses=spec.hypotheses,
        question_readiness="ready" if ready else "abstained",
        decision_readiness="experiment_required" if ready else "abstained",
        decision=None,
        decision_reason="decision_evidence_incomplete" if ready else "question_evidence_incomplete",
        counterevidence_status="not_evaluated",
        next_action_ids=(tuple(item.action_id for item in spec.next_actions) if ready else ()),
        limitations=(*_LIMITATIONS, *((analysis.reason,) if analysis.reason else ())),
    )
    validate_analysis_question_evaluation(spec, result)
    return result


def state_interaction_questions(
    analysis: CodeStateInteractionAnalysis,
    *,
    snapshot_id: str,
    snapshot_freshness: Literal["current", "publication_only", "unknown"],
    rank_offset: int,
) -> tuple[tuple[AnalysisQuestionSpec, ...], tuple[AnalysisQuestionEvaluation, ...]]:
    if isinstance(rank_offset, bool) or not isinstance(rank_offset, int) or rank_offset < 0:
        raise ValueError("state interaction rank offset must be non-negative")
    if snapshot_freshness not in {"current", "publication_only", "unknown"}:
        raise ValueError("state interaction snapshot freshness is invalid")
    specs = (SQL_INTERACTION_QUESTION, WORKFLOW_SQL_QUESTION)
    project = _subject(
        analysis,
        snapshot_id=snapshot_id,
        snapshot_freshness=snapshot_freshness,
        kind="project",
    )
    workflow = _subject(
        analysis,
        snapshot_id=snapshot_id,
        snapshot_freshness=snapshot_freshness,
        kind="workflow",
    )
    return specs, (
        _evaluation(specs[0], analysis, project, rank=rank_offset + 1, workflow=False),
        _evaluation(specs[1], analysis, workflow, rank=rank_offset + 2, workflow=True),
    )


def parse_code_state_interaction_payload(
    payload: Mapping[str, object],
) -> CodeStateInteractionAnalysis:
    if not isinstance(payload, Mapping) or payload.get("schema") != CODE_STATE_INTERACTION_SCHEMA:
        raise ValueError("state interaction payload schema is invalid")
    values = dict(payload)
    values.pop("schema", None)
    if set(values) != {item.name for item in fields(CodeStateInteractionAnalysis)}:
        raise ValueError("state interaction payload fields are invalid")
    for name, model in (
        ("interactions", StaticSqlInteraction),
        ("transaction_events", StaticTransactionEvent),
        ("workflow_boundaries", WorkflowBoundarySqlObservation),
    ):
        raw = values[name]
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
            raise ValueError(f"state interaction {name} are invalid")
        expected = {item.name for item in fields(model)}
        parsed = []
        for item in raw:
            if not isinstance(item, Mapping) or set(item) != expected:
                raise ValueError(f"state interaction {name} entry is invalid")
            entry = dict(item)
            for key, value in tuple(entry.items()):
                if key.endswith(("_tables", "_ids")) or key in {
                    "bound_symbols",
                    "resolved_symbols",
                    "transaction_event_kinds",
                }:
                    entry[key] = tuple(value)
            parsed.append(model(**entry))
        values[name] = tuple(parsed)
    for name in ("dynamic_sql_examples", "parse_error_examples", "limitations"):
        values[name] = tuple(values[name])
    return CodeStateInteractionAnalysis(**values)


__all__ = [
    "CODE_STATE_INTERACTION_EXAMPLE_LIMIT",
    "CODE_STATE_INTERACTION_POLICY",
    "CODE_STATE_INTERACTION_SCHEMA",
    "SQL_INTERACTION_QUESTION",
    "WORKFLOW_SQL_QUESTION",
    "CodeStateInteractionAnalysis",
    "StaticSqlInteraction",
    "StaticTransactionEvent",
    "WorkflowBoundarySqlObservation",
    "analyze_code_state_interactions",
    "parse_code_state_interaction_payload",
    "state_interaction_questions",
]
