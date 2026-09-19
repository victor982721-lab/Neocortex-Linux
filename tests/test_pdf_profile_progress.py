# region [00] Contexto del módulo
# Módulo: tests/test_pdf_profile_progress.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]
# region [01] Dependencias del módulo
from __future__ import annotations


import time
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from neocortex.progress import ProgressEvent
from neocortex.capabilities.formats.pdf.pdf_derived import PdfDerivedIndexer


TEST_CAPABILITIES = ('documents',)
# endregion [01]

# region [02] Implementación


@pytest.mark.parametrize("waiting_admission", [False, True])
def test_profile_wait_emits_periodic_liveness_metrics(waiting_admission) -> None:
    events: list[ProgressEvent] = []
    owner = threading.get_ident()
    callbacks = []

    def progress(event):
        callbacks.append(threading.get_ident())
        events.append(event)

    indexer = object.__new__(PdfDerivedIndexer)
    indexer.workers = 1
    indexer.progress = progress
    indexer.resource_gate = (
        SimpleNamespace(active_count=0, worker_capacity=lambda **kwargs: 0)
        if waiting_admission else None
    )
    indexer.profile_memory_bytes = 1
    from neocortex.runtime.control.cancellation import CancellationToken

    indexer.cancellation = CancellationToken()
    indexer._profile_candidates = (  # type: ignore[method-assign]
        lambda: iter((("key", "document.pdf", 123),))
    )
    indexer._profile_candidate_count = lambda: 1  # type: ignore[method-assign]

    def admit_document(_key: str, _path: str, _size: int) -> bool:
        time.sleep(0.15)
        return True

    indexer._profile_document_admitted = admit_document  # type: ignore[method-assign]

    with patch(
        "neocortex.capabilities.formats.pdf.pdf_derived.PROFILE_PROGRESS_INTERVAL_SECONDS",
        0.01,
    ):
        built, errors = indexer._build_profiles()

    assert (built, errors) == (1, 0)
    waiting = [event for event in events if not event.finished and event.completed == 0]
    metric = "pending_admissions" if waiting_admission else "in_flight"
    assert any(
        {item.name: item.value for item in event.metrics}.get(metric) == 1
        for event in waiting
    )
    if waiting_admission:
        assert all(
            {item.name: item.value for item in event.metrics}.get("in_flight") == 0
            for event in waiting
        )
    assert callbacks and set(callbacks) == {owner}


# endregion [02]
