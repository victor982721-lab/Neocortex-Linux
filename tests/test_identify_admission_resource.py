"""C02 Identify admission, fallback, and executable-proof regressions."""

from __future__ import annotations

import io
import struct
import threading
from pathlib import Path

from neocortex.deduplication import DedupIndex
from neocortex.platform.content_types import identify
from neocortex.platform.identification_probe import STRUCTURED_PROBE_LIMIT, structured_probe
from neocortex.persistence.framework_state_writer import FrameworkState
from neocortex.runtime.control.global_resources import (
    GlobalResourceCoordinator,
    GlobalResourceLimits,
    ResourceSample,
    resource_grant_scope,
)
from neocortex.workflow.actions.actions import FrameworkActions
from tests.internal_paths_test_support import begin_signed_normal_run


def _coordinator(memory: int) -> GlobalResourceCoordinator:
    return GlobalResourceCoordinator(
        ("actions.identify",),
        GlobalResourceLimits(
            memory_budget_bytes=memory,
            min_free_memory_bytes=0,
            min_free_commit_bytes=0,
            cpu_slots=1,
            native_thread_slots=1,
            poll_interval_seconds=0.005,
            wait_timeout_seconds=0.1,
        ),
        resource_probe=lambda: ResourceSample(
            available_physical=memory * 16,
            available_commit=memory * 16,
            total_physical=memory * 32,
            effective_cpu_capacity=1,
        ),
    )


def test_structured_escalation_abstains_instead_of_self_deadlocking() -> None:
    """An outer Identify lease cannot wait on impossible nested memory."""

    coordinator = _coordinator(128 * 1024)
    result: list[tuple[int, bool]] = []

    def worker() -> None:
        with coordinator.admit(
            "actions.identify",
            128 * 1024,
            cpu_slots=1,
            native_threads=1,
            io_slots=1,
        ) as grant:
            with resource_grant_scope(grant):
                with structured_probe(
                    io.BytesIO(b"x" * (128 * 1024 * 2)),
                    b"{" * (64 * 1024),
                    size=128 * 1024 * 3,
                ) as observed:
                    result.append((len(observed[0]), observed[1]))

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=1.0)
    try:
        assert not thread.is_alive(), "nested Identify admission is waiting on its own lease"
        assert result == [(64 * 1024, False)]
        assert coordinator.summary().transient_bytes == 0
    finally:
        if thread.is_alive():
            coordinator.cancel()
            thread.join(timeout=1.0)
        coordinator.close()


def test_structured_escalation_charges_one_nested_workspace_lease() -> None:
    """A fitting escalation is accounted separately and released exactly once."""

    coordinator = _coordinator(2 * 1024 * 1024)
    prefix = b"{" * (64 * 1024)
    with coordinator.admit(
        "actions.identify",
        len(prefix),
        cpu_slots=1,
        native_threads=1,
        io_slots=1,
    ) as grant:
        with resource_grant_scope(grant):
            with structured_probe(
                io.BytesIO(b"x"),
                prefix,
                size=len(prefix) + 1,
            ) as observed:
                assert observed[1] is True
                summary = coordinator.summary()
                nested = summary.routes["actions.identify.escalation"]
                assert nested.transient_bytes == (len(prefix) + 1) * 24
    assert coordinator.summary().transient_bytes == 0
    coordinator.close()


def test_budget_reservation_covers_structured_probe_cap_before_workers(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    state_root = tmp_path / "state"
    root.mkdir()
    state_root.mkdir()
    source = root / "large.dat"
    framing = b'{"value":"' + b'"}'
    source.write_bytes(
        b'{"value":"'
        + b"a" * (STRUCTURED_PROBE_LIMIT - len(framing))
        + b'"}'
    )
    reservations: list[tuple[str, int, int]] = []

    with (
        DedupIndex(state_root / "dedup.sqlite3") as index,
        FrameworkState(state_root / "framework.sqlite3") as state,
    ):
        scan = index.scan(root)
        run_id = begin_signed_normal_run(state, root)
        runner = FrameworkActions(
            index,
            state,
            run_id,
            scan.scan_id,
            apply=False,
            reserve_work=lambda key, items, size: reservations.append((key, items, size)),
        )
        summary = runner.identify_and_normalize()

        cold_reservations = tuple(reservations)
        reservations.clear()
        replay = FrameworkActions(
            index, state, run_id, scan.scan_id, apply=False,
            reserve_work=lambda key, items, size: reservations.append((key, items, size)),
        ).identify_and_normalize()

    content_reservations = [item for item in cold_reservations if "content-prefix" in item[0]]
    assert content_reservations
    assert content_reservations[0][2] >= STRUCTURED_PROBE_LIMIT
    assert summary.types_detected == 1
    assert replay.type_cache_hits == 1
    assert replay.type_cache_misses == 0
    assert not [item for item in reservations if "content-prefix" in item[0]]


def test_mz_zero_padding_is_not_executable_evidence(tmp_path) -> None:
    source = tmp_path / "unknown"
    source.write_bytes(b"MZ" + b"\0" * 510)
    decision = identify(source)
    assert decision.status == "unknown"
    assert decision.mime is None


def test_structurally_proved_pe_is_known(tmp_path) -> None:
    source = tmp_path / "runtime"
    payload = bytearray(512)
    payload[:2] = b"MZ"
    struct.pack_into("<I", payload, 0x3C, 0x40)
    payload[0x40:0x44] = b"PE\0\0"
    # One x86-64 section, a PE32+ optional header, executable image.
    struct.pack_into("<HHIIIHH", payload, 0x44, 0x8664, 1, 0, 0, 0, 0xF0, 0x0002)
    struct.pack_into("<H", payload, 0x40 + 24, 0x020B)
    source.write_bytes(payload)
    decision = identify(source)
    assert decision.mime == "application/vnd.microsoft.portable-executable"
    assert decision.evidence == "magic:pe"


def test_elf_magic_with_zero_header_is_unknown(tmp_path) -> None:
    source = tmp_path / "unknown-elf"
    source.write_bytes(b"\x7fELF" + b"\0" * 60)
    assert identify(source).status == "unknown"


def test_structurally_proved_elf_is_known(tmp_path) -> None:
    source = tmp_path / "runtime-elf"
    payload = bytearray(64)
    payload[:7] = b"\x7fELF\x02\x01\x01"
    struct.pack_into("<HHI", payload, 16, 2, 0x3E, 1)
    struct.pack_into("<QQ", payload, 32, 0, 0)
    struct.pack_into("<HHHHH", payload, 52, 64, 56, 0, 64, 0)
    source.write_bytes(payload)
    decision = identify(source)
    assert decision.mime == "application/x-elf"
    assert decision.evidence == "magic:elf"
