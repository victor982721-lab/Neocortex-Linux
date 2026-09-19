"""Real hashing workers follow live admission and leave SQLite on its owner."""

from contextlib import contextmanager
from pathlib import Path
import threading
import time
from unittest.mock import patch

from neocortex.deduplication import DedupIndex, DedupPlanner
from neocortex.deduplication import fingerprinting


class Gate:
    """Deterministic availability boundary; real pool/file reads are retained."""

    cancellation = None

    def __init__(self):
        self.condition = threading.Condition()
        self.capacity = 3
        self.active = 0
        self.peaks = {}
        self.first_peak = threading.Event()
        self.low_reads = 0
        self.low_read = threading.Event()
        self.phases = []

    def worker_capacity(self, **kwargs):
        with self.condition:
            return self.capacity

    def set_capacity(self, value):
        with self.condition:
            self.capacity = value
            self.condition.notify_all()

    @contextmanager
    def admit(self, estimated_bytes, **kwargs):
        gate = self

        class Grant:
            native_env = {}

            def __init__(self):
                self.held = False

            def acquire(self):
                with gate.condition:
                    while gate.active >= gate.capacity:
                        token = kwargs.get("cancellation")
                        if token is not None:
                            token.checkpoint()
                        gate.condition.wait(0.01)
                    gate.active += 1
                    self.held = True
                    gate.peaks[gate.capacity] = max(gate.peaks.get(gate.capacity, 0), gate.active)
                    if gate.capacity == 3 and gate.active == 3:
                        gate.first_peak.set()

            def release_cpu(self):
                with gate.condition:
                    if self.held:
                        gate.active -= 1
                        self.held = False
                        gate.condition.notify_all()

            def checkpoint(self, **_options):
                self.release_cpu()
                self.acquire()

        self.phases.append((kwargs.get("phase"), kwargs.get("io_slots"), kwargs.get("io_device")))
        grant = Grant()
        grant.acquire()
        try:
            yield grant
        finally:
            grant.release_cpu()


def test_hash_workers_contract_shrinks_and_recovers_with_one_sqlite_owner(tmp_path: Path):
    root = tmp_path / "corpus"
    root.mkdir()
    for i in range(48):
        (root / f"{i:03d}").write_bytes(bytes([i]) * (256 * 1024))
    gate = Gate()
    errors = []

    def change_availability():
        try:
            if not gate.first_peak.wait(5):
                raise AssertionError("initial parallel work did not start")
            gate.set_capacity(1)
            if not gate.low_read.wait(5):
                raise AssertionError("workers did not resume reads at low capacity")
        except BaseException as exc:
            errors.append(exc)
        finally:
            gate.set_capacity(4)

    full = fingerprinting.full_fingerprint
    observed_threads = set()

    def observed_full(snapshot, **kwargs):
        observed_threads.add(threading.get_ident())
        observer = kwargs.pop("read_observer")

        def read(count):
            observer(count)
            with gate.condition:
                if gate.capacity == 1 and count:
                    gate.low_reads += 1
                    if gate.low_reads >= 2:
                        gate.low_read.set()
            time.sleep(0.002)

        return full(snapshot, chunk_size=64 * 1024, read_observer=read, **kwargs)

    controller = threading.Thread(target=change_availability, daemon=True)
    owner_thread = threading.get_ident()
    sql_threads = set()
    with DedupIndex(tmp_path / "inventory.sqlite") as index:
        scan = index.scan(root, excluded_paths=())
        index._connection.set_trace_callback(lambda _statement: sql_threads.add(threading.get_ident()))
        controller.start()
        with patch.object(fingerprinting, "full_fingerprint", observed_full):
            plan = DedupPlanner(index, resource_gate=gate).plan(scan.scan_id)
        index._connection.set_trace_callback(None)
    controller.join(1)
    assert not controller.is_alive() and not errors
    assert gate.peaks[3] == 3 and gate.peaks[1] == 1 and gate.peaks[4] == 4
    assert gate.active == 0
    assert len(observed_threads) >= 3 and owner_thread not in observed_threads
    assert sql_threads == {owner_thread}
    assert plan.group_count == 0 and plan.statistics.full_hash_files == 48
    assert plan.statistics.hash_read_bytes == 48 * 256 * 1024
    assert all(phase in {"dedup_full", "dedup_metadata"} and slots == 1 and device is not None
               for phase, slots, device in gate.phases)
