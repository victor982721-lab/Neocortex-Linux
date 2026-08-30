"""Hermetic internal-path policies for mutation-boundary tests."""
# region [00] Contexto del módulo
# Módulo: tests/internal_paths_test_support.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

import os
from pathlib import Path

from neocortex.enumeration import JournalCursor
from neocortex.safety.internal_paths import (
    InternalPathSpec,
    InternalPathsPolicy,
)
from neocortex.integrations.inventory.inventory_boundary import (
    build_normal_inventory_boundary,
)
from neocortex.persistence.framework_state_writer import FrameworkState
# endregion [01]

# region [02] Implementación


def disjoint_internal_paths_policy(base: Path) -> InternalPathsPolicy:
    """Reserve a missing canonical layout beside, never above, the test tree."""

    reservation = base.parent / f"{base.name}-internal-paths"
    repository = reservation / "Repository"
    runtime = reservation / "Programs" / "Neocortex"
    application_data = reservation / "AppData" / "Neocortex"
    return InternalPathsPolicy.capture(
        (
            InternalPathSpec("repository", "tree", repository),
            InternalPathSpec("runtime", "tree", runtime),
            InternalPathSpec("application_data", "tree", application_data),
            InternalPathSpec(
                "self_analysis",
                "tree",
                application_data / "self-analysis",
            ),
            InternalPathSpec(
                "launcher",
                "file",
                runtime / "bin" / "Neocortex.exe",
            ),
        )
    )


def begin_signed_normal_run(
    state: FrameworkState,
    corpus_root: Path,
    *,
    cursor: JournalCursor | None = None,
    internal_paths_policy: InternalPathsPolicy | None = None,
) -> int:
    """Begin a normal run with an exact boundary signature on sibling trees.

    Mutation fixtures must keep the framework database in a state directory
    beside, never above or inside, the corpus.  Deriving the state directory
    from the open repository prevents a fixture from signing a different
    boundary than the one that later authorizes its actions.
    """

    root = Path(os.path.abspath(os.path.realpath(corpus_root.expanduser())))
    state_directory = Path(os.path.abspath(os.path.realpath(state.path.parent.expanduser())))
    if os.path.normcase(os.fspath(root.parent)) != os.path.normcase(
        os.fspath(state_directory.parent)
    ):
        raise ValueError("test corpus and framework state directory must be siblings")
    boundary = build_normal_inventory_boundary(
        root,
        state_directory,
        internal_paths_policy=internal_paths_policy,
    )
    return state.begin_initial_run(
        root,
        cursor or JournalCursor(root.drive, 1, 0),
        inventory_policy_signature=boundary.effective_signature,
    )


__all__ = ["begin_signed_normal_run", "disjoint_internal_paths_policy"]
# endregion [02]
