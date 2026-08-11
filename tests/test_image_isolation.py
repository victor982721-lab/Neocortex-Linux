from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from PIL import Image

from _04_Nucleo_Operativo.cancellation import (
    CancellationRequested,
    CancellationToken,
)
from _04_Nucleo_Operativo.image_isolation import (
    ImageWorkerError,
    ImageWorkerSupervisor,
    ImageWorkerTimeout,
    RemoteImageWorkerError,
)


# region [01] Spawn-safe worker fixture


def _blocking_image_worker(task_channel, result_channel) -> None:
    del result_channel
    while True:
        task = task_channel.get()
        if task is None:
            return
        time.sleep(30)


def _memory_hungry_image_worker(task_channel, result_channel) -> None:
    del result_channel
    task = task_channel.get()
    if task is None:
        return
    allocations = []
    while True:
        allocations.append(bytearray(16 * 1024 * 1024))


# endregion [01]


# region [02] Cancellation and containment


class ImageIsolationTests(unittest.TestCase):
    def test_remote_decode_failure_preserves_structured_disposition(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "uniform-truncated.jpg"
            with Image.new("RGB", (512, 384), "white") as image:
                image.save(path, quality=90)
            path.write_bytes(path.read_bytes()[:-2])
            supervisor = ImageWorkerSupervisor()
            try:
                with self.assertRaises(RemoteImageWorkerError) as raised:
                    supervisor.classify(
                        path,
                        root,
                        memory_limit_bytes=256 * 1024 * 1024,
                        timeout_seconds=5,
                        cancellation=CancellationToken(),
                    )
            finally:
                supervisor.close()

        failure = raised.exception.failure
        self.assertEqual(failure.error_type, "RecoveredImageContentError")
        self.assertEqual(failure.phase, "decode")
        self.assertFalse(failure.retryable)
        self.assertEqual(failure.disposition, "deletion_candidate")

    @unittest.skipUnless(os.name == "nt", "Windows Job Object memory limit")
    def test_hard_memory_cap_contains_the_decoder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "image.png"
            with Image.new("RGB", (32, 32), "white") as image:
                image.save(path)
            supervisor = ImageWorkerSupervisor(_memory_hungry_image_worker)
            try:
                with self.assertRaises((ImageWorkerError, ImageWorkerTimeout)):
                    supervisor.classify(
                        path,
                        root,
                        memory_limit_bytes=512 * 1024 * 1024,
                        timeout_seconds=15,
                        cancellation=CancellationToken(),
                    )
            finally:
                supervisor.close()

    def test_impossible_memory_cap_fails_before_spawning_decoder(self) -> None:
        supervisor = ImageWorkerSupervisor()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "image.png"
            with Image.new("RGB", (32, 32), "white") as image:
                image.save(path)
            with self.assertRaisesRegex(ImageWorkerError, "minimum safe reservation"):
                supervisor.classify(
                    path,
                    root,
                    memory_limit_bytes=8 * 1024 * 1024,
                    timeout_seconds=2,
                    cancellation=CancellationToken(),
                )
        self.assertIsNone(supervisor._process)

    def test_cancellation_stops_a_blocked_decoder_promptly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "image.png"
            with Image.new("RGB", (32, 32), "white") as image:
                image.save(path)
            supervisor = ImageWorkerSupervisor(_blocking_image_worker)
            cancellation = CancellationToken()
            timer = threading.Timer(0.2, cancellation.cancel)
            started = time.monotonic()
            timer.start()
            try:
                with self.assertRaises(CancellationRequested):
                    supervisor.classify(
                        path,
                        root,
                        memory_limit_bytes=256 * 1024 * 1024,
                        timeout_seconds=30,
                        cancellation=cancellation,
                    )
            finally:
                timer.cancel()
                supervisor.close()
            self.assertLess(time.monotonic() - started, 5)


# endregion [02]


if __name__ == "__main__":
    unittest.main()
