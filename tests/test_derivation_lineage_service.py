from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

import neocortex.semantic.derivation_lineage_service as lineage_service_module
from neocortex.deduplication import FileSnapshot, snapshot_path
from neocortex.runtime.control.cancellation import CancellationToken
from neocortex.semantic.derivation_lineage_service import (
    inspect_derivation_lineage,
    rebuild_derivation_projection_from_owners,
    rebuild_text_derivation_projection,
)
from neocortex.semantic.derivation_contracts import (
    CapabilityFailure,
    InputBinding,
    ReproducibilityClass,
    StageDescriptor,
)
from neocortex.foundation.file_identity import file_key_from_snapshot
from neocortex.knowledge.knowledge_contracts import RevisionRef, RevisionState
from neocortex.knowledge.knowledge_snapshot import KnowledgeStateRootError
from neocortex.safety.route_filters import CandidateSelection
from neocortex.capabilities.formats.text.text_derivation_repository import (
    TextDerivationAttemptStart,
    begin_text_derivation_attempt,
    cancel_text_derivation_attempt,
)
from neocortex.capabilities.formats.text.text_route import TextRoute, TextRouteConfig
from neocortex.capabilities.formats.text.text_state import initialize_text_state, text_database


class _OneTextCandidate:
    def __init__(self, snapshot: FileSnapshot) -> None:
        self.snapshot = snapshot

    def selected_route_candidate_counts(
        self,
        _run_id: int,
        mime: str,
        _max_file_bytes: int | None,
        route_name: str,
        _selection: CandidateSelection,
    ) -> tuple[int, int]:
        assert route_name == "text"
        return (1, 1) if mime == "text/plain" else (0, 0)

    def iter_selected_route_candidates(
        self,
        _run_id: int,
        mime: str,
        route_name: str,
        _selection: CandidateSelection,
    ):
        assert route_name == "text"
        if mime == "text/plain":
            yield self.snapshot


def _indexed_text(tmp_path: Path) -> tuple[Path, FileSnapshot]:
    source = tmp_path / "corpus" / "lineage.txt"
    source.parent.mkdir()
    source.write_text("Linaje reproducible de una protección diferencial.", encoding="utf-8")
    snapshot = snapshot_path(source)
    state = tmp_path / "text.sqlite3"
    route = TextRoute(
        TextRouteConfig(state_path=state),
        _OneTextCandidate(snapshot),
        1,
        cancellation=CancellationToken(),
    )
    summary = route.run()
    assert summary.processed == 1
    assert summary.errors == 0
    return state, snapshot


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_text_lineage_explains_revision_stage_inputs_outputs_and_rebuilds(
    tmp_path: Path,
) -> None:
    state, snapshot = _indexed_text(tmp_path)

    payload = inspect_derivation_lineage(tmp_path, file_key_from_snapshot(snapshot))

    assert payload["status"] == "ready"
    assert payload["complete"] is True
    assert payload["semantic"] is None
    text = payload["text"]
    assert isinstance(text, dict)
    lineage = text["lineage"]
    assert isinstance(lineage, dict)
    assert lineage["attribution"] == "attributed"
    revision = lineage["revision"]
    assert isinstance(revision, dict)
    assert revision["producer"] == "text.source"
    assert text["receipt_count"] == 1
    receipts = text["receipts"]
    assert isinstance(receipts, list)
    receipt = receipts[0]
    assert receipt["stage"]["stage_id"] == "text.extract"
    assert receipt["execution_mode"] == "executed"
    assert receipt["reproducibility"] == "environment_bound"
    assert len(receipt["inputs"]) == 1
    assert {item["name"] for item in receipt["outputs"]} == {
        "text_fts",
        "text_representation",
    }
    assert text["current_materialization_heads"] == 2

    first = rebuild_text_derivation_projection(state)
    rebuilt = rebuild_text_derivation_projection(state)
    combined = rebuild_derivation_projection_from_owners(tmp_path)
    assert first == rebuilt == combined
    assert first.events_applied == 1


def test_lineage_inspection_is_read_only_and_missing_state_stays_missing(
    tmp_path: Path,
) -> None:
    state, snapshot = _indexed_text(tmp_path)
    before = _digest(state)

    first = inspect_derivation_lineage(tmp_path, file_key_from_snapshot(snapshot))
    second = inspect_derivation_lineage(tmp_path, file_key_from_snapshot(snapshot))

    assert first == second
    assert _digest(state) == before

    missing = tmp_path / "missing"
    not_found = inspect_derivation_lineage(missing, "revision:missing")
    assert not_found["status"] == "not_found"
    assert not_found["read_only"] is True
    assert not missing.exists()


def test_lineage_inspection_rejects_ambiguous_state_root_and_owner_path(
    tmp_path: Path,
) -> None:
    root_file = tmp_path / "state-file"
    root_file.write_text("not a state directory", encoding="utf-8")
    with pytest.raises(KnowledgeStateRootError, match="is not a directory"):
        inspect_derivation_lineage(root_file, "revision:any")
    with pytest.raises(KnowledgeStateRootError, match="is not a directory"):
        rebuild_derivation_projection_from_owners(root_file)

    state_root = tmp_path / "state-directory"
    state_root.mkdir()
    (state_root / "text.sqlite3").mkdir()
    with pytest.raises(KnowledgeStateRootError, match="non-file owner state path"):
        inspect_derivation_lineage(state_root, "revision:any")
    with pytest.raises(KnowledgeStateRootError, match="non-file owner state path"):
        rebuild_text_derivation_projection(state_root / "text.sqlite3")


def test_truncated_lineage_window_is_explicitly_partial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, snapshot = _indexed_text(tmp_path)
    second = TextRoute(
        TextRouteConfig(state_path=state),
        _OneTextCandidate(snapshot),
        2,
        cancellation=CancellationToken(),
    ).run()
    assert second.cache_hits == 1
    monkeypatch.setattr(lineage_service_module, "MAX_LINEAGE_RECEIPTS", 1)

    payload = inspect_derivation_lineage(tmp_path, file_key_from_snapshot(snapshot))

    assert payload["status"] == "partial"
    assert payload["complete"] is False
    assert "text:receipt_window_truncated" in payload["warnings"]
    assert "text:dependency_window_truncated" in payload["warnings"]


def test_historical_revision_materialization_and_receipt_remain_exactly_inspectable(
    tmp_path: Path,
) -> None:
    state, first_snapshot = _indexed_text(tmp_path)
    file_key = file_key_from_snapshot(first_snapshot)
    first = inspect_derivation_lineage(tmp_path, file_key)
    first_text = first["text"]
    assert isinstance(first_text, dict)
    first_lineage = first_text["lineage"]
    assert isinstance(first_lineage, dict)
    first_revision = first_lineage["revision"]
    assert isinstance(first_revision, dict)
    old_revision_id = str(first_revision["revision_id"])
    old_receipt_id = str(first_text["receipts"][0]["receipt_id"])
    first_materialization = first_lineage["materializations"][0]
    old_materialization_id = str(first_materialization["materialization"]["materialization_id"])

    source = Path(first_snapshot.path)
    source.write_text(
        "Una revisión posterior cambia el contenido y conserva el historial.",
        encoding="utf-8",
    )
    second_snapshot = snapshot_path(source)
    second = TextRoute(
        TextRouteConfig(state_path=state),
        _OneTextCandidate(second_snapshot),
        2,
        cancellation=CancellationToken(),
    ).run()
    assert second.extracted == 1

    current = inspect_derivation_lineage(tmp_path, file_key_from_snapshot(second_snapshot))
    current_revision = current["text"]["lineage"]["revision"]["revision_id"]
    assert current_revision != old_revision_id

    for identifier in (old_revision_id, old_materialization_id, old_receipt_id):
        historical = inspect_derivation_lineage(tmp_path, identifier)
        historical_text = historical["text"]
        assert isinstance(historical_text, dict)
        lineage = historical_text["lineage"]
        assert lineage["revision"]["revision_id"] == old_revision_id
        assert lineage["document_status"] == "historical"
        assert lineage["attribution"] == "attributed"
        assert historical_text["current_materialization_heads"] == 0


def test_lineage_reports_corrupt_owner_without_repairing_it(tmp_path: Path) -> None:
    state = tmp_path / "text.sqlite3"
    state.write_bytes(b"not a SQLite database")
    before = state.read_bytes()

    payload = inspect_derivation_lineage(tmp_path, "revision:any")

    assert payload["status"] == "corrupt"
    assert payload["complete"] is False
    assert payload["exit_code"] == 7
    assert state.read_bytes() == before


@pytest.mark.parametrize(
    "mutation",
    (
        "UPDATE document_fts SET body='FORGED'",
        "DELETE FROM text_materialization_heads WHERE materialization_kind='text_fts'",
        "UPDATE documents SET text_zlib=x'00'",
    ),
)
def test_lineage_never_marks_tampered_current_text_outputs_ready(
    tmp_path: Path,
    mutation: str,
) -> None:
    state, snapshot = _indexed_text(tmp_path)
    with sqlite3.connect(state) as connection:
        connection.execute(mutation)

    payload = inspect_derivation_lineage(tmp_path, file_key_from_snapshot(snapshot))

    assert payload["status"] == "corrupt"
    assert payload["complete"] is False
    assert payload["exit_code"] == 7


def test_cancelled_receipt_remains_inspectable_without_a_published_document(
    tmp_path: Path,
) -> None:
    state = tmp_path / "text.sqlite3"
    initialize_text_state(state)
    revision = RevisionRef(
        "resource:text:cancelled",
        "revision:text:cancelled",
        "text.source",
        "text-source-partial-v1",
        None,
        RevisionState.PARTIAL,
    )
    start = TextDerivationAttemptStart(
        attempt_id="attempt:text:cancelled",
        stage=StageDescriptor("text.extract", "2", "text-config-v2"),
        inputs=(InputBinding("source_observation", revision, "snapshot-fingerprint"),),
        effective_configuration=(),
        runtime=(("python", "3.14"),),
        started_at_utc="2026-08-11T00:00:00Z",
        started_monotonic_ns=1,
        attempt=1,
        run_id="run:cancelled",
        correlation_id="correlation:cancelled",
        recorded_ns=1,
    )
    begin_text_derivation_attempt(state, start)
    with text_database(state, create=False) as connection:
        connection.execute("BEGIN IMMEDIATE")
        cancel_text_derivation_attempt(
            connection,
            start.attempt_id,
            receipt_id="receipt:text:cancelled",
            finished_at_utc="2026-08-11T00:00:01Z",
            duration_ns=1,
            reproducibility=ReproducibilityClass.ENVIRONMENT_BOUND,
            failure=CapabilityFailure(
                "text.extract",
                "cancelled",
                "cancelled before publication",
                True,
            ),
            terminal_ns=2,
        )
        connection.commit()

    payload = inspect_derivation_lineage(tmp_path, "receipt:text:cancelled")

    assert payload["status"] == "partial"
    assert payload["complete"] is False
    text = payload["text"]
    assert isinstance(text, dict)
    assert text["lineage"]["attribution"] == "receipt_only"
    assert text["receipts"][0]["outcome"] == "cancelled"
    assert text["current_materialization_heads"] == 0


def test_failed_text_document_keeps_causal_receipt_without_false_corruption(
    tmp_path: Path,
) -> None:
    source = tmp_path / "malformado.txt"
    source.write_bytes(b"\x81\x8d\x8f\x90\x9d")
    snapshot = snapshot_path(source)
    state = tmp_path / "text.sqlite3"
    summary = TextRoute(
        TextRouteConfig(state_path=state),
        _OneTextCandidate(snapshot),
        1,
        cancellation=CancellationToken(),
    ).run()
    assert summary.errors == 1

    payload = inspect_derivation_lineage(tmp_path, file_key_from_snapshot(snapshot))

    assert payload["status"] == "partial"
    assert payload["complete"] is False
    text = payload["text"]
    assert isinstance(text, dict)
    assert text["lineage"]["document_status"] == "error"
    assert text["receipts"][0]["outcome"] == "failed"
    assert text["current_materialization_heads"] == 0


def test_failed_text_document_rejects_revision_from_another_physical_resource(
    tmp_path: Path,
) -> None:
    first_source = tmp_path / "malformado-a.txt"
    second_source = tmp_path / "malformado-b.txt"
    first_source.write_bytes(b"\x81\x8d\x8f\x90\x9d")
    second_source.write_bytes(b"\x81\x8d\x8f\x90\x9e")
    first_snapshot = snapshot_path(first_source)
    second_snapshot = snapshot_path(second_source)
    state = tmp_path / "text.sqlite3"
    for run_id, snapshot in enumerate((first_snapshot, second_snapshot), start=1):
        summary = TextRoute(
            TextRouteConfig(state_path=state, max_documents=1),
            _OneTextCandidate(snapshot),
            run_id,
            cancellation=CancellationToken(),
        ).run()
        assert summary.errors == 1
    with sqlite3.connect(state) as connection:
        second_revision = connection.execute(
            "SELECT revision_id FROM documents WHERE file_key=?",
            (file_key_from_snapshot(second_snapshot),),
        ).fetchone()[0]
        connection.execute(
            "UPDATE documents SET revision_id=? WHERE file_key=?",
            (second_revision, file_key_from_snapshot(first_snapshot)),
        )

    payload = inspect_derivation_lineage(tmp_path, file_key_from_snapshot(first_snapshot))

    assert payload["status"] == "corrupt"
    assert payload["complete"] is False
