from __future__ import annotations

import unittest
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from neocortex.capabilities.formats.pdf.pdf_runtime import (
    MemorySnapshot,
    PdfResourceError,
    PdfResourceGate,
    PdfResourceLimits,
    ensure_free_space,
    wait_for_available_memory,
)


# region [01] Resource admission
# Exercise both immediate admission and bounded failure without real memory pressure.


class PdfRuntimeTests(unittest.TestCase):
    def test_reports_only_admitted_jobs_as_active(self):
        gate = PdfResourceGate(
            PdfResourceLimits(
                min_free_bytes=0,
                memory_backpressure_bytes=0,
                commit_backpressure_bytes=0,
                memory_budget_bytes=100,
                worker_memory_bytes=60,
            ),
            Path.cwd(),
        )
        self.assertEqual(gate.active_count, 0)
        with gate.admit(0):
            self.assertEqual(gate.active_count, 1)
        self.assertEqual(gate.active_count, 0)

    def test_rejects_budget_smaller_than_one_worker(self):
        with self.assertRaisesRegex(ValueError, "cannot be smaller"):
            PdfResourceGate(
                PdfResourceLimits(
                    memory_budget_bytes=59,
                    worker_memory_bytes=60,
                ),
                Path.cwd(),
            )

    def test_free_space_floor_rejects_dispatch(self):
        with patch(
            "neocortex.capabilities.formats.pdf.pdf_runtime.shutil.disk_usage",
            return_value=SimpleNamespace(free=100),
        ):
            with self.assertRaises(PdfResourceError):
                ensure_free_space(Path.cwd(), 101)

    def test_memory_backpressure_has_bounded_wait(self):
        with (
            patch(
                "neocortex.capabilities.formats.pdf.pdf_runtime.memory_snapshot",
                return_value=MemorySnapshot(None, 100, None, None),
            ),
            patch("neocortex.capabilities.formats.pdf.pdf_runtime.time.sleep"),
            patch(
                "neocortex.capabilities.formats.pdf.pdf_runtime.time.monotonic",
                side_effect=(0.0, 0.0, 1.0),
            ),
        ):
            with self.assertRaises(PdfResourceError):
                wait_for_available_memory(101, 0.5)

    def test_commit_backpressure_has_bounded_wait(self):
        with (
            patch(
                "neocortex.capabilities.formats.pdf.pdf_runtime.memory_snapshot",
                return_value=MemorySnapshot(None, 1_000, None, 100),
            ),
            patch("neocortex.capabilities.formats.pdf.pdf_runtime.time.sleep"),
            patch(
                "neocortex.capabilities.formats.pdf.pdf_runtime.time.monotonic",
                side_effect=(0.0, 0.0, 1.0),
            ),
        ):
            with self.assertRaises(PdfResourceError):
                wait_for_available_memory(101, 0.5, 101)

    def test_weighted_budget_serializes_workers_over_capacity(self):
        gate = PdfResourceGate(
            PdfResourceLimits(
                min_free_bytes=0,
                memory_backpressure_bytes=1,
                commit_backpressure_bytes=1,
                memory_budget_bytes=100,
                worker_memory_bytes=60,
                memory_wait_timeout_seconds=0,
            ),
            Path.cwd(),
        )
        first_entered = threading.Event()
        release_first = threading.Event()
        second_entered = threading.Event()

        def first() -> None:
            with gate.admit(0):
                first_entered.set()
                release_first.wait(2)

        def second() -> None:
            with gate.admit(0):
                second_entered.set()

        with (
            patch(
                "neocortex.capabilities.formats.pdf.pdf_runtime.memory_snapshot",
                return_value=MemorySnapshot(None, 1_000, None, 1_000),
            ),
        ):
            first_thread = threading.Thread(target=first)
            second_thread = threading.Thread(target=second)
            first_thread.start()
            self.assertTrue(first_entered.wait(1))
            second_thread.start()
            time.sleep(0.05)
            self.assertFalse(second_entered.is_set())
            release_first.set()
            first_thread.join(2)
            second_thread.join(2)
        self.assertTrue(second_entered.is_set())


# endregion [01]


if __name__ == "__main__":
    unittest.main()
