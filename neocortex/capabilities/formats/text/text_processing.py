"""Pure Text parsing in an isolated process with effective per-item limits."""

from __future__ import annotations

import os
import resource
import signal
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, Protocol, TypeVar

from neocortex.capabilities.broker import CapabilitySelection
from neocortex.deduplication import FileChangedError, FileSnapshot, snapshot_path
from neocortex.runtime.control.cancellation import CancellationToken
from neocortex.runtime.control.elastic_workers import current_worker_cancellation

class _ParserConfiguration(Protocol):
    @property
    def worker_timeout_seconds(self) -> float: ...

    @property
    def worker_memory_bytes(self) -> int: ...


_Configuration = TypeVar("_Configuration", bound=_ParserConfiguration)
_Extracted = TypeVar("_Extracted")


@dataclass(frozen=True, slots=True)
class TextParseWork(Generic[_Configuration, _Extracted]):
    attempt_id: str
    snapshot: FileSnapshot
    mime: str
    payload: bytes
    config: _Configuration
    selection: CapabilitySelection
    extractor: Callable[[bytes, str, str, _Configuration, CapabilitySelection], _Extracted]


@dataclass(frozen=True, slots=True)
class TextParseResult(Generic[_Extracted]):
    attempt_id: str
    extracted: _Extracted | None = None
    failure: tuple[str, str] | None = None
    completed_metrics: tuple[tuple[str, int], ...] = ()


def _deadline(_signal_number, _frame) -> None:
    raise TimeoutError("Text parser exceeded its execution deadline")


@contextmanager
def text_parser_limits(timeout_seconds: float, memory_bytes: int):
    """Scope limits to one spawned worker task, restoring reusable workers."""

    if timeout_seconds <= 0 or memory_bytes < 1:
        raise ValueError("Text parser limits must be positive")
    current_virtual = int(Path("/proc/self/statm").read_text().split()[0]) * os.sysconf(
        "SC_PAGE_SIZE"
    )
    if memory_bytes <= current_virtual:
        raise MemoryError("Text parser memory limit cannot fit its isolated interpreter")
    previous_limit = resource.getrlimit(resource.RLIMIT_AS)
    hard_limit = previous_limit[1]
    effective_limit = (
        memory_bytes if hard_limit == resource.RLIM_INFINITY else min(memory_bytes, hard_limit)
    )
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    started = time.monotonic()
    resource.setrlimit(resource.RLIMIT_AS, (effective_limit, hard_limit))
    try:
        signal.signal(signal.SIGALRM, _deadline)
        signal.setitimer(signal.ITIMER_REAL, timeout_seconds)
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        resource.setrlimit(resource.RLIMIT_AS, previous_limit)
        if previous_timer[0] > 0:
            signal.setitimer(
                signal.ITIMER_REAL,
                max(0.000001, previous_timer[0] - (time.monotonic() - started)),
                previous_timer[1],
            )


def parse_text_work(work: TextParseWork[_Configuration, _Extracted]) -> TextParseResult[_Extracted]:
    """No SQLite, capability probing, original writes, or parent handles."""

    cancellation = current_worker_cancellation() or CancellationToken()
    try:
        cancellation.checkpoint()
        with text_parser_limits(
            work.config.worker_timeout_seconds, work.config.worker_memory_bytes,
        ):
            extracted = work.extractor(
                work.payload, work.mime, work.snapshot.path, work.config, work.selection,
            )
            cancellation.checkpoint()
            if snapshot_path(work.snapshot.path) != work.snapshot:
                raise FileChangedError("Text source changed during extraction")
        return TextParseResult(work.attempt_id, extracted=extracted)
    except (MemoryError, OSError, RuntimeError, UnicodeError, ValueError, ET.ParseError) as exc:
        kind = (
            "memory" if isinstance(exc, MemoryError)
            else "timeout" if isinstance(exc, TimeoutError)
            else "source" if isinstance(exc, FileChangedError)
            else "io" if isinstance(exc, OSError)
            else "unicode" if isinstance(exc, UnicodeError)
            else "xml" if isinstance(exc, ET.ParseError)
            else "value" if isinstance(exc, ValueError)
            else "runtime"
        )
        return TextParseResult(work.attempt_id, failure=(kind, str(exc)[:4096]))


def text_worker_failure(failure: tuple[str, str]) -> Exception:
    kind, message = failure
    if kind == "memory":
        from neocortex.runtime.control.memory_runtime import MemoryBudgetExceeded
        return MemoryBudgetExceeded(message)
    errors = {
        "timeout": TimeoutError,
        "source": FileChangedError,
        "io": OSError,
        "unicode": UnicodeError,
        "xml": ET.ParseError,
        "value": ValueError,
        "runtime": RuntimeError,
    }
    return errors.get(kind, RuntimeError)(message)
