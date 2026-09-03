"""Validated UI execution requests translated into the stable CLI contract."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import uuid4


# region [01] Request schema

# Keep this explicit and stable so the UI cannot silently enable new mutation
# surfaces merely because the canonical CLI gains another route.
ROUTE_ORDER = ("pdf", "docx", "office", "archive", "text", "audio", "image", "code")

ExecutionProfile = Literal["pilot", "full"]

# The desktop UI must never create an unbounded run.  ``pilot`` is the safe
# default and deliberately fits the documented 20--50 element trial window;
# ``full`` remains finite so it cannot turn into an accidental daemon.
PILOT_MAX_ITEMS = 50
PILOT_DEADLINE_SECONDS = 15 * 60
FULL_MAX_ITEMS = 100_000
FULL_DEADLINE_SECONDS = 48 * 60 * 60
MAX_UI_ITEMS = FULL_MAX_ITEMS
MAX_UI_DEADLINE_SECONDS = FULL_DEADLINE_SECONDS
PROFILE_DEFAULTS: dict[ExecutionProfile, tuple[int, float]] = {
    "pilot": (PILOT_MAX_ITEMS, PILOT_DEADLINE_SECONDS),
    "full": (FULL_MAX_ITEMS, FULL_DEADLINE_SECONDS),
}

_ROUTE_LIMIT_FLAGS = {
    "pdf": "--pdf-max-count",
    "docx": "--docx-max-count",
    "office": "--office-max-count",
    "archive": "--archive-max-count",
    "text": "--text-max-count",
    "audio": "--audio-max-count",
    "image": "--image-max-count",
    "code": "--code-max-count",
}


@dataclass(frozen=True, slots=True)
class RunRequest:
    """One immutable execution request produced by the desktop UI."""

    root: Path
    routes: tuple[str, ...]
    apply: bool = False
    route_only: bool = False
    profile: ExecutionProfile = "pilot"
    max_items: int | None = None
    deadline_seconds: float | None = None
    request_id: str = ""

    def validated(self) -> "RunRequest":
        root = self._validated_root()
        normalized_routes = self._validated_routes()
        self._validate_mode(normalized_routes)
        profile = self._validated_profile()
        max_items, deadline_seconds = self._validated_budget(profile)
        request_id = self._validated_request_id()
        return RunRequest(
            root=root,
            routes=normalized_routes,
            apply=self.apply,
            route_only=self.route_only,
            profile=profile,
            max_items=max_items,
            deadline_seconds=deadline_seconds,
            request_id=request_id,
        )

    def _validated_root(self) -> Path:
        root = self.root.expanduser().resolve()
        if not root.is_dir():
            raise ValueError(f"La raíz no es un directorio accesible: {root}")
        return root

    def _validated_routes(self) -> tuple[str, ...]:
        unknown = tuple(route for route in self.routes if route not in ROUTE_ORDER)
        if unknown:
            raise ValueError("Rutas desconocidas: " + ", ".join(unknown))
        selected = frozenset(self.routes)
        return tuple(route for route in ROUTE_ORDER if route in selected)

    def _validate_mode(self, routes: tuple[str, ...]) -> None:
        if self.route_only and not routes:
            raise ValueError("La ejecución aislada requiere al menos una ruta")
        if self.route_only and self.apply:
            raise ValueError("La ejecución aislada es siempre no destructiva; desactiva Apply")
        if os.name != "nt" and self.apply:
            raise ValueError(
                "linux_mutation_backend_unavailable: el modo Apply no está disponible en Linux"
            )

    def _validated_profile(self) -> ExecutionProfile:
        if self.profile not in PROFILE_DEFAULTS:
            raise ValueError("El perfil debe ser 'pilot' o 'full'")
        return self.profile

    def _validated_budget(self, profile: ExecutionProfile) -> tuple[int, float]:
        default_items, default_deadline = PROFILE_DEFAULTS[profile]
        max_items = default_items if self.max_items is None else self.max_items
        deadline_seconds = (
            default_deadline if self.deadline_seconds is None else self.deadline_seconds
        )
        if isinstance(max_items, bool) or not isinstance(max_items, int):
            raise ValueError("El límite de elementos debe ser un entero")
        profile_max_items = PILOT_MAX_ITEMS if profile == "pilot" else FULL_MAX_ITEMS
        if not 1 <= max_items <= profile_max_items:
            raise ValueError(
                f"El perfil {profile} permite entre 1 y {profile_max_items} elementos"
            )
        if isinstance(deadline_seconds, bool) or not isinstance(deadline_seconds, (int, float)):
            raise ValueError("El tiempo máximo debe ser numérico")
        profile_max_deadline = (
            PILOT_DEADLINE_SECONDS if profile == "pilot" else FULL_DEADLINE_SECONDS
        )
        if not 0.001 <= float(deadline_seconds) <= profile_max_deadline:
            raise ValueError(
                f"El perfil {profile} permite un tiempo de 0.001 a {profile_max_deadline} segundos"
            )
        return max_items, float(deadline_seconds)

    def _validated_request_id(self) -> str:
        request_id = self.request_id or uuid4().hex
        if not isinstance(request_id, str) or not request_id.strip():
            raise ValueError("El identificador de ejecución no puede estar vacío")
        if len(request_id) > 128 or any(ord(character) < 0x20 for character in request_id):
            raise ValueError("El identificador de ejecución no es válido")
        return request_id

    def cli_arguments(self) -> list[str]:
        request = self.validated()
        arguments = [
            "--root",
            str(request.root),
        ]
        selected = ",".join(request.routes) or "none"
        arguments.extend(("--route", selected))
        for route in request.routes:
            arguments.extend((_ROUTE_LIMIT_FLAGS[route], str(request.max_items)))
        if request.route_only:
            arguments.append("--route-only")
        if request.apply:
            arguments.append("--apply")
        return arguments


# endregion [01]
