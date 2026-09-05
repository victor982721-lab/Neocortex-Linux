from __future__ import annotations


import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from neocortex.capabilities.formats.image.analysis import (
    ImageMemoryGate,
    ImageResourceLimits,
    extract_features,
)
from neocortex.runtime.control.memory_runtime import (
    MemoryBudgetExceeded,
    MemoryHeadroomTimeout,
    MemorySnapshot,
)


TEST_CAPABILITIES = ('image',)


# region [01] Integrated analysis memory bounds


class ImageClassifierMemoryTests(unittest.TestCase):
    def test_memory_budget_serializes_large_estimates(self):
        gate = ImageMemoryGate(
            ImageResourceLimits(
                memory_budget_bytes=100,
                min_free_memory_bytes=0,
                min_free_commit_bytes=0,
            )
        )
        first_entered = threading.Event()
        release_first = threading.Event()
        second_entered = threading.Event()

        def first() -> None:
            with gate.admit(60):
                first_entered.set()
                release_first.wait(2)

        def second() -> None:
            with gate.admit(60):
                second_entered.set()

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
        self.assertEqual(gate.peak_reserved_bytes, 60)

    def test_headroom_wait_recalculates_after_another_reservation_releases(self):
        gate = ImageMemoryGate(
            ImageResourceLimits(
                memory_budget_bytes=200,
                min_free_memory_bytes=0,
                min_free_commit_bytes=0,
                wait_timeout_seconds=1.0,
            )
        )
        first_entered = threading.Event()
        release_first = threading.Event()
        second_entered = threading.Event()
        errors: list[BaseException] = []

        def first() -> None:
            try:
                with gate.admit(100):
                    first_entered.set()
                    release_first.wait(2)
            except BaseException as exc:
                errors.append(exc)

        def second() -> None:
            try:
                with gate.admit(100):
                    second_entered.set()
            except BaseException as exc:
                errors.append(exc)

        with patch(
            "neocortex.runtime.control.memory_runtime.memory_snapshot",
            return_value=MemorySnapshot(150, 150),
        ):
            first_thread = threading.Thread(target=first)
            second_thread = threading.Thread(target=second)
            first_thread.start()
            self.assertTrue(first_entered.wait(1))
            second_thread.start()

            deadline = time.monotonic() + 1
            reserved = 0
            while time.monotonic() < deadline:
                with gate._condition:
                    reserved = gate._reserved
                if reserved == 200:
                    break
                time.sleep(0.01)
            self.assertEqual(reserved, 200)
            self.assertFalse(second_entered.is_set())

            release_first.set()
            self.assertTrue(second_entered.wait(1))
            first_thread.join(2)
            second_thread.join(2)

        self.assertFalse(first_thread.is_alive())
        self.assertFalse(second_thread.is_alive())
        self.assertEqual(errors, [])

    def test_simultaneous_headroom_admissions_do_not_reserve_before_one_passes(self):
        gate = ImageMemoryGate(
            ImageResourceLimits(
                memory_budget_bytes=200,
                min_free_memory_bytes=0,
                min_free_commit_bytes=0,
                wait_timeout_seconds=1.0,
            )
        )
        first_probe = threading.Event()
        release_probe = threading.Event()
        probe_lock = threading.Lock()
        probe_calls = 0
        entered = 0
        errors: list[BaseException] = []
        original_wait = gate._wait_for_headroom

        def blocked_wait(deadline: float) -> None:
            nonlocal probe_calls
            with probe_lock:
                probe_calls += 1
            first_probe.set()
            release_probe.wait(1)
            original_wait(deadline)

        def worker() -> None:
            nonlocal entered
            try:
                with gate.admit(100):
                    with probe_lock:
                        entered += 1
            except BaseException as exc:
                errors.append(exc)

        with (
            patch(
                "neocortex.runtime.control.memory_runtime.memory_snapshot",
                return_value=MemorySnapshot(150, 150),
            ),
            patch.object(gate, "_wait_for_headroom", side_effect=blocked_wait),
        ):
            first_thread = threading.Thread(target=worker)
            second_thread = threading.Thread(target=worker)
            first_thread.start()
            self.assertTrue(first_probe.wait(1))
            second_thread.start()
            time.sleep(0.05)

            with gate._condition:
                reserved_before_release = gate._reserved
            with probe_lock:
                probes_before_release = probe_calls
            self.assertEqual(reserved_before_release, 100)
            self.assertEqual(probes_before_release, 1)

            release_probe.set()
            first_thread.join(2)
            second_thread.join(2)

        self.assertFalse(first_thread.is_alive())
        self.assertFalse(second_thread.is_alive())
        self.assertEqual(entered, 2)
        self.assertEqual(errors, [])

    def test_feature_decode_uses_bounded_sample_and_reservation(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "large.jpg"
            with Image.new("RGB", (2400, 1600), "white") as image:
                image.save(path, quality=85)
            gate = ImageMemoryGate(
                ImageResourceLimits(
                    memory_budget_bytes=128 * 1024 * 1024,
                    min_free_memory_bytes=0,
                    min_free_commit_bytes=0,
                )
            )
            original_getexif = Image.Image.getexif

            def checked_getexif(image):
                self.assertGreater(gate._reserved, 0)
                return original_getexif(image)

            with patch.object(Image.Image, "getexif", checked_getexif):
                features = extract_features(path, gate)
            self.assertEqual((features.width, features.height), (2400, 1600))
            self.assertGreater(features.white_fraction, 0.99)
            self.assertGreaterEqual(gate.peak_reserved_bytes, path.stat().st_size)
            self.assertLessEqual(gate.peak_reserved_bytes, 128 * 1024 * 1024)

    def test_single_image_estimate_cannot_overrun_route_budget(self):
        gate = ImageMemoryGate(
            ImageResourceLimits(
                memory_budget_bytes=100,
                min_free_memory_bytes=0,
                min_free_commit_bytes=0,
            )
        )
        with self.assertRaises(MemoryBudgetExceeded):
            with gate.admit(101):
                self.fail("oversized reservation was admitted")

    def test_live_headroom_includes_the_incoming_reservation(self):
        gate = ImageMemoryGate(
            ImageResourceLimits(
                memory_budget_bytes=100,
                min_free_memory_bytes=100,
                min_free_commit_bytes=100,
                wait_timeout_seconds=0,
            )
        )
        with patch(
            "neocortex.runtime.control.memory_runtime.memory_snapshot",
            return_value=MemorySnapshot(150, 150),
        ):
            with self.assertRaises(MemoryHeadroomTimeout):
                with gate.admit(60):
                    self.fail("unsafe physical/commit headroom was admitted")


# endregion [01]


if __name__ == "__main__":
    unittest.main()
