"""Validated UI execution requests translated into the stable CLI contract."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


# region [01] Request schema

# Keep this explicit and stable so the UI cannot silently enable new mutation
# surfaces merely because the canonical CLI gains another route.
ROUTE_ORDER = ("pdf", "docx", "office", "audio", "image", "code")


@dataclass(frozen=True, slots=True)
class RunRequest:
    """One immutable execution request produced by the desktop UI."""

    root: Path
    routes: tuple[str, ...]
    apply: bool = False
    route_only: bool = False

    def validated(self) -> "RunRequest":
        root = self.root.expanduser().resolve()
        if not root.is_dir():
            raise ValueError(f"La raíz no es un directorio accesible: {root}")
        unknown = tuple(route for route in self.routes if route not in ROUTE_ORDER)
        if unknown:
            raise ValueError("Rutas desconocidas: " + ", ".join(unknown))
        normalized_routes = tuple(route for route in ROUTE_ORDER if route in frozenset(self.routes))
        if self.route_only and not normalized_routes:
            raise ValueError("La ejecución aislada requiere al menos una ruta")
        if self.route_only and self.apply:
            raise ValueError("La ejecución aislada es siempre no destructiva; desactiva Apply")
        if os.name != "nt" and self.apply:
            raise ValueError(
                "linux_mutation_backend_unavailable: el modo Apply no está disponible en Linux"
            )
        return RunRequest(
            root=root,
            routes=normalized_routes,
            apply=self.apply,
            route_only=self.route_only,
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
