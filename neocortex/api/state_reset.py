"""Bounded Python facade for the selectable NeoCortex state reset.

The persistence package owns the state topology, backup, locks, fences and
destructive transaction.  This module only validates the public request and
projects the typed persistence plan/result into the same small envelope used
by the human CLI.  In particular, a preview never calls the destructive
executor and an apply always retains the exact confirmation token and plan
digest supplied by the caller.
"""

from __future__ import annotations

import inspect
import os
from collections.abc import Mapping
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal, TypeAlias
from uuid import uuid4


if TYPE_CHECKING:
    from neocortex.persistence.state_reset import (
        StateResetEntry as StateResetEntry,
        StateResetPlan as StateResetPlan,
        StateResetResult as StateResetResult,
        StateResetTarget as StateResetTarget,
        apply_state_reset as apply_state_reset,
        execute_state_reset as execute_state_reset,
        plan_state_reset as plan_state_reset,
        reset_state as reset_state,
    )


StateResetScope: TypeAlias = Literal["runs", "runs-and-caches", "all"]

STATE_RESET_SCHEMA = "neocortex.state-reset/v1"
# Keep an API-specific spelling available for consumers that use the other
# public modules' ``*_API_SCHEMA`` convention.  It intentionally has the same
# value as the persistence contract; the API does not version a second body.
STATE_RESET_API_SCHEMA = STATE_RESET_SCHEMA
STATE_RESET_CONFIRMATION = "RESET_STATE"
# Compatibility spelling shared with the persistence engine and the CLI.
RESET_STATE_CONFIRMATION = STATE_RESET_CONFIRMATION
STATE_RESET_YES = "yes"
STATE_RESET_INTERNAL_TOKEN = "__neocortex_state_reset_internal__"
STATE_RESET_SCOPES: Final[tuple[StateResetScope, ...]] = (
    "runs",
    "runs-and-caches",
    "all",
)

_ENGINE_TYPE_EXPORTS: Final[frozenset[str]] = frozenset(
    {"StateResetEntry", "StateResetPlan", "StateResetResult", "StateResetTarget"}
)
_ENGINE_EXPORTS: Final[frozenset[str]] = frozenset(
    {"apply_state_reset", "execute_state_reset", "plan_state_reset", "reset_state"}
)
_MAX_REQUEST_ID = 4_096
_MAX_DIGEST = 4_096


def _engine() -> Any:
    """Load the persistence engine only when this facade is actually used."""

    return import_module("neocortex.persistence.state_reset")


def _request_id(value: object) -> str:
    if value is None:
        return f"state-reset-{uuid4().hex}"
    if (
        not isinstance(value, str)
        or not value
        or value.strip() != value
        or len(value) > _MAX_REQUEST_ID
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("request_id is invalid")
    return value


def _scope(value: object) -> StateResetScope:
    if not isinstance(value, str) or value not in STATE_RESET_SCOPES:
        choices = ", ".join(STATE_RESET_SCOPES)
        raise ValueError(f"scope must be one of: {choices}")
    return value


def _absolute_path(value: object, *, label: str) -> Path:
    """Validate lexical path shape before delegating physical safety to the engine."""

    if isinstance(value, (str, os.PathLike)):
        try:
            raw = os.fspath(value)
        except TypeError as error:
            raise ValueError(f"{label} is invalid") from error
    else:
        raise ValueError(f"{label} is invalid")
    if isinstance(raw, bytes):
        try:
            raw = os.fsdecode(raw)
        except UnicodeDecodeError as error:
            raise ValueError(f"{label} is invalid") from error
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise ValueError(f"{label} is invalid")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{label} must be absolute")
    # Do not resolve here: resolving can access the filesystem and can hide an
    # alias.  The persistence engine owns the no-symlink/identity fence.
    return Path(os.path.normpath(os.fspath(path)))


def _digest(value: object) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or value.strip() != value
        or len(value) > _MAX_DIGEST
        or "\x00" in value
    ):
        raise ValueError("plan_digest is invalid")
    return value


def _as_payload(value: object, *, mode: str) -> dict[str, object]:
    """Project a typed engine object without reimplementing its serialization."""

    serializer = getattr(value, "as_payload", None)
    if callable(serializer):
        parameters: Mapping[str, inspect.Parameter] | None
        try:
            parameters = inspect.signature(serializer).parameters
        except (TypeError, ValueError):
            parameters = None
        if parameters is not None and "mode" in parameters:
            raw = serializer(mode=mode)
        else:
            raw = serializer()
    elif isinstance(value, Mapping):
        raw = dict(value)
    else:
        serializer = getattr(value, "to_dict", None)
        if not callable(serializer):
            raise TypeError("state reset result must expose as_payload() or be a mapping")
        raw = serializer()
    if not isinstance(raw, Mapping):
        raise TypeError("state reset payload must be a mapping")
    result = dict(raw)
    result.setdefault("mode", mode)
    return result


def _plan_digest_from(plan: object, payload: Mapping[str, object]) -> str:
    value = payload.get("plan_digest")
    if not isinstance(value, str) or not value:
        value = getattr(plan, "plan_digest", None)
    if not isinstance(value, str) or not value:
        raise ValueError("state reset plan did not provide a plan digest")
    return value


def _accepts_keyword(function: object, name: str) -> bool:
    if not callable(function):
        return False
    try:
        parameters = inspect.signature(function).parameters
    except (TypeError, ValueError):
        return True
    return name in parameters or any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
    )


def _execute(
    engine: Any,
    state_directory: Path,
    *,
    scope: StateResetScope,
    backup_directory: Path | None,
    confirmation: str,
    plan_digest: str,
    yes: bool = False,
) -> object:
    """Call either spelling of the engine's digest-binding keyword.

    ``plan_digest`` is the canonical public spelling.  The temporary
    ``expected_plan_digest`` compatibility branch keeps this facade usable
    while the persistence implementation is integrated across a checkout;
    it does not retry an execution after a runtime error.
    """

    function = engine.execute_state_reset
    kwargs: dict[str, object] = {
        "scope": scope,
        "backup_directory": backup_directory,
        "apply": True,
        "confirmation": confirmation,
    }
    # The optional flag is deliberately capability-detected so an older
    # injected/embedded engine keeps the legacy digest/token call shape.
    if yes and _accepts_keyword(function, "yes"):
        kwargs["yes"] = True
    if _accepts_keyword(function, "plan_digest"):
        kwargs["plan_digest"] = plan_digest
    elif _accepts_keyword(function, "expected_plan_digest"):
        kwargs["expected_plan_digest"] = plan_digest
    else:
        # The preview was already compared above.  Do not invent a keyword
        # unsupported by an older compatible engine; its own lock/replan still
        # remains authoritative for the effect.
        pass
    return function(state_directory, **kwargs)


def _envelope(
    *,
    request_id: str,
    scope: str,
    read_only: bool,
    status: str,
    result: Mapping[str, object] | None = None,
    error: Mapping[str, object] | None = None,
    exit_code: int = 0,
) -> dict[str, object]:
    return {
        "schema": STATE_RESET_API_SCHEMA,
        "kind": "state-reset",
        "operation": "state-reset",
        "request_id": request_id,
        "scope": scope,
        "read_only": read_only,
        "status": status,
        "exit_code": exit_code,
        "error": None if error is None else dict(error),
        "result": {} if result is None else dict(result),
    }


def _error_envelope(
    *,
    request_id: str,
    scope: str,
    read_only: bool,
    error: BaseException,
) -> dict[str, object]:
    message = str(error)
    # Exception text is produced by the local engine and may contain paths;
    # bound it before it crosses the public boundary.
    if len(message) > 1_000:
        message = message[:1_000]
    name = type(error).__name__
    lowered = name.casefold() + " " + message.casefold()
    if isinstance(error, ValueError):
        code = "invalid_request"
        exit_code = 2
    elif "confirmation" in lowered:
        code = "confirmation_required"
        exit_code = 3
    elif "busy" in lowered or "lock" in lowered:
        code = "busy"
        exit_code = 4
    elif "changed" in lowered or "digest" in lowered:
        code = "plan_changed"
        exit_code = 4
    elif "recovery" in lowered:
        code = "recovery_required"
        exit_code = 5
    elif "backup" in lowered:
        code = "backup_failed"
        exit_code = 5
    else:
        code = "unavailable"
        exit_code = 1
    return _envelope(
        request_id=request_id,
        scope=scope,
        read_only=read_only,
        status="error",
        error={
            "code": code,
            "type": name,
            "message": message,
            "retryable": code in {"busy", "plan_changed", "unavailable"},
            **({"operation_id": getattr(error, "operation_id", None)} if getattr(error, "operation_id", None) else {}),
        },
        exit_code=exit_code,
    )


def state_reset_recovery_payload(
    state_directory: str | Path, operation_id: str, *, apply: bool = False,
    receipt_digest: str | None = None, confirmation: str | None = None,
    request_id: str | None = None,
) -> dict[str, object]:
    """Preview or reconcile an exact durable reset operation without retrying it."""
    request = _request_id(request_id)
    try:
        if not isinstance(apply, bool):
            raise ValueError("apply must be a boolean")
        if not isinstance(operation_id, str) or not operation_id.startswith("state-reset-") or len(operation_id) > 256:
            raise ValueError("operation_id is invalid")
        result = _engine().reconcile_state_reset(
            _absolute_path(state_directory, label="state_directory"), operation_id,
            apply=apply, expected_receipt_digest=_digest(receipt_digest), confirmation=confirmation,
        )
        return _envelope(request_id=request, scope="recovery", read_only=not apply,
                         status="complete" if apply else "preview", result=result)
    except (OSError, RuntimeError, ValueError, TypeError) as error:
        return _error_envelope(request_id=request, scope="recovery", read_only=not apply, error=error)


def state_reset_payload(
    state_directory: str | os.PathLike[str],
    *,
    scope: StateResetScope = "runs",
    apply: bool = False,
    confirmation: str | None = None,
    yes: bool = False,
    plan_digest: str | None = None,
    expected_plan_digest: str | None = None,
    backup_directory: str | os.PathLike[str] | None = None,
    request_id: str | None = None,
) -> dict[str, object]:
    """Preview or apply one explicitly selected reset scope.

    ``apply=False`` is the safe default and returns the engine's read-only
    plan.  Applying requires both the exact ``RESET_STATE`` token and the
    digest returned by a current preview.  The state directory is intentionally
    explicit: callers cannot accidentally target the product state by omitting
    it while testing this destructive boundary.
    """

    # Keep a usable request id even when validation itself fails.
    try:
        request = _request_id(request_id)
    except ValueError:
        request = _request_id(None)
    read_only = apply is not True
    selected_scope = scope if isinstance(scope, str) else "-"
    try:
        if not isinstance(apply, bool):
            raise ValueError("apply must be a boolean")
        selected_scope = _scope(scope)
        state = _absolute_path(state_directory, label="state_directory")
        backup = (
            None
            if backup_directory is None
            else _absolute_path(backup_directory, label="backup_directory")
        )
        requested_digest = _digest(plan_digest)
        legacy_digest = _digest(expected_plan_digest)
        if requested_digest is not None and legacy_digest is not None and requested_digest != legacy_digest:
            raise ValueError("plan_digest and expected_plan_digest disagree")
        if requested_digest is None:
            requested_digest = legacy_digest
        if not apply:
            if confirmation is not None or yes is not False:
                raise ValueError("confirmation is only valid with apply=True")
            if requested_digest is not None:
                raise ValueError("plan_digest is only valid with apply=True")
        else:
            if not isinstance(yes, bool):
                raise ValueError("yes must be a boolean")
            if yes:
                if confirmation is None:
                    confirmation = STATE_RESET_CONFIRMATION
                elif confirmation not in {
                    STATE_RESET_CONFIRMATION,
                    STATE_RESET_YES,
                    STATE_RESET_INTERNAL_TOKEN,
                }:
                    raise ValueError("yes conflicts with confirmation")
            if confirmation in {STATE_RESET_YES, STATE_RESET_INTERNAL_TOKEN}:
                confirmation = STATE_RESET_CONFIRMATION
            if not isinstance(confirmation, str) or confirmation != STATE_RESET_CONFIRMATION:
                raise ValueError(
                    f"apply requires confirmation token {STATE_RESET_CONFIRMATION!r}"
                )
            if requested_digest is None:
                raise ValueError("apply requires plan_digest from the read-only reset preview")
            confirmed = confirmation
            requested_digest_value = requested_digest

        engine = _engine()
        planned = engine.plan_state_reset(state, scope=selected_scope)
        planned_payload = _as_payload(planned, mode="preview")
        observed_digest = _plan_digest_from(planned, planned_payload)
        if not apply:
            result = dict(planned_payload)
            result.update(
                {
                    "scope": selected_scope,
                    "mode": "preview",
                    "requires_confirmation": True,
                    "confirmation_option": "--confirm-state-reset RESET_STATE",
                    "apply_options": (
                        "--apply",
                        "--confirm-state-reset RESET_STATE",
                        "--plan-digest " + observed_digest,
                    ),
                }
            )
            if backup is not None:
                result["backup_directory"] = str(backup)
            return _envelope(
                request_id=request,
                scope=selected_scope,
                read_only=True,
                status="preview",
                result=result,
            )

        if observed_digest != requested_digest:
            raise ValueError(
                "state reset plan digest does not match the current state; preview again"
            )
        applied = _execute(
            engine,
            state,
            scope=selected_scope,
            backup_directory=backup,
            confirmation=confirmed,
            plan_digest=requested_digest_value,
            yes=yes,
        )
        result = _as_payload(applied, mode="applied")
        result.setdefault("scope", selected_scope)
        result["mode"] = "applied"
        return _envelope(
            request_id=request,
            scope=selected_scope,
            read_only=False,
            status="complete",
            result=result,
        )
    except Exception as error:
        return _error_envelope(
            request_id=request,
            scope=selected_scope,
            read_only=read_only,
            error=error,
        )


def __getattr__(name: str) -> Any:
    """Resolve typed engine result classes lazily for annotations/inspection."""

    if name in _ENGINE_TYPE_EXPORTS or name in _ENGINE_EXPORTS:
        value = getattr(_engine(), name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    """Expose the typed facade without eagerly importing the persistence engine."""

    return sorted(set(globals()) | set(__all__))


__all__ = (  # noqa: RUF022
    "STATE_RESET_API_SCHEMA",
    "STATE_RESET_CONFIRMATION",
    "STATE_RESET_INTERNAL_TOKEN",
    "STATE_RESET_SCHEMA",
    "STATE_RESET_SCOPES",
    "STATE_RESET_YES",
    "RESET_STATE_CONFIRMATION",
    "StateResetEntry",
    "StateResetPlan",
    "StateResetResult",
    "StateResetScope",
    "StateResetTarget",
    "apply_state_reset",
    "execute_state_reset",
    "plan_state_reset",
    "reset_state",
    "state_reset_payload",
)
