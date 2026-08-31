"""Validated UI execution requests translated into the stable CLI contract."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


# region [01] Request schema

# Keep this explicit and stable so the UI cannot silently enable new mutation
# surfaces merely because the canonical CLI gains another route.
ROUTE_ORDER = ("pdf", "docx", "office", "archive", "text", "audio", "image", "code")


@dataclass(frozen=True, slots=True)
class RunRequest:
    """One immutable execution request produced by the desktop UI."""

    root: Path
    routes: tuple[str, ...]
    apply: bool = False
    route_only: bool = False

    def validated(self) -> "RunRequest":
        root = self._validated_root()
        normalized_routes = self._validated_routes()
        self._validate_mode(normalized_routes)
        return RunRequest(
            root=root,
            routes=normalized_routes,
            apply=self.apply,
            route_only=self.route_only,
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

    def cli_arguments(self) -> list[str]:
        request = self.validated()
        arguments = [
            "--root",
            str(request.root),
        ]
        selected = ",".join(request.routes) or "none"
        arguments.extend(("--route", selected))
        if request.route_only:
            arguments.append("--route-only")
        if request.apply:
            arguments.append("--apply")
        return arguments


# endregion [01]
