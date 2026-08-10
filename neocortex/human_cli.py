"""Concise canonical CLI for read-only NeoCortex consultation."""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TextIO

from .read_api import (
    ReadScope,
    code_search_payload,
    context_payload,
    search_payload,
    status_payload,
)


HUMAN_COMMANDS = frozenset({"help", "status", "search", "ask", "inspect", "review", "agent"})


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
            "personal consulta el corpus; framework consulta el autoanálisis; "
            "all los agrupa sin mezclar scores"
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
            "Consulta local, trazable y de solo lectura. Los comandos heredados "
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
    _add_scope(inspect_code, default=ReadScope.FRAMEWORK)
    inspect_code.add_argument("--limit", type=int, default=10, metavar="N")
    inspect_code.add_argument(
        "--mode",
        action="append",
        dest="modes",
        help="canal Code; puede repetirse (por defecto: hybrid)",
    )
    inspect_code.add_argument("--json", action="store_true")

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
    review_value.add_argument("--json", action="store_true")

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
    return {"personal": "Personal", "framework": "Framework"}.get(str(value), str(value))


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


def _run_review_value(args: argparse.Namespace) -> int:
    try:
        adapter = importlib.import_module("neocortex.value_cli_adapter")
        run_value_review = adapter.run_value_review
    except (AttributeError, ImportError):
        _print(
            "La revisión de valor aún no está disponible en este árbol; "
            "no se modificó ningún archivo.",
            file=sys.stderr,
        )
        return 2
    return run_value_review(scope=args.scope, limit=args.limit, json_output=args.json)


def _run_agent_serve() -> int:
    from .agent_server import run_stdio_server

    return run_stdio_server()


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
    if args.command == "review" and args.review_command == "value":
        return _run_review_value(args)
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
