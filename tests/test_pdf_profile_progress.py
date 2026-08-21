# region [00] Contexto del módulo
# Módulo: tests/test_pdf_profile_progress.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]
# region [01] Dependencias del módulo
from __future__ import annotations

import time
from unittest.mock import patch

from neocortex.progress import ProgressEvent
from _04_Nucleo_Operativo.pdf_derived import PdfDerivedIndexer
# endregion [01]

# region [02] Implementación


def test_profile_wait_emits_periodic_liveness_metrics() -> None:
    events: list[ProgressEvent] = []
    indexer = object.__new__(PdfDerivedIndexer)
    indexer.workers = 1
    indexer.progress = events.append
    from _04_Nucleo_Operativo.cancellation import CancellationToken

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
        "_04_Nucleo_Operativo.pdf_derived.PROFILE_PROGRESS_INTERVAL_SECONDS",
        0.01,
    ):
        built, errors = indexer._build_profiles()

    assert (built, errors) == (1, 0)
    waiting = [event for event in events if not event.finished and event.completed == 0]
    assert any(
        {metric.name: metric.value for metric in event.metrics}.get("in_flight") == 1
        for event in waiting
    )


# endregion [02]
