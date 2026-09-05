"""Contract tests for the contained synthetic USN journal."""
# region [00] Contexto del módulo
# Módulo: tests/test_synthetic_usn.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

from pathlib import Path

import pytest

from neocortex.integrations.inventory import inventory_coordinator, reconcile
from neocortex.runtime.orchestration import orchestrator
from tests.synthetic_usn import (
    SyntheticUsnContainmentError,
    SyntheticUsnJournal,
)
# endregion [01]

# region [02] Implementación


def test_synthetic_usn_emits_create_modify_delete_and_rename(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    modified = root / "modified.bin"
    deleted = root / "deleted.bin"
    renamed = root / "old.bin"
    modified.write_bytes(b"old")
    deleted.write_bytes(b"delete")
    renamed.write_bytes(b"rename")

    with SyntheticUsnJournal(root) as journal:
        start = journal.capture(root.drive)
        modified.write_bytes(b"modified-and-longer")
        # Allocate the new file before releasing an inode. NTFS file references
        # carry a reuse sequence, while the POSIX ``st_ino`` test double does
        # not; this ordering keeps delete/create distinct on both platforms.
        (root / "created.bin").write_bytes(b"create")
        deleted.unlink()
        renamed.rename(root / "new.bin")
        target = journal.capture(root.drive)
        with journal.consume_changes(root.drive, start) as reader:
            batches = tuple(reader.iter_until(target.next_usn))

    reasons = [record.reason for batch in batches for record in batch.records]
    assert len(batches) == 1
    assert len(reasons) == 5
    assert {0x1, 0x100, 0x200, 0x1000, 0x2000} == set(reasons)
    assert journal.raw_volume_open_attempts == 0


@pytest.mark.parametrize("candidate_name", ("outside", "laboratory"))
def test_synthetic_usn_rejects_root_outside_temporary_lab(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, candidate_name: str,
) -> None:
    # An ordinary source extraction may itself be under /tmp. Exercise a
    # controlled boundary rather than assuming the checkout is outside it.
    laboratory = tmp_path / "laboratory"
    laboratory.mkdir()
    candidate = tmp_path / candidate_name
    candidate.mkdir(exist_ok=True)
    monkeypatch.setenv("NEOCORTEX_AUDIT_LAB_ROOT", str(laboratory))
    with pytest.raises(SyntheticUsnContainmentError, match="temporary laboratory"):
        SyntheticUsnJournal(candidate)


def test_synthetic_usn_restores_lookup_points_after_base_exception(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    original = (
        orchestrator.query_journal_cursor,
        inventory_coordinator.query_journal_cursor,
        reconcile.consume_changes,
    )

    class Abort(BaseException):
        pass

    with pytest.raises(Abort):
        with SyntheticUsnJournal(root):
            raise Abort

    assert (
        orchestrator.query_journal_cursor,
        inventory_coordinator.query_journal_cursor,
        reconcile.consume_changes,
    ) == original


# endregion [02]
