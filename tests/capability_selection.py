"""Static test declarations, read before pytest imports an optional capability.

``TEST_CAPABILITIES`` and ``TEST_PLATFORMS`` are literal tuples at module scope,
not product requirements. Undeclared modules are base tests. A mixed module may
declare several capabilities and use ``@pytest.mark.capability(...)`` on its
individual tests, with optional imports inside those tests/fixtures. Declaring
``base`` means the module can be collected with only the runtime and test-base
extras, regardless of which other capabilities are installed.

Selection is explicit, never inferred from whether a dependency is installed;
missing dependencies in a selected test remain failures rather than skips.
Capabilities are selection labels (OR), not a dependency/availability expression:
every dependency actually used by a selected test is still required (AND).
The nearest per-test marker overrides a class/module marker rather than widening
it; a mixed module's unmarked tests default to base when it declares base.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


CAPABILITIES = frozenset({"base", "documents", "image", "inference", "platform", "agent"})


class CapabilitySelectionError(ValueError):
    """An explicit test declaration/selection is malformed."""


def validate_capabilities(values: object, *, label: str) -> frozenset[str]:
    if not isinstance(values, (tuple, list)) or not values:
        raise CapabilitySelectionError(f"{label} must be a nonempty literal tuple/list")
    if any(not isinstance(value, str) or value not in CAPABILITIES for value in values):
        raise CapabilitySelectionError(
            f"{label} requires names from: {', '.join(sorted(CAPABILITIES))}"
        )
    return frozenset(values)


def parse_selection(value: str) -> frozenset[str]:
    names = tuple(name.strip() for name in value.split(","))
    if names == ("all",):
        return CAPABILITIES
    return validate_capabilities(names, label="--capabilities")


@dataclass(frozen=True)
class TestCapabilities:
    capabilities: frozenset[str] = frozenset({"base"})
    platforms: frozenset[str] = frozenset()


def read_test_capabilities(path: Path) -> TestCapabilities:
    stat = path.stat()
    return _read_test_capabilities(path, stat.st_mtime_ns, stat.st_size)


@lru_cache(maxsize=1024)
def _read_test_capabilities(path: Path, _mtime_ns: int, _size: int) -> TestCapabilities:
    # AST/literal_eval never executes a module, imports a backend, or queries a
    # package registry. A syntax error remains a normal pytest collection error.
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError:
        return TestCapabilities()
    declarations: dict[str, object] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Name):
                continue
            if target.id not in {"TEST_CAPABILITIES", "TEST_PLATFORMS"}:
                continue
            if target.id in declarations:
                raise CapabilitySelectionError(f"{path}: duplicate {target.id}")
            try:
                declarations[target.id] = ast.literal_eval(node.value)
            except (ValueError, TypeError) as exc:
                raise CapabilitySelectionError(
                    f"{path}: {target.id} must be a literal tuple/list"
                ) from exc
    capabilities = validate_capabilities(
        declarations.get("TEST_CAPABILITIES", ("base",)), label=f"{path}: TEST_CAPABILITIES"
    )
    platforms = declarations.get("TEST_PLATFORMS", ())
    if not isinstance(platforms, (tuple, list)) or any(
        not isinstance(value, str) or value not in {"linux", "win32", "darwin"}
        for value in platforms
    ):
        raise CapabilitySelectionError(f"{path}: TEST_PLATFORMS requires sys.platform names")
    return TestCapabilities(capabilities, frozenset(platforms))
