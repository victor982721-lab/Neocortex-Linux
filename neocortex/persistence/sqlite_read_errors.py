"""Dependency-free errors shared by SQLite read policy and temporary admission."""

from __future__ import annotations


class ImmutableSQLiteUnavailable(RuntimeError):
    """A database cannot be proven safe for an immutable read."""


class SQLiteSnapshotBudgetExceeded(ImmutableSQLiteUnavailable):
    """A detached SQLite snapshot exceeded a bounded preparation budget."""

    def __init__(self, reason: str, **context: object) -> None:
        if reason not in {"temporary_bytes", "prepare_time", "cancelled", "disk_space", "memory_pressure"}:
            raise ValueError(f"unsupported SQLite snapshot budget reason: {reason}")
        self.reason = reason
        self.context = context
        super().__init__(f"SQLite snapshot {reason.replace('_', ' ')} budget exhausted")
        if context:
            self.add_context(**context)

    def add_context(self, **context: object) -> None:
        """Attach actionable evidence without replacing the original reason."""
        self.context.update(context)
        detail = " ".join(f"{key}={value}" for key, value in self.context.items())
        self.args = (
            f"SQLite snapshot {self.reason.replace('_', ' ')} budget exhausted; {detail}; "
            "recovery=retain durable progress, release temporary pressure and resume; do not delete WAL/SHM",
        )

    def __reduce__(self):
        # Exception's default reducer would feed the human-readable message
        # back to __init__, which expects the stable reason code instead.
        return type(self), (self.reason,), self.__dict__

    def __setstate__(self, state: dict[str, object]) -> None:
        self.__dict__.update(state)
        if self.context:
            self.add_context()


# Preserve the established public exception identity/import and serialization path.
ImmutableSQLiteUnavailable.__module__ = "neocortex.persistence.sqlite_immutable"
SQLiteSnapshotBudgetExceeded.__module__ = "neocortex.persistence.sqlite_immutable"
