from __future__ import annotations


import os
import queue
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
import pytest

from unittest.mock import Mock, patch

from neocortex.api.public import FrameworkConfig, FrameworkOrchestrator, RouteAdapter
from neocortex.runtime.control.cancellation import (
    CancellationRequested,
    CancellationToken,
)
from neocortex.runtime.control import console_cancellation
from neocortex.runtime.control.console_cancellation import (
    CTRL_BREAK_EVENT,
    CTRL_C_EVENT,
    ConsoleCancellationBridge,
)
from neocortex.runtime.control.global_resources import (
    GlobalResourceCoordinator,
    GlobalResourceLimits,
)
from neocortex.runtime.control.memory_runtime import MemorySnapshot
from neocortex.runtime.control.isolated_process import isolated_spawn_process
from neocortex.progress import RecordingProgress


TEST_CAPABILITIES = ("base", "documents")


# region [01] Framework-level cancellation


class FrameworkCancellationTests(unittest.TestCase):
    def test_console_event_requests_cancellation_and_interrupts_main(self) -> None:
        calls: list[str] = []
        bridge = ConsoleCancellationBridge(
            lambda: calls.append("cancel"),
            interrupt_main=lambda: calls.append("interrupt"),
        )

        self.assertTrue(bridge.handle_event(CTRL_C_EVENT))
        self.assertTrue(bridge.handle_event(CTRL_C_EVENT))
        self.assertFalse(bridge.handle_event(2))
        self.assertEqual(calls, ["cancel", "interrupt", "interrupt"])

    def test_windows_console_handler_registers_forwards_break_and_unregisters(
        self,
    ) -> None:
        calls: list[str] = []
        bridge = ConsoleCancellationBridge(
            lambda: calls.append("cancel"),
            interrupt_main=lambda: calls.append("interrupt"),
        )
        set_handler = Mock(side_effect=[True, True])
        kernel32 = SimpleNamespace(SetConsoleCtrlHandler=set_handler)
        fake_ctypes = SimpleNamespace(
            WINFUNCTYPE=lambda *_args: lambda callback: callback,
            WinDLL=lambda *_args, **_kwargs: kernel32,
            c_bool=object(),
            c_uint32=object(),
            get_last_error=lambda: 0,
        )

        with (
            patch.object(console_cancellation, "os", SimpleNamespace(name="nt")),
            patch.object(console_cancellation, "ctypes", fake_ctypes),
        ):
            with bridge:
                callback = bridge._callback
                assert callback is not None
                self.assertTrue(callback(CTRL_BREAK_EVENT))

        self.assertEqual(calls, ["cancel", "interrupt"])
        self.assertEqual(set_handler.call_count, 2)
        self.assertEqual(set_handler.call_args_list[0].args, (callback, True))
        self.assertEqual(set_handler.call_args_list[1].args, (callback, False))
        self.assertFalse(bridge._registered)
        self.assertIsNone(bridge._callback)
        self.assertIsNone(bridge._kernel32)

    def test_windows_console_registration_failure_releases_unregistered_state(
        self,
    ) -> None:
        bridge = ConsoleCancellationBridge(lambda: None, interrupt_main=lambda: None)
        set_handler = Mock(return_value=False)
        kernel32 = SimpleNamespace(SetConsoleCtrlHandler=set_handler)
        fake_ctypes = SimpleNamespace(
            WINFUNCTYPE=lambda *_args: lambda callback: callback,
            WinDLL=lambda *_args, **_kwargs: kernel32,
            c_bool=object(),
            c_uint32=object(),
            get_last_error=lambda: 87,
        )

        with (
            patch.object(console_cancellation, "os", SimpleNamespace(name="nt")),
            patch.object(console_cancellation, "ctypes", fake_ctypes),
            self.assertRaisesRegex(OSError, "registration failed") as raised,
        ):
            bridge.__enter__()

        self.assertEqual(raised.exception.errno, 87)
        self.assertFalse(bridge._registered)
        self.assertIsNone(bridge._callback)
        self.assertIsNone(bridge._kernel32)

    def test_windows_console_unregistration_failure_retains_native_callback(
        self,
    ) -> None:
        bridge = ConsoleCancellationBridge(lambda: None, interrupt_main=lambda: None)
        set_handler = Mock(side_effect=[True, False, True])
        kernel32 = SimpleNamespace(SetConsoleCtrlHandler=set_handler)
        fake_ctypes = SimpleNamespace(
            WINFUNCTYPE=lambda *_args: lambda callback: callback,
            WinDLL=lambda *_args, **_kwargs: kernel32,
            c_bool=object(),
            c_uint32=object(),
            get_last_error=lambda: 6,
        )

        with (
            patch.object(console_cancellation, "os", SimpleNamespace(name="nt")),
            patch.object(console_cancellation, "ctypes", fake_ctypes),
        ):
            with self.assertRaisesRegex(OSError, "unregistration failed") as raised:
                with bridge:
                    callback = bridge._callback
            assert callback is not None
            self.assertEqual(raised.exception.errno, 6)
            self.assertTrue(bridge._registered)
            self.assertIs(bridge._callback, callback)
            self.assertIs(bridge._kernel32, kernel32)
            retained = console_cancellation._FAILED_UNREGISTRATIONS[id(callback)]
            self.assertIs(retained[0], callback)
            self.assertIs(retained[1], kernel32)
            with self.assertRaisesRegex(RuntimeError, "still registered"):
                bridge.__enter__()

            bridge.__exit__(None, None, None)

        self.assertEqual(set_handler.call_count, 3)
        self.assertFalse(bridge._registered)
        self.assertIsNone(bridge._callback)
        self.assertIsNone(bridge._kernel32)
        self.assertNotIn(
            id(callback),
            console_cancellation._FAILED_UNREGISTRATIONS,
        )

    def test_keyboard_interrupt_cancels_other_routes_and_persists_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            corpus = base / "corpus"
            state = base / "state"
            corpus.mkdir()
            journal = SyntheticUsnJournal(corpus).start()
            self.addCleanup(journal.close)
            waiting_started = threading.Event()
            cancellation_wakes: list[bool] = []

            def waiting_route(context):
                waiting_started.set()
                cancellation_wakes.append(context.cancellation.wait(5))
                context.cancellation.checkpoint()

            def interrupting_route(_context):
                if not waiting_started.wait(1):
                    raise RuntimeError("waiting route did not start")
                raise KeyboardInterrupt

            registry = {
                "waiting": RouteAdapter("waiting", waiting_route),
                "interrupt": RouteAdapter("interrupt", interrupting_route),
            }
            orchestrator = FrameworkOrchestrator(
                FrameworkConfig(
                    root=corpus,
                    state_directory=state,
                    route="waiting,interrupt",
                    global_memory_budget_bytes=128 * 1024 * 1024,
                    global_min_free_memory_bytes=0,
                    global_min_free_commit_bytes=0,
                    global_cpu_slots=2,
                ),
                route_registry=registry,
            )

            with self.assertRaises(KeyboardInterrupt):
                orchestrator.run_initial()
            self.assertEqual(cancellation_wakes, [True])

            with closing(sqlite3.connect(state / "framework.sqlite3")) as connection:
                run_status = connection.execute(
                    "SELECT status FROM initial_runs ORDER BY run_id DESC LIMIT 1"
                ).fetchone()[0]
                route_statuses = dict(
                    connection.execute(
                        "SELECT route_name,status FROM route_runs "
                        "WHERE run_id=(SELECT MAX(run_id) FROM initial_runs)"
                    )
                )
            self.assertEqual(run_status, "cancelled")
            self.assertEqual(
                route_statuses,
                {"waiting": "cancelled", "interrupt": "cancelled"},
            )

# endregion [01]


# region [02] Blocking resource and subprocess waits


class BlockingCancellationTests(unittest.TestCase):
    def test_global_admission_wait_is_woken_by_cancel(self) -> None:
        token = CancellationToken()
        coordinator = GlobalResourceCoordinator(
            ("pdf", "image"),
            GlobalResourceLimits(
                memory_budget_bytes=100,
                min_free_memory_bytes=0,
                min_free_commit_bytes=0,
                cpu_slots=1,
                wait_timeout_seconds=5,
                poll_interval_seconds=1,
            ),
            cpu_load_probe=lambda: 0.0,
            cancellation=token,
        )
        snapshot = MemorySnapshot(10_000, 10_000, 20_000, 20_000)
        errors: list[BaseException] = []

        def waiting_admission() -> None:
            try:
                with coordinator.admit("image", 20):
                    self.fail("cancelled admission was granted")
            except BaseException as exc:
                errors.append(exc)

        with patch(
            "neocortex.runtime.control.global_resources.memory_snapshot",
            return_value=snapshot,
        ):
            with coordinator.admit("pdf", 80):
                thread = threading.Thread(target=waiting_admission)
                thread.start()
                time.sleep(0.05)
                coordinator.cancel()
                thread.join(1)

        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], CancellationRequested)

    @pytest.mark.capability("documents")
    def test_pdf_isolated_child_is_killed_when_cancelled(self) -> None:
        from neocortex.capabilities.formats.pdf.pdf_isolation import stream_isolated_profiles

        token = CancellationToken()

        class FakeQueue:
            def get(self, timeout):
                token.cancel()
                raise queue.Empty

            def close(self):
                return None

            def cancel_join_thread(self):
                return None

        class FakeProcess:
            pid = None
            exitcode = None

            def __init__(self):
                self.alive = False
                self.killed = False
                self.tree_terminated = False

            def start(self):
                self.alive = True

            def is_alive(self):
                return self.alive

            def kill(self):
                self.killed = True
                self.alive = False
                self.exitcode = -9

            def terminate_tree(self):
                self.tree_terminated = True
                self.alive = False
                self.exitcode = 1

            def join(self, timeout=None):
                return None

        fake_queue = FakeQueue()
        fake_process = FakeProcess()

        class FakeContext:
            def Queue(self, maxsize):
                return fake_queue

        with (
            patch(
                "neocortex.capabilities.formats.pdf.pdf_isolation.multiprocessing.get_context",
                return_value=FakeContext(),
            ),
            patch(
                "neocortex.capabilities.formats.pdf.pdf_isolation.isolated_spawn_process",
                return_value=fake_process,
            ),
        ):
            with self.assertRaises(CancellationRequested):
                list(
                    stream_isolated_profiles(
                        "document.pdf",
                        (),
                        timeout_seconds=10,
                        cancellation=token,
                    )
                )

        self.assertTrue(fake_process.tree_terminated)
        self.assertFalse(fake_process.killed)

    @unittest.skipUnless(os.name == "nt", "Windows Job Object behavior")
    def test_isolated_process_job_starts_and_terminates_only_its_child(self) -> None:
        normal = isolated_spawn_process(target=time.sleep, args=(0.01,))
        normal.start()
        normal.join(10)
        self.assertFalse(normal.is_alive())
        self.assertEqual(normal.exitcode, 0)
        normal.close()

        unrelated = isolated_spawn_process(target=time.sleep, args=(30,))
        cancelled = isolated_spawn_process(target=time.sleep, args=(30,))
        unrelated.start()
        cancelled.start()
        try:
            self.assertTrue(unrelated.is_alive())
            self.assertTrue(cancelled.is_alive())
            cancelled.terminate_tree()
            cancelled.join(10)
            self.assertFalse(cancelled.is_alive())
            self.assertTrue(unrelated.is_alive())
        finally:
            if unrelated.is_alive():
                unrelated.terminate_tree()
                unrelated.join(10)
            if cancelled.is_alive():
                cancelled.terminate_tree()
                cancelled.join(10)
            unrelated.close()
            cancelled.close()


# endregion [02]
