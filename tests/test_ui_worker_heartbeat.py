# region [00] Contexto del módulo
# Módulo: tests/test_ui_worker_heartbeat.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]
# region [01] Dependencias del módulo
from __future__ import annotations

from neocortex.progress import ProgressEvent
from neocortex.interface.protocol.worker import (
    _active_progress_snapshot,
    _reset_active_progress,
    _track_progress,
)

import pytest


TEST_CAPABILITIES = ("base", 'ui')
pytestmark = pytest.mark.capability("base", 'ui')
# endregion [01]

# region [02] Implementación


def test_worker_heartbeat_tracks_only_unfinished_progress() -> None:
    _reset_active_progress()
    _track_progress(ProgressEvent("pdf", "profile", "Perfilando PDF", 0, 26, "PDF"))

    active = _active_progress_snapshot()
    assert len(active) == 1
    assert active[0]["description"] == "Perfilando PDF"

    _track_progress(
        ProgressEvent("pdf", "profile", "Perfiles PDF actualizados", 26, 26, "PDF", True)
    )
    assert _active_progress_snapshot() == []


# endregion [02]
