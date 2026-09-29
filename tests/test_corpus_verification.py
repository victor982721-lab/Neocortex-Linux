from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from neocortex.runtime.orchestration.corpus_verification import (
    ClassifiedSurvivor,
    CorpusVerificationInputs,
    CorpusVerifier,
    OrganizationMoveReceipt,
    OwnerCurrentPath,
    PendingFinding,
    ResidualSurvivor,
    verify_after_semantic,
    verify_before_semantic,
)


def _receipt(source: Path, target: Path) -> OrganizationMoveReceipt:
    metadata = target.stat()
    return OrganizationMoveReceipt.from_json(
        json.dumps(
            {
                "organization_receipt_schema": "neocortex.organization-move-receipt/v1",
                "operation": "move",
                "source_absent": True,
                "source_path": str(source),
                "source_digest": "metadata:v1:source",
                "target_path": str(target),
                "target_identity": {
                    "path": str(target),
                    "size": metadata.st_size,
                    "mtime_ns": metadata.st_mtime_ns,
                    "birthtime_ns": getattr(metadata, "st_birthtime_ns", -1),
                    "volume_id": f"{metadata.st_dev:x}",
                    "file_id": f"{metadata.st_ino:x}",
                },
            }
        ),
        owner="document_organization",
        status="applied",
    )


def _fixture(tmp_path: Path) -> tuple[Path, CorpusVerificationInputs]:
    root = tmp_path / "corpus"
    ordered = root / "Corpus_ordenado" / "PDF"
    residual = root / "Sin_clasificar" / "_MIME" / "application" / "octet-stream"
    ordered.mkdir(parents=True)
    residual.mkdir(parents=True)
    classified_path = ordered / "informe.pdf"
    residual_path = residual / "sin-tipo"
    classified_path.write_bytes(b"classified")
    residual_path.write_bytes(b"unknown")
    source = root / "source.pdf"
    state = tmp_path / "state"
    state.mkdir()
    current_path_values = tuple(
        (owner, state / f"{owner}.current")
        for owner in ("source_cache", "catalog", "framework", "dedup", "semantic")
    )
    for _, path in current_path_values:
        path.write_text("current", encoding="utf-8")
    current_paths = tuple(
        OwnerCurrentPath(
            owner,
            path,
            physical_identity={
                "volume_id": path.stat().st_dev,
                "file_id": path.stat().st_ino,
                "size": path.stat().st_size,
                "mtime_ns": path.stat().st_mtime_ns,
                "birthtime_ns": getattr(path.stat(), "st_birthtime_ns", -1),
            },
        )
        for owner, path in current_path_values
    )
    classified = ClassifiedSurvivor(
        path=classified_path,
        classification={"primary_kind": "report"},
        confidence=0.98,
        evidence={"source": "catalog", "classification_status": "classified"},
        organization_root=root,
        receipt=_receipt(source, classified_path),
    )
    residual_record = ResidualSurvivor(
        path=residual_path,
        mime="application/octet-stream",
        evidence={
            "detector": "fallback",
            "inventory_current": {
                "path": str(residual_path),
                "volume_id": residual_path.stat().st_dev,
                "file_id": residual_path.stat().st_ino,
                "size": residual_path.stat().st_size,
                "mtime_ns": residual_path.stat().st_mtime_ns,
                "birthtime_ns": getattr(residual_path.stat(), "st_birthtime_ns", -1),
            },
            "mime_identity": {
                "path": str(residual_path),
                "mime": "application/octet-stream",
                "physical_identity": {
                    "volume_id": residual_path.stat().st_dev,
                    "file_id": residual_path.stat().st_ino,
                    "size": residual_path.stat().st_size,
                    "mtime_ns": residual_path.stat().st_mtime_ns,
                    "birthtime_ns": getattr(residual_path.stat(), "st_birthtime_ns", -1),
                },
            },
        },
    )
    index = {classified_path: classified}
    residual_index = {residual_path: residual_record}
    inputs = CorpusVerificationInputs(
        classified_lookup=lambda path: index.get(path),
        residual_lookup=lambda path: residual_index.get(path),
        current_paths=current_paths,
        actionable_junk=(),
        actionable_duplicates=(),
        producers_pending=(),
        policy_findings=(),
    )
    return root, inputs


def test_after_semantic_requires_every_survivor_and_owner_path(tmp_path: Path) -> None:
    root, inputs = _fixture(tmp_path)
    result = verify_after_semantic(root, inputs, min_confidence=0.72)
    assert result.passed
    assert result.status == "complete"
    assert result.metrics["final_files"] == 2
    assert result.metrics["mime_buckets"] == 1
    assert result.unaccounted_paths == ()


def test_topology_and_missing_evidence_fail_closed(tmp_path: Path) -> None:
    root, inputs = _fixture(tmp_path)
    stray = root / "stray.txt"
    stray.write_text("not allowed", encoding="utf-8")
    good = inputs.classified_lookup
    assert good is not None
    path = root / "Corpus_ordenado" / "PDF" / "informe.pdf"
    record = good(path)
    assert record is not None
    broken = ClassifiedSurvivor(
        path=record.path,
        classification=record.classification,
        confidence=record.confidence,
        evidence={},
        organization_root=record.organization_root,
        receipt=record.receipt,
    )
    inputs = CorpusVerificationInputs(
        classified_lookup=lambda candidate: broken if candidate == path else None,
        residual_lookup=inputs.residual_lookup,
        current_paths=inputs.current_paths,
    )
    result = verify_after_semantic(root, inputs, min_confidence=0.72)
    codes = {issue.code for issue in result.failures}
    assert not result.passed
    assert "unexpected_top_level" in codes
    assert "classification_evidence_missing" in codes


def test_residual_bucket_and_mime_are_checked(tmp_path: Path) -> None:
    root, inputs = _fixture(tmp_path)
    path = root / "Sin_clasificar" / "_MIME" / "application" / "octet-stream" / "sin-tipo"
    broken = ResidualSurvivor(path=path, mime="text/plain", evidence={"detector": "bad"})
    inputs = CorpusVerificationInputs(
        classified_lookup=inputs.classified_lookup,
        residual_lookup=lambda candidate: broken if candidate == path else None,
        current_paths=inputs.current_paths,
    )
    result = verify_after_semantic(root, inputs, min_confidence=0.72)
    assert not result.passed
    assert any(issue.code == "residual_mime_bucket_mismatch" for issue in result.failures)


def test_allowed_blocked_finding_is_explicit_but_does_not_hide_it(tmp_path: Path) -> None:
    root, inputs = _fixture(tmp_path)
    finding = PendingFinding("junk", "protected", root / "Sin_clasificar" / "_MIME" / "application" / "octet-stream" / "sin-tipo")
    inputs = CorpusVerificationInputs(
        classified_lookup=inputs.classified_lookup,
        residual_lookup=inputs.residual_lookup,
        current_paths=inputs.current_paths,
        actionable_junk=(finding,),
    )
    result = verify_after_semantic(root, inputs, min_confidence=0.72)
    assert result.passed
    assert any(issue.code == "allowed_exception" for issue in result.warnings)
    assert result.metrics["blocked"] == 1


def test_empty_non_structural_directory_and_stale_current_path_block(tmp_path: Path) -> None:
    root, inputs = _fixture(tmp_path)
    (root / "Corpus_ordenado" / "empty").mkdir()
    paths = tuple(
        OwnerCurrentPath(item.owner, Path("/definitely/missing/current"))
        if item.owner == "semantic"
        else item
        for item in inputs.current_paths
    )
    inputs = CorpusVerificationInputs(inputs.classified_lookup, inputs.residual_lookup, paths)
    result = verify_after_semantic(root, inputs, min_confidence=0.72)
    codes = {issue.code for issue in result.failures}
    assert "unexpected_empty_directory" in codes
    assert "owner_current_path_stale" in codes


def test_before_semantic_has_a_smaller_owner_frontier(tmp_path: Path) -> None:
    root, inputs = _fixture(tmp_path)
    paths = tuple(item for item in inputs.current_paths if item.owner != "semantic")
    inputs = CorpusVerificationInputs(inputs.classified_lookup, inputs.residual_lookup, paths)
    result = verify_before_semantic(root, inputs, min_confidence=0.72)
    assert result.passed
    assert all(not value.startswith("semantic:") for value in result.owner_paths_checked)


def test_after_semantic_missing_semantic_owner_is_not_skipped(tmp_path: Path) -> None:
    root, inputs = _fixture(tmp_path)
    paths = tuple(item for item in inputs.current_paths if item.owner != "semantic")
    inputs = CorpusVerificationInputs(inputs.classified_lookup, inputs.residual_lookup, paths)
    result = verify_after_semantic(root, inputs, min_confidence=0.72)
    assert not result.passed
    assert any(issue.code == "owner_current_paths_missing" and issue.owner == "semantic" for issue in result.failures)


def test_bounded_scan_is_partial_not_approved(tmp_path: Path) -> None:
    root, inputs = _fixture(tmp_path)
    residual_path = root / "Sin_clasificar" / "_MIME" / "application" / "octet-stream"
    for index_value in range(4):
        path = residual_path / f"extra-{index_value}"
        path.write_bytes(b"x")
    result = CorpusVerifier(max_files=2).verify(root, inputs, phase="after_semantic", min_confidence=0.72)
    assert not result.passed
    assert result.status == "partial"
    assert result.coverage == "partial"


def test_no_sqlite_or_mutation_is_needed(tmp_path: Path) -> None:
    root, inputs = _fixture(tmp_path)
    before = sorted(path.relative_to(tmp_path) for path in root.rglob("*"))
    result = verify_after_semantic(root, inputs, min_confidence=0.72)
    after = sorted(path.relative_to(tmp_path) for path in root.rglob("*"))
    assert result.passed
    assert before == after


def test_owner_current_identity_change_is_not_accepted_by_path_existence(tmp_path: Path) -> None:
    root, inputs = _fixture(tmp_path)
    semantic_path = next(item.path for item in inputs.current_paths if item.owner == "semantic")
    assert semantic_path is not None
    Path(semantic_path).write_text("changed", encoding="utf-8")
    result = verify_after_semantic(root, inputs, min_confidence=0.72)
    assert not result.passed
    assert any(issue.code == "owner_current_identity_stale" for issue in result.failures)


def test_classified_receipt_target_cannot_cross_bind_to_another_path(tmp_path: Path) -> None:
    root, inputs = _fixture(tmp_path)
    path = root / "Corpus_ordenado" / "PDF" / "informe.pdf"
    record = inputs.classified_lookup(path)
    assert record is not None
    other = root / "Corpus_ordenado" / "PDF" / "other.pdf"
    other.write_bytes(b"other")
    bad = replace(record, receipt=_receipt(root / "source.pdf", other))
    inputs = replace(
        inputs,
        classified_lookup=lambda candidate: bad if candidate == path else None,
    )
    result = verify_after_semantic(root, inputs, min_confidence=0.72)
    assert not result.passed
    assert any(issue.code == "organization_receipt_target_mismatch" for issue in result.failures)


def test_receipt_requires_owner_schema_and_identity() -> None:
    with pytest.raises(ValueError):
        OrganizationMoveReceipt.from_json(
            json.dumps(
                {
                    "organization_receipt_schema": "neocortex.organization-move-receipt/v1",
                    "source_path": "/corpus/a",
                    "target_path": "/corpus/b",
                    "source_absent": True,
                    "target_identity": {"path": "/corpus/b"},
                }
            ),
            owner="document_organization",
            status="applied",
        )


def test_unbound_hash_is_not_classification_evidence(tmp_path: Path) -> None:
    root, inputs = _fixture(tmp_path)
    path = root / "Corpus_ordenado" / "PDF" / "informe.pdf"
    record = inputs.classified_lookup(path)
    assert record is not None
    broken = ClassifiedSurvivor(
        path=record.path,
        classification=record.classification,
        confidence=record.confidence,
        evidence="sha256:unbound-input",
        organization_root=record.organization_root,
        receipt=record.receipt,
    )
    inputs = CorpusVerificationInputs(
        classified_lookup=lambda candidate: broken if candidate == path else None,
        residual_lookup=inputs.residual_lookup,
        current_paths=inputs.current_paths,
    )
    result = verify_after_semantic(root, inputs, min_confidence=0.72)
    assert not result.passed
    assert any(issue.code == "classification_evidence_missing" for issue in result.failures)
