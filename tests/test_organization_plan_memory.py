"""Organization selection keeps ordering and atomicity without retaining payloads."""

from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3

import pytest

from neocortex.documents import document_organization_planning as planning
from neocortex.documents.document_catalog import document_catalog_database
from neocortex.documents.document_organization_scope import capture_organization_input_scope
from neocortex.documents.document_resource_binding import build_resource_binding
from neocortex.foundation.file_identity import FileIdentity
from neocortex.platform.policy import stat_birthtime_ns
from neocortex.progress import ProgressEvent
from neocortex.runtime.control.cancellation import CancellationRequested, CancellationToken
from tests.test_document_organization_scope import _catalog, _file, _seed


def _members(catalog: Path, root: Path, count: int, *, payload_size: int = 0) -> None:
    anchor = _file(root, "package.zip")
    _seed(catalog, anchor, kind="archive", member="member-00000.pdf")
    observed = anchor.stat()
    identity = FileIdentity(observed.st_dev, observed.st_ino)
    with document_catalog_database(catalog) as connection:
        template = dict(connection.execute("SELECT * FROM documents").fetchone())
        columns = tuple(template)

        def rows():
            for index in range(count):
                row = dict(template)
                member = f"member-{index:05d}.pdf"
                key = f"archive:fixture:{anchor.name}:{member}"
                locator = f"{anchor}!/{member}"
                row.update(file_key=key, path=locator, classification_json=json.dumps({"payload": "x" * payload_size}))
                row["resource_binding_json"] = json.dumps(build_resource_binding(
                    source_kind="archive", file_key=key, path=locator,
                    identity=identity, birthtime_ns=stat_birthtime_ns(observed),
                    size=observed.st_size, mtime_ns=observed.st_mtime_ns,
                    anchor_path=str(anchor), archive_member={
                        "container_key": identity.packed_key,
                        "container_path": str(anchor), "member_chain": member,
                    },
                ), sort_keys=True)
                yield tuple(row[column] for column in columns)

        connection.execute("DELETE FROM documents")
        connection.executemany(
            f"INSERT INTO documents({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
            rows(),
        )
        connection.commit()


def _plans(catalog: Path) -> list[dict[str, object]]:
    with document_catalog_database(catalog, readonly=True) as connection:
        return [dict(row) for row in connection.execute("SELECT * FROM organization_plans ORDER BY plan_id")]


def test_organization_plan_bounds_live_classification_payloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = _catalog(tmp_path)
    root = tmp_path / "corpus"
    count = 900
    _members(catalog, root, count, payload_size=16_384)
    scope = capture_organization_input_scope(catalog, root)
    live = peak = read = 0
    planned_keys = []

    class ObservedRow(sqlite3.Row):
        def __init__(self, cursor, values):
            nonlocal live, peak, read
            self.has_payload = "classification_json" in self.keys()
            if self.has_payload:
                live += 1
                read += 1
                peak = max(peak, live)

        def __del__(self):
            nonlocal live
            if self.has_payload:
                live -= 1

    @contextmanager
    def observed_database(path, **kwargs):
        with document_catalog_database(path, **kwargs) as connection:
            connection.row_factory = ObservedRow
            yield connection

    def observe_plan(_connection, _run_id, row, _root, **_kwargs):
        planned_keys.append(row["file_key"])
        return "review"

    monkeypatch.setattr(planning, "document_catalog_database", observed_database)
    monkeypatch.setattr(planning, "_plan_catalog_document", observe_plan)
    summary = planning.plan_document_organization(catalog, tmp_path / "destination", source_scope=scope)
    assert summary.considered == count and summary.review_required == count
    assert len(set(planned_keys)) == count
    assert planned_keys == sorted(planned_keys)
    assert read == count
    assert peak <= 129, f"retained {peak} complete classification payloads for {count} documents"
    assert live == 0


def test_organization_pages_preserve_selection_order_destinations_and_progress(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    root = tmp_path / "corpus"
    _members(catalog, root, 129)
    physical = [_file(root, name) for name in ("Zeta.pdf", "Álgebra.pdf")]
    for path in physical:
        _seed(catalog, path)
    with document_catalog_database(catalog, readonly=True) as connection:
        expected_paths = [row[0] for row in connection.execute("SELECT path FROM documents WHERE active=1 ORDER BY path,source_kind,file_key")]
    _seed(catalog, _file(tmp_path / "outside", "excluded.pdf"))
    _seed(catalog, _file(root, "unresolved.pdf"), binding_present=False)
    _seed(catalog, root / "package.zip", kind="archive", member="component.xml", representation_metadata={
        "document_role": "document_component", "independently_organizable": False,
    })
    destination = tmp_path / "destination"
    events: list[ProgressEvent] = []
    summary = planning.plan_document_organization(
        catalog, destination, source_scope=capture_organization_input_scope(catalog, root),
        progress=events.append,
    )
    assert (summary.considered, summary.planned, summary.review_required) == (131, 2, 129)
    assert (summary.excluded_out_of_scope, summary.unresolved_scope, summary.excluded_components) == (1, 1, 1)
    plans = _plans(catalog)
    assert [plan["source_path"] for plan in plans] == expected_paths
    for plan in plans:
        if plan["source_kind"] == "archive":
            assert plan["destination_path"] is None
            assert plan["reason"] == "virtual_resource_requires_logical_organization"
        else:
            target = Path(str(plan["destination_path"]))
            assert target.is_relative_to(destination)
            assert target.name == Path(str(plan["source_path"])).name
    assert [(event.completed, event.total) for event in events] == [(0, 131), *((value, 131) for value in range(10, 131, 10)), (131, 131), (131, 131)]
    assert events[-1].finished
    assert not destination.exists()


@pytest.mark.parametrize("phase", ("selection", "planning"))
def test_organization_cancellation_preserves_complete_previous_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str,
) -> None:
    catalog = _catalog(tmp_path)
    root = tmp_path / "corpus"
    _members(catalog, root, 135)
    destination = tmp_path / "destination"
    scope = capture_organization_input_scope(catalog, root)
    planning.plan_document_organization(catalog, destination, source_scope=scope)
    previous = _plans(catalog)
    token = CancellationToken()
    seam = "assess_organization_resource" if phase == "selection" else "_plan_catalog_document"
    original = getattr(planning, seam)

    def cancel_after_first(*args, **kwargs):
        result = original(*args, **kwargs)
        token.cancel()
        return result

    monkeypatch.setattr(planning, seam, cancel_after_first)
    with pytest.raises(CancellationRequested):
        planning.plan_document_organization(catalog, destination, source_scope=scope, cancellation=token)
    assert _plans(catalog) == previous
    with document_catalog_database(catalog, readonly=True) as connection:
        status = connection.execute("SELECT status,error_type FROM catalog_runs ORDER BY catalog_run_id DESC LIMIT 1").fetchone()
        assert tuple(status) == ("failed", "CancellationRequested")
    assert not destination.exists()
