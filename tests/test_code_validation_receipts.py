"""Exact receipt publication and reuse contracts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from _04_Nucleo_Operativo import code_change_validation, code_validation_receipts
from _04_Nucleo_Operativo.code_change_validation import (
    AffectedTestSelection,
    CODE_CHANGE_VALIDATION_POLICY,
    CodeChangeValidationResult,
    GitChangeSnapshot,
    ValidationGate,
    _build_result,
)
from _04_Nucleo_Operativo.code_validation_receipts import (
    CodeValidationReceiptError,
    load_current_code_validation_receipt,
    publish_code_validation_receipt,
)
from _04_Nucleo_Operativo.code_validation_public_review import (
    VALIDATION_STABLE_PUBLIC_REVIEW_SCHEMA,
    code_review_identity,
    validation_stable_public_review_identity,
    validation_stable_review_identity,
)
from _04_Nucleo_Operativo.semantic_models import canonical_json


_REQUIRED_GATES = (
    "clean_source_sha",
    "pip_audit_snapshot_preflight",
    "static_no_regression",
    "architecture_contracts",
    "trusted_deep_publication",
    "autoanalysis_verdict",
    "affected_coverage",
    "candidate_wheel_smoke",
    "trusted_deep_replay_publication",
    "autoanalysis_replay_verdict",
    "trusted_deep_replay",
    "diff_bound_technical_dispositions",
    "public_review_stability",
    "source_snapshot_unchanged",
)


@dataclass(frozen=True)
class _ReviewDigest:
    value: str


def _review_result(value: str) -> SimpleNamespace:
    return SimpleNamespace(
        status="ready",
        reason=None,
        snapshot=SimpleNamespace(
            analysis_run_id=17,
            processing_signature="snapshot:fixture",
            freshness="publication_only",
        ),
        digest=_ReviewDigest(value),
        external_evidence_suite=SimpleNamespace(profile="trusted-deep", providers=()),
        experiment_receipts=(),
        question_evaluations=(),
        materialization_limit=50,
        mutation_authority=False,
    )


def _public_review_evidence(value: str) -> dict[str, object]:
    public = code_review_identity(_review_result(value))
    return {
        "public_identity": public,
        "validation_stable_identity": validation_stable_public_review_identity(public),
    }


def _result(root: Path, state: Path) -> dict[str, object]:
    review_digest = {"value": "published-review"}
    gates = [
        {
            "gate_id": gate_id,
            "status": "passed",
            "reason": "passed",
            "duration_ms": 1,
            "command": [],
            "evidence": (
                _public_review_evidence("published-review")
                if gate_id == "public_review_stability"
                else {"digest": review_digest}
                if gate_id == "autoanalysis_replay_verdict"
                else {
                    "external_profile": "trusted-deep",
                    "missing_providers": [],
                    "provider_failures": [],
                    "failed_supply_gates": [],
                }
                if gate_id == "autoanalysis_verdict"
                else {}
            ),
        }
        for gate_id in _REQUIRED_GATES
    ]
    gates[-1]["reason"] = "source_unchanged"
    gates.insert(
        -1,
        {
            "gate_id": "allowlisted_experiments",
            "status": "not_required",
            "reason": "none_affected",
            "duration_ms": 0,
            "command": [],
            "evidence": {},
        },
    )
    payload: dict[str, object] = {
        "schema": "neocortex.code-change-validation/v3",
        "status": "passed",
        "reason": None,
        "policy_id": "local-linux-diff-aware-validation-v12",
        "source_root": str(root),
        "state_directory": str(state),
        "git": {
            "head_sha": "b" * 40,
            "baseline": "a" * 40,
            "changed_paths": ["module.py"],
            "untracked_paths": [],
            "staged_paths": [],
            "unstaged_paths": [],
            "content_digest": "c" * 64,
        },
        "selection": {},
        "gates": gates,
        "experiment_proposals": [],
        "executable_experiments": [],
        "experiment_receipts": [],
        "resource_boundary": None,
        "source_unchanged": True,
        "authority": "validation",
        "mutation_authority": False,
    }
    digest_values = dict(payload)
    digest_values.pop("schema")
    payload["digest"] = (
        "sha256:" + hashlib.sha256(canonical_json(digest_values).encode("utf-8")).hexdigest()
    )
    return payload


def _prepare(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Path, dict[str, object]]:
    root = tmp_path / "Repository"
    state = tmp_path / "state"
    root.mkdir()
    state.mkdir()
    payload = _result(root, state)
    monkeypatch.setattr(
        code_validation_receipts,
        "source_repository_directory",
        lambda: root,
    )
    monkeypatch.setattr(
        code_validation_receipts,
        "self_analysis_data_directory",
        lambda: state,
    )
    return root, state, payload


def _object_result(root: Path, state: Path) -> CodeChangeValidationResult:
    review_digest = {"value": "published-review"}
    gates = [
        ValidationGate(
            gate_id,
            "passed",
            "source_unchanged" if gate_id == "source_snapshot_unchanged" else "passed",
            1,
            (),
            (
                _public_review_evidence("published-review")
                if gate_id == "public_review_stability"
                else {"digest": review_digest}
                if gate_id == "autoanalysis_replay_verdict"
                else {
                    "external_profile": "trusted-deep",
                    "missing_providers": [],
                    "provider_failures": [],
                    "failed_supply_gates": [],
                }
                if gate_id == "autoanalysis_verdict"
                else {}
            ),
        )
        for gate_id in _REQUIRED_GATES
    ]
    gates.insert(
        -1,
        ValidationGate(
            "allowlisted_experiments",
            "not_required",
            "none_affected",
            0,
            (),
            {},
        ),
    )
    return _build_result(
        {
            "status": "passed",
            "reason": None,
            "policy_id": CODE_CHANGE_VALIDATION_POLICY,
            "source_root": str(root),
            "state_directory": str(state),
            "git": GitChangeSnapshot(
                "b" * 40,
                "a" * 40,
                ("module.py",),
                (),
                (),
                (),
                "c" * 64,
            ),
            "selection": AffectedTestSelection(
                strategy="affected",
                selectors=("tests/test_fixture.py",),
                direct_tests=("tests/test_fixture.py",),
                dependency_tests=(),
                convention_tests=(),
                uncovered_sources=(),
                reasons=("fixture_selection",),
            ),
            "gates": tuple(gates),
            "experiment_proposals": (),
            "executable_experiments": (),
            "experiment_receipts": (),
            "resource_boundary": None,
            "source_unchanged": True,
            "authority": "validation",
            "mutation_authority": False,
        }
    )


def test_real_validation_payload_crosses_the_receipt_publication_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, state, _payload = _prepare(tmp_path, monkeypatch)
    result = _object_result(root, state)

    publication = publish_code_validation_receipt(result.as_payload())
    stored = json.loads(Path(publication.receipt_path).read_text(encoding="utf-8"))

    assert stored["result"] == result.as_payload()


def test_validation_stable_reader_abstains_without_code_state(tmp_path: Path) -> None:
    identity = validation_stable_review_identity(tmp_path)

    assert identity["schema"] == VALIDATION_STABLE_PUBLIC_REVIEW_SCHEMA
    assert identity["status"] == "abstained"
    assert identity["reason"] == "code_state_missing"


def test_published_receipt_reuses_only_the_exact_clean_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, state, payload = _prepare(tmp_path, monkeypatch)
    git = payload["git"]
    assert isinstance(git, dict)
    monkeypatch.setattr(
        code_change_validation,
        "capture_git_change",
        lambda *_args, **_kwargs: SimpleNamespace(
            head_sha=git["head_sha"],
            baseline=git["baseline"],
            changed_paths=tuple(git["changed_paths"]),
            staged_paths=(),
            unstaged_paths=(),
            untracked_paths=(),
            content_digest=git["content_digest"],
        ),
    )
    monkeypatch.setattr(
        "_04_Nucleo_Operativo.code_validation_public_review.validation_stable_review_identity",
        lambda *_args, **_kwargs: validation_stable_public_review_identity(
            code_review_identity(_review_result("published-review"))
        ),
    )

    publication = publish_code_validation_receipt(payload)
    status = load_current_code_validation_receipt(
        source_root=root,
        state_directory=state,
    )

    assert Path(publication.receipt_path).is_file()
    assert Path(publication.receipt_path).name.startswith("b" * 40)
    assert status.status == "reused"
    assert status.validation_digest == payload["digest"]


def test_receipt_becomes_stale_when_head_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, state, payload = _prepare(tmp_path, monkeypatch)
    publish_code_validation_receipt(payload)
    monkeypatch.setattr(
        code_change_validation,
        "capture_git_change",
        lambda *_args, **_kwargs: SimpleNamespace(
            head_sha="d" * 40,
            baseline="a" * 40,
            changed_paths=("module.py",),
            staged_paths=(),
            unstaged_paths=(),
            untracked_paths=(),
            content_digest="c" * 64,
        ),
    )

    status = load_current_code_validation_receipt(
        source_root=root,
        state_directory=state,
    )

    assert status.status == "stale"
    assert status.reason == "receipt_head_changed"


def test_receipt_becomes_stale_when_review_publication_is_displaced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, state, payload = _prepare(tmp_path, monkeypatch)
    git = payload["git"]
    assert isinstance(git, dict)
    publish_code_validation_receipt(payload)
    monkeypatch.setattr(
        code_change_validation,
        "capture_git_change",
        lambda *_args, **_kwargs: SimpleNamespace(
            head_sha=git["head_sha"],
            baseline=git["baseline"],
            changed_paths=tuple(git["changed_paths"]),
            staged_paths=(),
            unstaged_paths=(),
            untracked_paths=(),
            content_digest=git["content_digest"],
        ),
    )
    displaced = _review_result("published-review")
    displaced.snapshot = SimpleNamespace(
        analysis_run_id=18,
        processing_signature="snapshot:displaced",
        freshness="publication_only",
    )
    monkeypatch.setattr(
        "_04_Nucleo_Operativo.code_validation_public_review.validation_stable_review_identity",
        lambda *_args, **_kwargs: validation_stable_public_review_identity(
            code_review_identity(displaced)
        ),
    )

    status = load_current_code_validation_receipt(
        source_root=root,
        state_directory=state,
    )

    assert status.status == "stale"
    assert status.reason == "receipt_publication_displaced"


def test_receipt_survives_operational_review_digest_and_experiment_visibility(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, state, payload = _prepare(tmp_path, monkeypatch)
    git = payload["git"]
    assert isinstance(git, dict)
    publish_code_validation_receipt(payload)
    monkeypatch.setattr(
        code_change_validation,
        "capture_git_change",
        lambda *_args, **_kwargs: SimpleNamespace(
            head_sha=git["head_sha"],
            baseline=git["baseline"],
            changed_paths=tuple(git["changed_paths"]),
            staged_paths=(),
            unstaged_paths=(),
            untracked_paths=(),
            content_digest=git["content_digest"],
        ),
    )
    advanced = _review_result("operationally-advanced-review")
    advanced.experiment_receipts = (
        SimpleNamespace(receipt=SimpleNamespace(receipt_id="receipt:operational")),
    )
    advanced.question_evaluations = (object(), object())
    monkeypatch.setattr(
        "_04_Nucleo_Operativo.code_validation_public_review.validation_stable_review_identity",
        lambda *_args, **_kwargs: validation_stable_public_review_identity(
            code_review_identity(advanced)
        ),
    )

    status = load_current_code_validation_receipt(
        source_root=root,
        state_directory=state,
    )

    assert status.status == "reused"
    assert status.reason == "exact_validation_receipt_reused"


def test_tampered_receipt_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, state, payload = _prepare(tmp_path, monkeypatch)
    publish_code_validation_receipt(payload)
    latest = state / "validation-receipts" / "latest.json"
    stored = json.loads(latest.read_text(encoding="utf-8"))
    stored["receipt_digest"] = "sha256:" + "0" * 64
    latest.write_text(canonical_json(stored) + "\n", encoding="utf-8")

    status = load_current_code_validation_receipt(
        source_root=root,
        state_directory=state,
    )

    assert status.status == "stale"
    assert status.reason == "receipt_digest_mismatch"


def test_missing_receipt_is_explicit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, state, _payload = _prepare(tmp_path, monkeypatch)

    status = load_current_code_validation_receipt(
        source_root=root,
        state_directory=state,
    )

    assert status.status == "missing"
    assert status.reason == "receipt_missing"


def test_dirty_validation_result_is_never_published(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _root, _state, payload = _prepare(tmp_path, monkeypatch)
    git = payload["git"]
    assert isinstance(git, dict)
    git["unstaged_paths"] = ["module.py"]
    digest_values = dict(payload)
    digest_values.pop("schema")
    digest_values.pop("digest")
    payload["digest"] = (
        "sha256:" + hashlib.sha256(canonical_json(digest_values).encode("utf-8")).hexdigest()
    )

    with pytest.raises(CodeValidationReceiptError, match="validation_source_not_clean"):
        publish_code_validation_receipt(payload)


def test_incomplete_supply_boundary_is_never_published(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _root, _state, payload = _prepare(tmp_path, monkeypatch)
    gates = payload["gates"]
    assert isinstance(gates, list)
    autoanalysis = next(item for item in gates if item["gate_id"] == "autoanalysis_verdict")
    autoanalysis["evidence"]["missing_providers"] = ["pip-audit"]
    digest_values = dict(payload)
    digest_values.pop("schema")
    digest_values.pop("digest")
    payload["digest"] = (
        "sha256:" + hashlib.sha256(canonical_json(digest_values).encode("utf-8")).hexdigest()
    )

    with pytest.raises(CodeValidationReceiptError, match="supply_boundary_not_proven"):
        publish_code_validation_receipt(payload)


def test_disjoint_technical_disposition_is_an_explicit_accepted_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _root, _state, payload = _prepare(tmp_path, monkeypatch)
    gates = payload["gates"]
    assert isinstance(gates, list)
    disposition = next(
        item for item in gates if item["gate_id"] == "diff_bound_technical_dispositions"
    )
    disposition["status"] = "not_required"
    disposition["reason"] = "no_validation_required_question_is_affected"
    digest_values = dict(payload)
    digest_values.pop("schema")
    digest_values.pop("digest")
    payload["digest"] = (
        "sha256:" + hashlib.sha256(canonical_json(digest_values).encode("utf-8")).hexdigest()
    )

    publication = publish_code_validation_receipt(payload)

    assert Path(publication.receipt_path).is_file()
