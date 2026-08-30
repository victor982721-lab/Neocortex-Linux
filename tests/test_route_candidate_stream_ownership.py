# region [00] Contexto del módulo
# Módulo: tests/test_route_candidate_stream_ownership.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]
# region [01] Dependencias del módulo
from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any, Iterator, cast
from unittest.mock import patch

from neocortex.deduplication import DedupIndex, FileSnapshot
from neocortex.runtime.control.cancellation import CancellationRequested
from neocortex.capabilities.formats.image.route import (
    ImageRoute,
    ImageRouteConfig,
    ImageRouteState,
)
from neocortex.capabilities.formats.image.state import iter_candidates
from neocortex.capabilities.formats.pdf.pdf_route import PdfRoute, PdfRouteConfig, PdfRouteState
# endregion [01]

# region [02] Implementación


class _ImageState:
    def __init__(self, snapshots: tuple[FileSnapshot, ...]) -> None:
        self.snapshots = snapshots

    def iter_route_candidates_by_prefix(
        self,
        _run_id: int,
        _mime_prefix: str,
    ) -> Iterator[tuple[str, FileSnapshot]]:
        for snapshot in self.snapshots:
            yield "image/png", snapshot


class _PdfState:
    def __init__(self, snapshots: tuple[FileSnapshot, ...]) -> None:
        self.snapshots = snapshots

    def iter_route_candidates(
        self,
        _run_id: int,
        mime: str,
    ) -> Iterator[FileSnapshot]:
        if mime == "application/pdf":
            yield from self.snapshots


def _snapshots(root: Path, suffix: str) -> tuple[FileSnapshot, ...]:
    return tuple(
        FileSnapshot(
            str(root / f"candidate-{identity}{suffix}"),
            1,
            identity,
            100,
            identity,
            identity,
        )
        for identity in (1, 2)
    )


def _close_on_another_thread(stream: Any) -> tuple[int, list[BaseException]]:
    errors: list[BaseException] = []
    closer_thread_id = 0

    def close_stream() -> None:
        nonlocal closer_thread_id
        closer_thread_id = threading.get_ident()
        try:
            stream.close()
        except BaseException as exc:  # pragma: no branch - regression capture
            errors.append(exc)

    thread = threading.Thread(target=close_stream)
    thread.start()
    thread.join(timeout=5)
    if thread.is_alive():
        raise AssertionError("candidate stream closer thread did not terminate")
    return closer_thread_id, errors


class RouteCandidateStreamOwnershipTests(unittest.TestCase):
    def test_image_candidate_connection_closes_in_owner_thread_on_failure_and_cancel(
        self,
    ) -> None:
        for failure in (
            RuntimeError("deterministic image worker failure"),
            CancellationRequested("deterministic image cancellation"),
        ):
            with (
                self.subTest(failure=type(failure).__name__),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)
                state_path = root / "state" / "image.sqlite3"
                route = ImageRoute(
                    ImageRouteConfig(
                        state_path=state_path,
                        root=root,
                        workers=1,
                        memory_budget_bytes=64 * 1024 * 1024,
                        min_free_memory_bytes=0,
                        min_free_commit_bytes=0,
                        document_ocr_mode="never",
                    ),
                    cast(
                        ImageRouteState,
                        _ImageState(_snapshots(root, ".png")),
                    ),
                    1,
                )
                streams: list[Any] = []

                def candidate_factory(
                    *args: Any,
                    _streams: list[Any] = streams,
                    **kwargs: Any,
                ) -> Any:
                    stream = iter_candidates(*args, **kwargs)
                    _streams.append(stream)
                    return stream

                def fail_after_open(
                    rows: Iterator[Any],
                    *_args: Any,
                    _failure: BaseException = failure,
                ) -> None:
                    next(rows)
                    raise _failure

                owner_thread_id = threading.get_ident()
                with (
                    patch(
                        "neocortex.capabilities.formats.image.route.iter_candidates",
                        side_effect=candidate_factory,
                    ),
                    patch.object(route, "_execute_rows", side_effect=fail_after_open),
                    self.assertRaises(type(failure)),
                ):
                    route.run()

                self.assertEqual(len(streams), 1)
                self.assertIsNone(streams[0].gi_frame)
                closer_thread_id, errors = _close_on_another_thread(streams[0])
                self.assertNotEqual(closer_thread_id, owner_thread_id)
                self.assertEqual(errors, [])

    def test_pdf_candidate_connection_closes_in_owner_thread_on_failure_and_cancel(
        self,
    ) -> None:
        for failure in (
            RuntimeError("deterministic PDF worker failure"),
            CancellationRequested("deterministic PDF cancellation"),
        ):
            with (
                self.subTest(failure=type(failure).__name__),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)
                state_path = root / "pdf.sqlite3"
                state = _PdfState(_snapshots(root, ".pdf"))
                with DedupIndex(root / "dedup.sqlite3") as index:
                    route = PdfRoute(
                        PdfRouteConfig(
                            state_path,
                            workers=1,
                            ocr_workers=1,
                            ocr_mode="never",
                            min_free_bytes=0,
                            commit_backpressure_bytes=0,
                        ),
                        index,
                        cast(PdfRouteState, state),
                        1,
                        1,
                    )
                    stream = route._candidate_snapshots()

                    def fail_after_open(
                        runtime: Any,
                        _connection: Any,
                        _failure: BaseException = failure,
                    ) -> None:
                        next(runtime.iterator)
                        raise _failure

                    owner_thread_id = threading.get_ident()
                    with (
                        patch.object(
                            route,
                            "_candidate_snapshots",
                            return_value=stream,
                        ),
                        patch.object(
                            route,
                            "_execute_extraction",
                            side_effect=fail_after_open,
                        ),
                        self.assertRaises(type(failure)),
                    ):
                        route.run()

                self.assertIsNone(cast(Any, stream).gi_frame)
                closer_thread_id, errors = _close_on_another_thread(stream)
                self.assertNotEqual(closer_thread_id, owner_thread_id)
                self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
# endregion [02]
