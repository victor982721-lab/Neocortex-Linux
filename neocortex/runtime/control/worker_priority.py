"""Give owned workers idle CPU while yielding fairly to foreground processes.

Only a worker itself calls ``configure_worker_priority``. No PID is targeted,
no parent environment or scheduling state changes, and no permission to raise
priority is needed. Admission still controls CPU/RAM/native/I/O concurrency.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class WorkerPriorityStatus:
    requested_nice: int
    previous_nice: int | None
    effective_nice: int | None
    applied: bool
    reason: str | None = None
    autogroup_requested: bool = False
    autogroup_applied: bool = False
    autogroup_nice: int | None = None
    autogroup_reason: str | None = None


_STATUS: WorkerPriorityStatus | None = None


def _configure_private_autogroup(nice: int) -> tuple[bool, int | None, str | None]:
    """Change only the autogroup of a caller-declared private session leader."""
    try:
        if os.getsid(0) != os.getpid() or os.getpgrp() != os.getpid():
            return False, None, "shared-session"
        path = Path("/proc/self/autogroup")
        previous = int(path.read_text(encoding="ascii").rsplit("nice", 1)[1].strip())
        if previous < nice:
            path.write_text(str(nice), encoding="ascii")
        current = int(path.read_text(encoding="ascii").rsplit("nice", 1)[1].strip())
        return current >= nice, current, None if current >= nice else "unchanged"
    except (AttributeError, IndexError, ValueError):
        return False, None, "unsupported"
    except OSError as exc:
        return False, None, f"errno:{exc.errno}"


def configure_worker_priority(*, nice: int = 10,
                              private_session: bool = False) -> WorkerPriorityStatus:
    """Lower only this process's priority, preserving any lower inherited one.

    Nice changes sharing under competition; it imposes no CPU utilization
    ceiling when processors are idle. This is deliberately not ``preexec_fn``:
    callers run here in an initialized worker or a fresh launcher interpreter.
    ``private_session=True`` is only for a child just placed in its own session.
    It also lowers that session's autogroup weight, after verifying that this
    process is both session and process-group leader. Pool workers leave the
    flag false and never change the parent's shared autogroup.
    Unsupported/denied policy is observable and leaves normal admission intact.
    """
    global _STATUS
    if not 0 <= nice <= 19:
        raise ValueError("worker nice value must be between 0 and 19")
    previous: int | None = None
    try:
        previous = os.getpriority(os.PRIO_PROCESS, 0)
        if previous < nice:
            os.nice(nice - previous)
        current = os.getpriority(os.PRIO_PROCESS, 0)
        _STATUS = WorkerPriorityStatus(nice, previous, current, current >= nice)
    except (AttributeError, OSError) as exc:
        _STATUS = WorkerPriorityStatus(
            nice, previous, previous, False,
            "unsupported" if isinstance(exc, AttributeError) else f"errno:{exc.errno}",
        )
    if private_session:
        applied, group_nice, reason = _configure_private_autogroup(nice)
        _STATUS = WorkerPriorityStatus(
            _STATUS.requested_nice, _STATUS.previous_nice, _STATUS.effective_nice,
            _STATUS.applied, _STATUS.reason, True, applied, group_nice, reason,
        )
    return _STATUS


def current_worker_priority() -> WorkerPriorityStatus | None:
    """Return the actual policy result from this initialized worker."""
    return _STATUS


def background_command(command: Sequence[str]) -> list[str]:
    """Launch a native tool with a priority change made only by its own child.

    The launcher uses exec, so cancellation, PID identity, return code, stdio,
    limits and process-group lifetime still belong to the original Popen child.
    """
    if not command:
        raise ValueError("background command cannot be empty")
    # A subprocess may have an unrelated cwd and an intentionally empty env.
    # The standalone launcher therefore never relies on package discovery.
    return [sys.executable, str(Path(__file__).resolve()), "--", *command]


def main() -> None:
    command = sys.argv[1:]
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise SystemExit("worker priority launcher requires a command")
    private_session = os.getsid(0) == os.getpid() and os.getpgrp() == os.getpid()
    configure_worker_priority(private_session=private_session)
    os.execvpe(command[0], command, os.environ)


if __name__ == "__main__":
    main()
