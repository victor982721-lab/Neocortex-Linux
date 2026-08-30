"""Lazy, extensible registry for source-language analyzers."""

from __future__ import annotations

from neocortex.platform import preserve_legacy_module as _preserve_legacy_module

import sys
from dataclasses import dataclass
from importlib import import_module
from threading import Lock
from typing import cast

from .code_contracts import LanguageAnalyzer
from neocortex.semantic.semantic_models import canonical_json, fingerprint_text


# region [01] Registration contract


@dataclass(frozen=True, slots=True)
class AnalyzerSpec:
    """Import recipe for one optional analyzer implementation."""

    analyzer_id: str
    languages: frozenset[str]
    module_name: str
    class_name: str
    analyzer_version: str
    priority: int = 100

    def __post_init__(self) -> None:
        if not self.analyzer_id or not self.analyzer_version or not self.languages:
            raise ValueError(
                "analyzer spec requires an identifier, version and languages"
            )


class AnalyzerRegistry:
    """Thread-safe lazy registry; missing optional analyzers never break fallback."""

    def __init__(self, specs: tuple[AnalyzerSpec, ...] = ()):
        self._lock = Lock()
        self._specs: dict[str, AnalyzerSpec] = {}
        self._loaded: dict[str, LanguageAnalyzer] = {}
        self._load_errors: dict[str, str] = {}
        for spec in specs:
            self.register(spec)

    def register(self, spec: AnalyzerSpec, *, replace: bool = False) -> None:
        with self._lock:
            if spec.analyzer_id in self._specs and not replace:
                raise ValueError(f"analyzer already registered: {spec.analyzer_id}")
            self._specs[spec.analyzer_id] = spec
            self._loaded.pop(spec.analyzer_id, None)
            self._load_errors.pop(spec.analyzer_id, None)

    def _ordered_specs(self, language: str | None) -> tuple[AnalyzerSpec, ...]:
        exact = [
            spec
            for spec in self._specs.values()
            if language is not None and language in spec.languages
        ]
        fallback = [spec for spec in self._specs.values() if "*" in spec.languages]
        return tuple(sorted([*exact, *fallback], key=lambda item: item.priority))

    def _load(self, spec: AnalyzerSpec) -> LanguageAnalyzer:
        with self._lock:
            loaded = self._loaded.get(spec.analyzer_id)
            if loaded is not None:
                return loaded
        try:
            module = import_module(spec.module_name, package=__package__)
        except ModuleNotFoundError:
            # Third-party and historical analyzer recipes may still name the
            # legacy package while the built-in registry resolves canonical
            # modules.  Keep that optional seam lazy and fail closed when both
            # locations are unavailable.
            if not spec.module_name.startswith("."):
                raise
            module = import_module(
                "_04_Nucleo_Operativo" + spec.module_name,
            )
        analyzer = cast(LanguageAnalyzer, getattr(module, spec.class_name)())
        if analyzer.analyzer_id != spec.analyzer_id:
            raise RuntimeError(
                f"analyzer {spec.class_name} declared {analyzer.analyzer_id!r}, "
                f"expected {spec.analyzer_id!r}"
            )
        if analyzer.analyzer_version != spec.analyzer_version:
            raise RuntimeError(
                f"analyzer {spec.class_name} declared version "
                f"{analyzer.analyzer_version!r}, expected {spec.analyzer_version!r}"
            )
        with self._lock:
            self._loaded[spec.analyzer_id] = analyzer
            self._load_errors.pop(spec.analyzer_id, None)
        return analyzer

    def analyzer_for(self, language: str | None) -> LanguageAnalyzer:
        errors: list[str] = []
        for spec in self._ordered_specs(language):
            try:
                return self._load(spec)
            except (ImportError, AttributeError, RuntimeError) as exc:
                detail = f"{type(exc).__name__}: {exc}"
                with self._lock:
                    self._load_errors[spec.analyzer_id] = detail
                errors.append(f"{spec.analyzer_id}={detail}")
        raise RuntimeError("no usable code analyzer: " + "; ".join(errors))

    def status(self) -> tuple[dict[str, object], ...]:
        with self._lock:
            return tuple(
                {
                    "analyzer_id": spec.analyzer_id,
                    "languages": tuple(sorted(spec.languages)),
                    "module": spec.module_name,
                    "class": spec.class_name,
                    "analyzer_version": spec.analyzer_version,
                    "loaded": spec.analyzer_id in self._loaded,
                    "load_error": self._load_errors.get(spec.analyzer_id),
                }
                for spec in sorted(self._specs.values(), key=lambda item: item.priority)
            )

    @property
    def processing_signature(self) -> str:
        """Fingerprint the lazy analyzer contract without importing analyzers."""

        with self._lock:
            ordered = sorted(
                self._specs.values(),
                key=lambda item: (item.priority, item.analyzer_id),
            )
            payload = canonical_json(
                {
                    "contract": "code-analyzer-registry-v1",
                    "analyzers": [
                        {
                            "id": spec.analyzer_id,
                            "version": spec.analyzer_version,
                            "languages": sorted(spec.languages),
                            "module": spec.module_name,
                            "class": spec.class_name,
                            "priority": spec.priority,
                        }
                        for spec in ordered
                    ],
                }
            )
        return "code-analyzers-v1:" + fingerprint_text(payload).xxh3_128


# endregion [01]


# region [02] Built-in analyzers


BUILTIN_ANALYZERS = (
    AnalyzerSpec(
        "neocortex-python-ast",
        frozenset({"python"}),
        ".code_python",
        "PythonAnalyzer",
        f"5|python-{sys.version_info.major}.{sys.version_info.minor}",
        priority=10,
    ),
    AnalyzerSpec(
        "neocortex-rust-lexical",
        frozenset({"rust"}),
        ".code_rust",
        "RustAnalyzer",
        "1",
        priority=20,
    ),
    AnalyzerSpec(
        "neocortex-generic-text",
        frozenset({"*"}),
        ".code_generic",
        "GenericAnalyzer",
        "1",
        priority=1000,
    ),
)


def builtin_analyzer_registry() -> AnalyzerRegistry:
    """Create an independent registry so tests/plugins cannot leak global state."""

    return AnalyzerRegistry(BUILTIN_ANALYZERS)


# endregion [02]


__all__ = [  # noqa: RUF022
    "AnalyzerRegistry",
    "AnalyzerSpec",
    "BUILTIN_ANALYZERS",
    "builtin_analyzer_registry",
]


_preserve_legacy_module(globals(), "_04_Nucleo_Operativo.code_analyzers")
