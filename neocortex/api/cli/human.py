"""Concise canonical CLI for consultation and explicit state maintenance."""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TextIO

from ..read_api import (
    ReadScope,
    asset_health_payload,
    code_search_payload,
    context_payload,
    lineage_payload,
    search_payload,
    status_payload,
)
from neocortex.runtime.config.app_paths import default_state_directory


HUMAN_COMMANDS = frozenset(
    {
        "help",
        "status",
        "search",
        "ask",
        "inspect",
        "review",
        "knowledge",
        "databases",
        "database",
        "agent",
    }
)

_DATABASE_STORE_CHOICES = (
    "inventory",
    "framework",
    "catalog",
    "pdf",
    "docx",
    "office",
    "audio",
    "video",
    "image",
    "semantic",
    "code",
    "archive",
    "text",
)


def handles_human_command(arguments: Sequence[str]) -> bool:
    return bool(arguments) and arguments[0] in HUMAN_COMMANDS


def _console_text(value: str, stream: object) -> str:
    encoding = getattr(stream, "encoding", None)
    if not encoding:
        return value
    try:
        value.encode(encoding)
    except UnicodeEncodeError:
        return value.encode(encoding, errors="backslashreplace").decode(encoding)
    except LookupError:  # pragma: no cover - custom stream defense
        return value
    return value


def _print(value: str = "", *, file: TextIO | None = None) -> None:
    stream = sys.stdout if file is None else file
    print(_console_text(value, stream), file=stream)


def _json(payload: Mapping[str, object]) -> None:
    _print(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _exit_code(payload: Mapping[str, object]) -> int:
    value = payload.get("exit_code")
    return value if isinstance(value, int) and not isinstance(value, bool) else 1


def _add_scope(parser: argparse.ArgumentParser, *, default: ReadScope) -> None:
    parser.add_argument(
        "--scope",
        choices=tuple(scope.value for scope in ReadScope),
        default=default.value,
        help=(
            "personal consulta el estado publicado; framework es un alias de "
            "lectura del mismo owner; all agrupa sin mezclar scores"
        ),
    )


def _add_query_options(
    parser: argparse.ArgumentParser,
    *,
    default_scope: ReadScope = ReadScope.ALL,
) -> None:
    parser.add_argument("query", metavar="CONSULTA")
    _add_scope(parser, default=default_scope)
    parser.add_argument("--limit", type=int, default=10, metavar="N")
    parser.add_argument(
        "--mode",
        choices=("evidence", "discovery"),
        default="evidence",
        help="evidence prioriza citas concretas; discovery prioriza recursos",
    )
    parser.add_argument("--history", action="store_true", help="incluye revisiones históricas")
    parser.add_argument("--json", action="store_true", help="emite el contrato JSON completo")


def build_human_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="Neocortex",
        description=(
            "Consulta local, trazable y de solo lectura; el borrado de bases "
            "requiere una acción y confirmación explícitas. Los comandos heredados "
            "con flags siguen disponibles."
        ),
        allow_abbrev=False,
    )
    commands = parser.add_subparsers(dest="command", metavar="COMANDO")

    commands.add_parser("help", help="muestra esta guía breve", allow_abbrev=False)

    status = commands.add_parser(
        "status",
        help="resume cobertura y compatibilidad del estado publicado",
        allow_abbrev=False,
    )
    _add_scope(status, default=ReadScope.ALL)
    status.add_argument("--json", action="store_true")

    search = commands.add_parser(
        "search",
        help="busca evidencia y explica de dónde proviene",
        allow_abbrev=False,
    )
    _add_query_options(search)

    ask = commands.add_parser(
        "ask",
        help="prepara contexto citado para responder sin inventar",
        allow_abbrev=False,
    )
    _add_query_options(ask)
    ask.add_argument(
        "--characters",
        type=int,
        default=12_000,
        metavar="N",
        help="presupuesto máximo de contexto por scope",
    )

    inspect = commands.add_parser(
        "inspect",
        help="inspecciona proyecciones estructuradas",
        allow_abbrev=False,
    )
    inspect_commands = inspect.add_subparsers(dest="inspect_command", metavar="TIPO")
    inspect_code = inspect_commands.add_parser(
        "code",
        help="busca símbolos, texto y relaciones en Code publicado",
        allow_abbrev=False,
    )
    inspect_code.add_argument("query", metavar="CONSULTA")
    _add_scope(inspect_code, default=ReadScope.PERSONAL)
    inspect_code.add_argument("--limit", type=int, default=10, metavar="N")
    inspect_code.add_argument(
        "--mode",
        action="append",
        dest="modes",
        help="canal Code; puede repetirse (por defecto: hybrid)",
    )
    inspect_code.add_argument("--json", action="store_true")
    inspect_lineage = inspect_commands.add_parser(
        "lineage",
        help="explica cómo se produjo una revisión o materialización",
        allow_abbrev=False,
    )
    inspect_lineage.add_argument("identifier", metavar="IDENTIFICADOR")
    _add_scope(inspect_lineage, default=ReadScope.PERSONAL)
    inspect_lineage.add_argument("--json", action="store_true")

    review = commands.add_parser(
        "review",
        help="revisa propuestas conservadoras sin mutar archivos",
        allow_abbrev=False,
    )
    review_commands = review.add_subparsers(dest="review_command", metavar="TIPO")
    review_value = review_commands.add_parser(
        "value",
        help="explica candidatos de valor bajo, duplicados y desconocidos",
        allow_abbrev=False,
    )
    _add_scope(review_value, default=ReadScope.PERSONAL)
    review_value.add_argument("--limit", type=int, default=50, metavar="N")
    review_value.add_argument(
        "--refresh",
        action="store_true",
        help="publica una página acotada de tareas durables; no muta archivos",
    )
    review_value.add_argument("--json", action="store_true")
    review_task = review_commands.add_parser(
        "task",
        help="inspecciona o decide una tarea durable mediante CAS",
        allow_abbrev=False,
    )
    review_task_commands = review_task.add_subparsers(dest="review_task_command", metavar="ACCIÓN")
    review_task_show = review_task_commands.add_parser(
        "show", help="muestra una tarea durable exacta", allow_abbrev=False
    )
    review_task_show.add_argument("task_id", metavar="TASK_ID")
    _add_scope(review_task_show, default=ReadScope.PERSONAL)
    review_task_show.add_argument("--json", action="store_true")
    review_task_history = review_task_commands.add_parser(
        "history", help="muestra el historial append-only de una tarea", allow_abbrev=False
    )
    review_task_history.add_argument("task_id", metavar="TASK_ID")
    _add_scope(review_task_history, default=ReadScope.PERSONAL)
    review_task_history.add_argument("--json", action="store_true")
    review_task_claim = review_task_commands.add_parser(
        "claim", help="reclama una tarea abierta mediante CAS", allow_abbrev=False
    )
    review_task_claim.add_argument("task_id", metavar="TASK_ID")
    _add_scope(review_task_claim, default=ReadScope.PERSONAL)
    review_task_claim.add_argument("--expected-event-id", required=True, metavar="EVENT_ID")
    review_task_claim.add_argument("--actor", required=True, metavar="ACTOR")
    review_task_claim.add_argument("--note", metavar="NOTA")
    review_task_claim.add_argument("--json", action="store_true")
    review_task_decide = review_task_commands.add_parser(
        "decide", help="resuelve o descarta una tarea durable", allow_abbrev=False
    )
    review_task_decide.add_argument("task_id", metavar="TASK_ID")
    _add_scope(review_task_decide, default=ReadScope.PERSONAL)
    review_task_decide.add_argument("--expected-event-id", required=True, metavar="EVENT_ID")
    review_task_decide.add_argument("--decision", required=True, choices=("resolved", "dismissed"))
    review_task_decide.add_argument(
        "--decision-scope",
        required=True,
        choices=("until-source-change", "until-policy-change", "permanent"),
    )
    review_task_decide.add_argument("--actor", required=True, metavar="ACTOR")
    review_task_decide.add_argument("--note", metavar="NOTA")
    review_task_decide.add_argument("--json", action="store_true")

    knowledge = commands.add_parser(
        "knowledge",
        help="explica la salud causal de un activo publicado",
        allow_abbrev=False,
    )
    knowledge_commands = knowledge.add_subparsers(
        dest="knowledge_command",
        metavar="ACCIÓN",
    )
    knowledge_health = knowledge_commands.add_parser(
        "health",
        help="traza una identidad estable entre owners sin leer el corpus",
        allow_abbrev=False,
    )
    knowledge_health.add_argument("resource_id", metavar="RESOURCE_ID")
    _add_scope(knowledge_health, default=ReadScope.ALL)
    knowledge_health.add_argument("--json", action="store_true")

    databases = commands.add_parser(
        "databases",
        aliases=("database",),
        help="previsualiza o elimina las bases SQLite propias de NeoCortex",
        allow_abbrev=False,
    )
    database_commands = databases.add_subparsers(
        dest="database_command",
        metavar="ACCIÓN",
    )
    purge = database_commands.add_parser(
        "purge",
        help="borrar bases sólo con backup y confirmación explícita",
        allow_abbrev=False,
    )
    purge.add_argument(
        "--state-directory",
        type=Path,
        default=default_state_directory(),
        help="directorio de estado; por defecto, el estado Linux canónico",
    )
    purge.add_argument(
        "--store",
        action="append",
        choices=_DATABASE_STORE_CHOICES,
        metavar="OWNER",
        help="owner a borrar; puede repetirse, por defecto todos los owners",
    )
    purge.add_argument(
        "--backup-directory",
        type=Path,
        help="directorio nuevo fuera del estado donde conservar el backup verificado",
    )
    purge.add_argument(
        "--apply",
        action="store_true",
        help="ejecuta el borrado; sin esta opción sólo muestra la vista previa",
    )
    purge.add_argument(
        "--confirm-database-purge",
        metavar="TOKEN",
        help="debe ser DELETE_DATABASES junto con --apply",
    )
    purge.add_argument("--json", action="store_true", help="emite el resultado JSON")

    agent = commands.add_parser(
        "agent",
        help="expone las mismas consultas mediante un protocolo local",
        allow_abbrev=False,
    )
    agent_commands = agent.add_subparsers(dest="agent_command", metavar="ACCIÓN")
    agent_commands.add_parser(
        "serve",
        help="inicia el servidor MCP exclusivamente por stdio",
        allow_abbrev=False,
    )
    return parser


def _entries(payload: Mapping[str, object]) -> list[dict[str, object]]:
    value = payload.get("scopes")
    if not isinstance(value, list):
        return []
    return [entry for entry in value if isinstance(entry, dict)]


def _scope_label(value: object) -> str:
    return {"personal": "Personal", "framework": "Framework", "all": "Todos"}.get(
        str(value), str(value)
    )


def _render_scope_error(entry: Mapping[str, object]) -> None:
    _print(
        f"{_scope_label(entry.get('scope'))}: no se pudo consultar "
        f"({entry.get('error_type', 'error')}: {entry.get('reason', 'sin detalle')})."
    )


def _run_status(args: argparse.Namespace) -> int:
    payload = status_payload(args.scope)
    if args.json:
        _json(payload)
        return _exit_code(payload)
    _print("Estado publicado de NeoCortex (solo lectura)")
    for entry in _entries(payload):
        snapshot = entry.get("snapshot")
        if not isinstance(snapshot, dict):
            _render_scope_error(entry)
            continue
        owners = snapshot.get("owners", [])
        owner_rows = (
            [owner for owner in owners if isinstance(owner, dict)]
            if isinstance(owners, list)
            else []
        )
        available = sum(owner.get("state") == "available" for owner in owner_rows)
        absent = sum(owner.get("state") == "absent" for owner in owner_rows)
        attention = [
            f"{owner.get('owner')}={owner.get('state')}"
            for owner in owner_rows
            if owner.get("state") not in {"available", "absent"}
        ]
        models = snapshot.get("active_models", [])
        model_count = len(models) if isinstance(models, list) else 0
        _print(
            f"{_scope_label(entry.get('scope'))}: {entry.get('status')} · "
            f"{available} fuentes disponibles, {absent} ausentes, {model_count} modelos activos."
        )
        if attention:
            _print("  Atención: " + ", ".join(attention))
        _print(f"  Snapshot: {snapshot.get('snapshot_id', '-')}")
    _print("No se creó, migró ni modificó estado.")
    return _exit_code(payload)


def _single_line(value: object, *, limit: int = 360) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _locator(evidence: Mapping[str, object]) -> str:
    parts: list[str] = []
    if evidence.get("page") is not None:
        parts.append(f"página {evidence['page']}")
    if evidence.get("sheet") is not None:
        parts.append(f"hoja {evidence['sheet']}")
    if evidence.get("cell_range") is not None:
        parts.append(f"celdas {evidence['cell_range']}")
    if evidence.get("start_line") is not None:
        end_line = evidence.get("end_line", evidence["start_line"])
        parts.append(f"líneas {evidence['start_line']}-{end_line}")
    start_ms = evidence.get("start_ms")
    if isinstance(start_ms, (int, float)) and not isinstance(start_ms, bool):
        parts.append(f"{start_ms / 1000:.1f} s")
    return ", ".join(parts) or "ubicación estructurada disponible"


def _render_hit(hit: Mapping[str, object], *, prefix: str) -> None:
    resource = hit.get("resource")
    evidence = hit.get("evidence")
    if not isinstance(resource, dict) or not isinstance(evidence, dict):
        return
    path = str(resource.get("current_path") or resource.get("resource_id") or "sin ruta")
    name = Path(path).name or path
    _print(f"{prefix} {name} · {_locator(evidence)}")
    _print(f"   Ruta: {path}")
    snippet = _single_line(evidence.get("snippet"))
    if snippet:
        _print(f"   Evidencia: {snippet}")
    reasons = hit.get("reasons")
    if isinstance(reasons, list) and reasons:
        _print("   Coincidió por: " + "; ".join(str(reason) for reason in reasons))
    _print(f"   ID: {evidence.get('evidence_id', '-')}")


def _run_search(args: argparse.Namespace) -> int:
    payload = search_payload(
        args.query,
        args.scope,
        limit=args.limit,
        mode=args.mode,
        include_history=args.history,
    )
    if args.json:
        _json(payload)
        return _exit_code(payload)
    _print(f"Resultados para: {payload['query']}")
    if payload["scope_requested"] == "all":
        _print("Los scopes se muestran por separado; sus scores no se mezclan.")
    for entry in _entries(payload):
        result = entry.get("result")
        if not isinstance(result, dict):
            _render_scope_error(entry)
            continue
        hits = result.get("hits", [])
        hit_rows = [hit for hit in hits if isinstance(hit, dict)] if isinstance(hits, list) else []
        complete = bool(result.get("complete"))
        window_full = bool(result.get("result_window_full"))
        coverage = "completa" if complete else "parcial"
        suffix = "; hay más candidatos fuera del top solicitado" if window_full else ""
        _print(
            f"\n{_scope_label(entry.get('scope'))}: {len(hit_rows)} resultados · "
            f"cobertura {coverage}{suffix}."
        )
        for index, hit in enumerate(hit_rows, start=1):
            _render_hit(hit, prefix=f"{index}.")
        if not hit_rows:
            _print("  No se encontró evidencia para esta consulta.")
        warnings = result.get("warnings")
        if isinstance(warnings, list) and warnings:
            _print("  Límites: " + ", ".join(str(value) for value in warnings))
    _print("\nLos scores ordenan candidatos; no son probabilidades ni autorizan acciones.")
    return _exit_code(payload)


def _run_ask(args: argparse.Namespace) -> int:
    payload = context_payload(
        args.query,
        args.scope,
        limit=args.limit,
        max_characters=args.characters,
        mode=args.mode,
        include_history=args.history,
    )
    if args.json:
        _json(payload)
        return _exit_code(payload)
    _print(f"Evidencia citada para responder: {payload['query']}")
    _print("NeoCortex recupera contexto local; no inventa una respuesta sin evidencia.")
    for entry in _entries(payload):
        bundle = entry.get("context")
        if not isinstance(bundle, dict):
            _render_scope_error(entry)
            continue
        hits = bundle.get("selected_hits", [])
        citations = bundle.get("citation_ids", [])
        hit_rows = [hit for hit in hits if isinstance(hit, dict)] if isinstance(hits, list) else []
        citation_rows = (
            [citation for citation in citations if isinstance(citation, dict)]
            if isinstance(citations, list)
            else []
        )
        _print(
            f"\n{_scope_label(entry.get('scope'))}: "
            f"{bundle.get('completeness', entry.get('status'))}, {len(hit_rows)} citas."
        )
        for index, hit in enumerate(hit_rows):
            citation = citation_rows[index] if index < len(citation_rows) else {}
            _render_hit(hit, prefix=f"[{citation.get('citation_id', f'K{index + 1}')}]")
        if not hit_rows:
            missing = bundle.get("missing_information")
            if isinstance(missing, list) and missing:
                for reason in missing:
                    _print(f"  Falta: {reason}")
            else:
                _print("  No hay evidencia suficiente para responder.")
    return _exit_code(payload)


def _run_inspect_code(args: argparse.Namespace) -> int:
    payload = code_search_payload(
        args.query,
        args.scope,
        limit=args.limit,
        modes=tuple(args.modes or ("hybrid",)),
    )
    if args.json:
        _json(payload)
        return _exit_code(payload)
    _print(f"Inspección de código para: {payload['query']}")
    for entry in _entries(payload):
        hits = entry.get("hits")
        if not isinstance(hits, list):
            _render_scope_error(entry)
            continue
        _print(f"\n{_scope_label(entry.get('scope'))}: {len(hits)} coincidencias.")
        for index, hit in enumerate(hits, start=1):
            if not isinstance(hit, dict):
                continue
            symbol = f" · {hit['symbol']}" if hit.get("symbol") else ""
            _print(
                f"{index}. {hit.get('path', '-')}:{hit.get('start_line', '-')}-"
                f"{hit.get('end_line', '-')}{symbol}"
            )
            _print(f"   {_single_line(hit.get('snippet'))}")
            matches = hit.get("match_types")
            if isinstance(matches, (list, tuple)):
                _print("   Señales: " + ", ".join(str(value) for value in matches))
        if not hits:
            _print("  No se encontraron coincidencias en Code publicado.")
    return _exit_code(payload)


def _mapping(value: object) -> Mapping[str, object] | None:
    return value if isinstance(value, Mapping) else None


def _mapping_rows(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [row for row in value if isinstance(row, Mapping)]


def _render_text_lineage(text: Mapping[str, object]) -> None:
    lineage = _mapping(text.get("lineage"))
    if lineage is None:
        _print("  Text: el owner devolvió un contrato de linaje incompleto.")
        return
    location = lineage.get("path") or lineage.get("file_key") or "sin documento publicado"
    _print(
        f"  Text: {location} · "
        f"estado {lineage.get('document_status', '-')} · "
        f"atribución {lineage.get('attribution', '-')}."
    )
    revision = _mapping(lineage.get("revision"))
    if revision is None:
        _print("    Revisión: no atribuida.")
    else:
        _print(
            f"    Revisión: {revision.get('revision_id', '-')} · "
            f"recurso {revision.get('resource_id', '-')}."
        )
    materializations = _mapping_rows(lineage.get("materializations"))
    receipt_count = text.get("receipt_count", 0)
    head_count = text.get("current_materialization_heads", 0)
    _print(
        f"    Receipts: {receipt_count} · materializaciones: {len(materializations)} "
        f"({head_count} publicadas como head)."
    )
    if bool(text.get("receipt_window_truncated")):
        _print("    Advertencia: la ventana de receipts fue truncada por el límite de lectura.")
    for materialization in materializations:
        reference = _mapping(materialization.get("materialization"))
        materialization_id = (
            reference.get("materialization_id", "-") if reference is not None else "-"
        )
        head = "head actual" if bool(materialization.get("current_head")) else "histórica"
        _print(f"    - {materialization.get('name', 'output')}: {materialization_id} · {head}.")
    dependencies = _mapping_rows(text.get("dependencies"))
    if dependencies:
        _print(f"    Dependencias derivadas: {len(dependencies)}.")


def _render_semantic_lineage(semantic: Mapping[str, object]) -> None:
    lineage = _mapping(semantic.get("lineage"))
    if lineage is None:
        _print("  Semantic: el owner devolvió un contrato de linaje incompleto.")
        return
    origins = _mapping_rows(lineage.get("origins"))
    embeddings = _mapping_rows(lineage.get("embeddings"))
    _print(
        f"  Semantic: chunk {lineage.get('chunk_id', '-')} · "
        f"linaje {lineage.get('lineage_status', '-')} · "
        f"{len(origins)} orígenes · {len(embeddings)} embeddings."
    )
    signature = lineage.get("chunking_signature")
    if signature:
        _print(f"    Firma de chunking: {signature}")
    for embedding in embeddings:
        publication = "publicado" if bool(embedding.get("published")) else "no publicado"
        model = embedding.get("model_id") or embedding.get("model_signature") or "-"
        _print(
            f"    - generación {embedding.get('generation_id', '-')} · modelo {model} · "
            f"{publication} · linaje {embedding.get('lineage_status', '-')}."
        )


def _run_inspect_lineage(args: argparse.Namespace) -> int:
    payload = lineage_payload(args.identifier, args.scope)
    if args.json:
        _json(payload)
        return _exit_code(payload)
    _print(f"Linaje de derivación para: {payload.get('identifier', args.identifier)}")
    for entry in _entries(payload):
        lineage = _mapping(entry.get("lineage"))
        if lineage is None:
            _render_scope_error(entry)
            continue
        status = str(lineage.get("status", entry.get("status", "unknown")))
        text = _mapping(lineage.get("text"))
        semantic = _mapping(lineage.get("semantic"))
        if status == "not_found" and text is None and semantic is None:
            _print(
                f"{_scope_label(entry.get('scope'))}: no se encontró ese identificador "
                "en el linaje publicado."
            )
        elif text is None and semantic is None:
            _print(f"{_scope_label(entry.get('scope'))}: linaje no disponible ({status}).")
        else:
            coverage = "completa" if bool(lineage.get("complete")) else "parcial"
            _print(f"{_scope_label(entry.get('scope'))}: {status} · cobertura {coverage}.")
            if text is not None:
                _render_text_lineage(text)
            if semantic is not None:
                _render_semantic_lineage(semantic)
            semantic_dependents = _mapping(lineage.get("semantic_dependents"))
            if semantic_dependents is not None:
                count = semantic_dependents.get("chunk_count_in_window", 0)
                suffix = " (ventana truncada)" if semantic_dependents.get("truncated") else ""
                label = "chunk" if count == 1 else "chunks"
                _print(f"  Semantic dependiente: {count} {label}{suffix}.")
        warnings = lineage.get("warnings")
        if isinstance(warnings, list) and warnings:
            _print("  Advertencias: " + "; ".join(str(value) for value in warnings))
    _print("No se creó, migró ni modificó estado.")
    return _exit_code(payload)


def _run_review_value(args: argparse.Namespace) -> int:
    if args.refresh and args.scope == ReadScope.ALL.value:
        _print(
            "review value --refresh requiere --scope personal o framework; no se modificó estado.",
            file=sys.stderr,
        )
        return 2
    try:
        adapter = importlib.import_module("neocortex.api.cli.value_review")
        run_value_review = adapter.run_value_review
    except (AttributeError, ImportError):
        _print(
            "La revisión de valor aún no está disponible en este árbol; "
            "no se modificó ningún archivo.",
            file=sys.stderr,
        )
        return 2
    return run_value_review(
        scope=args.scope,
        limit=args.limit,
        json_output=args.json,
        refresh=args.refresh,
    )


def _run_review_task(args: argparse.Namespace) -> int:
    if args.scope == ReadScope.ALL.value:
        _print(
            "review task requiere --scope personal o framework; no se modificó estado.",
            file=sys.stderr,
        )
        return 2
    try:
        adapter = importlib.import_module("neocortex.api.cli.review_task")
    except (AttributeError, ImportError):
        _print("La revisión durable no está disponible; no se modificó estado.", file=sys.stderr)
        return 2
    if args.review_task_command == "show":
        return adapter.run_review_task_show(
            task_id=args.task_id,
            scope=args.scope,
            json_output=args.json,
        )
    if args.review_task_command == "history":
        return adapter.run_review_task_history(
            task_id=args.task_id,
            scope=args.scope,
            json_output=args.json,
        )
    if args.review_task_command == "claim":
        return adapter.run_review_task_claim(
            task_id=args.task_id,
            scope=args.scope,
            expected_event_id=args.expected_event_id,
            actor=args.actor,
            note=args.note,
            json_output=args.json,
        )
    if args.review_task_command == "decide":
        return adapter.run_review_task_decide(
            task_id=args.task_id,
            scope=args.scope,
            expected_event_id=args.expected_event_id,
            decision=args.decision,
            decision_scope=args.decision_scope,
            actor=args.actor,
            note=args.note,
            json_output=args.json,
        )
    raise ValueError("review task requires show or decide")


def _run_knowledge_health(args: argparse.Namespace) -> int:
    try:
        payload = asset_health_payload(args.resource_id, args.scope)
    except ValueError as exc:
        _print(f"knowledge health: {exc}", file=sys.stderr)
        return 2
    if args.json:
        _json(payload)
        return _exit_code(payload)
    _print(f"Salud causal del activo: {payload.get('resource_id', args.resource_id)}")
    for entry in _entries(payload):
        report = _mapping(entry.get("asset_health"))
        if report is None:
            _render_scope_error(entry)
            continue
        _print(
            f"{_scope_label(entry.get('scope'))}: {report.get('health', 'unknown')} · "
            f"evidencia {report.get('completeness', 'abstained')} · "
            f"razón {report.get('reason_code') or '-'}"
        )
        gaps = report.get("gaps")
        if isinstance(gaps, list) and gaps:
            _print("  Brechas: " + "; ".join(str(item) for item in gaps))
    _print("No se creó, migró ni modificó estado.")
    return _exit_code(payload)


def _run_agent_serve() -> int:
    from ..agent_server import run_stdio_server

    return run_stdio_server()


def _run_database_purge(args: argparse.Namespace) -> int:
    from .database_purge import run_database_purge

    return run_database_purge(args)


def run_human_command(arguments: Sequence[str]) -> int:
    parser = build_human_parser()
    args = parser.parse_args(list(arguments))
    if args.command == "help":
        parser.print_help()
        return 0
    if args.command == "status":
        return _run_status(args)
    if args.command == "search":
        return _run_search(args)
    if args.command == "ask":
        return _run_ask(args)
    if args.command == "inspect" and args.inspect_command == "code":
        return _run_inspect_code(args)
    if args.command == "inspect" and args.inspect_command == "lineage":
        return _run_inspect_lineage(args)
    if args.command == "review" and args.review_command == "value":
        return _run_review_value(args)
    if args.command == "review" and args.review_command == "task":
        return _run_review_task(args)
    if args.command == "knowledge" and args.knowledge_command == "health":
        return _run_knowledge_health(args)
    if args.command in {"databases", "database"} and args.database_command == "purge":
        return _run_database_purge(args)
    if args.command == "agent" and args.agent_command == "serve":
        return _run_agent_serve()
    parser.error("falta una acción concreta")
    raise AssertionError("argparse.error must exit")


__all__ = (
    "HUMAN_COMMANDS",
    "build_human_parser",
    "handles_human_command",
    "run_human_command",
)
