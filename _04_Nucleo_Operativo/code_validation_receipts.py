"""Exact, durable receipts for the canonical source-change validation.

The receipt is intentionally a consumer-side contract.  It never starts an
analysis and it is reusable only while Git and the published Code review still
match the validation that produced it.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, Mapping, cast

from .app_paths import self_analysis_data_directory, source_repository_directory
from .semantic_models import canonical_json


CODE_VALIDATION_RECEIPT_SCHEMA = "neocortex.code-validation-receipt/v1"
_CODE_CHANGE_VALIDATION_SCHEMA = "neocortex.code-change-validation/v3"
_CODE_CHANGE_VALIDATION_POLICY = "local-linux-diff-aware-validation-v8"
_MAX_RECEIPT_BYTES = 8 * 1024 * 1024
_REQUIRED_GATE_STATUSES = {
    "clean_source_sha": frozenset({"passed"}),
    "static_no_regression": frozenset({"passed"}),
    "architecture_contracts": frozenset({"passed"}),
    "trusted_deep_publication": frozenset({"passed"}),
    "autoanalysis_verdict": frozenset({"passed"}),
    "affected_coverage": frozenset({"passed"}),
    "allowlisted_experiments": frozenset({"passed", "not_required"}),
    "candidate_wheel_smoke": frozenset({"passed"}),
    "trusted_deep_replay_publication": frozenset({"passed"}),
    "autoanalysis_replay_verdict": frozenset({"passed"}),
    "trusted_deep_replay": frozenset({"passed"}),
    "diff_bound_technical_dispositions": frozenset({"passed", "not_required"}),
    "source_snapshot_unchanged": frozenset({"passed"}),
}


@dataclass(frozen=True, slots=True)
class CodeValidationReceiptStatus:
    """Bounded result of attempting to reuse the current validation receipt."""

    status: Literal["reused", "missing", "stale"]
    reason: str
    receipt_path: str | None = None
    validation_digest: str | None = None
    head_sha: str | None = None


@dataclass(frozen=True, slots=True)
class CodeValidationReceiptPublication:
    """Identity of one atomically published validation receipt."""

    receipt_path: str
    receipt_digest: str
    validation_digest: str
    head_sha: str


class CodeValidationReceiptError(RuntimeError):
    """Raised when a passing validation cannot be published safely."""


def _digest_payload(payload: Mapping[str, object]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _validation_digest(result: Mapping[str, object]) -> str:
    values = dict(result)
    values.pop("schema", None)
    claimed = values.pop("digest", None)
    actual = _digest_payload(values)
    if claimed != actual:
        raise CodeValidationReceiptError("validation_result_digest_mismatch")
    return actual


def _validated_result(payload: Mapping[str, object]) -> tuple[Mapping[str, object], str]:
    if payload.get("schema") != CODE_VALIDATION_RECEIPT_SCHEMA:
        raise CodeValidationReceiptError("receipt_schema_mismatch")
    claimed_receipt_digest = payload.get("receipt_digest")
    receipt_values = dict(payload)
    receipt_values.pop("receipt_digest", None)
    if claimed_receipt_digest != _digest_payload(receipt_values):
        raise CodeValidationReceiptError("receipt_digest_mismatch")
    result = payload.get("result")
    if not isinstance(result, Mapping):
        raise CodeValidationReceiptError("receipt_result_missing")
    result = cast(Mapping[str, object], result)
    if result.get("schema") != _CODE_CHANGE_VALIDATION_SCHEMA:
        raise CodeValidationReceiptError("validation_schema_mismatch")
    if result.get("policy_id") != _CODE_CHANGE_VALIDATION_POLICY:
        raise CodeValidationReceiptError("validation_policy_mismatch")
    if result.get("status") != "passed" or result.get("reason") is not None:
        raise CodeValidationReceiptError("validation_not_passed")
    if result.get("source_unchanged") is not True:
        raise CodeValidationReceiptError("validation_source_not_immutable")
    if result.get("authority") != "validation" or result.get("mutation_authority") is not False:
        raise CodeValidationReceiptError("validation_authority_invalid")
    validation_digest = _validation_digest(result)
    gates = result.get("gates")
    if not isinstance(gates, list):
        raise CodeValidationReceiptError("validation_gates_missing")
    by_id: dict[str, Mapping[str, object]] = {}
    for raw_gate in gates:
        if not isinstance(raw_gate, Mapping) or not isinstance(raw_gate.get("gate_id"), str):
            raise CodeValidationReceiptError("validation_gate_invalid")
        gate = cast(Mapping[str, object], raw_gate)
        gate_id = cast(str, gate["gate_id"])
        if gate_id in by_id:
            raise CodeValidationReceiptError("validation_gate_duplicate")
        by_id[gate_id] = gate
    missing = sorted(_REQUIRED_GATE_STATUSES.keys() - by_id.keys())
    if missing:
        raise CodeValidationReceiptError("validation_required_gates_missing:" + ",".join(missing))
    not_accepted = sorted(
        gate_id
        for gate_id, statuses in _REQUIRED_GATE_STATUSES.items()
        if by_id[gate_id].get("status") not in statuses
    )
    if not_accepted:
        raise CodeValidationReceiptError(
            "validation_required_gates_not_accepted:" + ",".join(not_accepted)
        )
    autoanalysis = by_id["autoanalysis_verdict"].get("evidence")
    if (
        not isinstance(autoanalysis, Mapping)
        or autoanalysis.get("external_profile") != "trusted-deep"
        or autoanalysis.get("missing_providers") != []
        or autoanalysis.get("provider_failures") != []
        or autoanalysis.get("failed_supply_gates") != []
    ):
        raise CodeValidationReceiptError("validation_supply_boundary_not_proven")
    return result, validation_digest


def _read_receipt(path: Path) -> Mapping[str, object]:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise CodeValidationReceiptError("receipt_missing") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise CodeValidationReceiptError("receipt_not_regular")
    if metadata.st_size < 2 or metadata.st_size > _MAX_RECEIPT_BYTES:
        raise CodeValidationReceiptError("receipt_size_invalid")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CodeValidationReceiptError("receipt_unreadable") from exc
    if not isinstance(payload, Mapping):
        raise CodeValidationReceiptError("receipt_not_object")
    return cast(Mapping[str, object], payload)


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def publish_code_validation_receipt(
    result_payload: Mapping[str, object],
) -> CodeValidationReceiptPublication:
    """Publish a passing, clean, committed validation result atomically."""

    provisional: dict[str, object] = {
        "schema": CODE_VALIDATION_RECEIPT_SCHEMA,
        "result": dict(result_payload),
    }
    provisional["receipt_digest"] = _digest_payload(provisional)
    result, validation_digest = _validated_result(provisional)
    git = result.get("git")
    if not isinstance(git, Mapping):
        raise CodeValidationReceiptError("validation_git_snapshot_missing")
    head = git.get("head_sha")
    baseline = git.get("baseline")
    changed = git.get("changed_paths")
    if not isinstance(head, str) or not isinstance(baseline, str) or head == baseline:
        raise CodeValidationReceiptError("validation_commit_range_invalid")
    if not isinstance(changed, list) or not changed:
        raise CodeValidationReceiptError("validation_change_set_empty")
    for field in ("staged_paths", "unstaged_paths", "untracked_paths"):
        if git.get(field) != []:
            raise CodeValidationReceiptError(f"validation_source_not_clean:{field}")
    source = Path(cast(str, result.get("source_root"))).resolve(strict=True)
    state = Path(cast(str, result.get("state_directory"))).expanduser().resolve(strict=False)
    canonical_source = source_repository_directory().resolve(strict=True)
    canonical_state = self_analysis_data_directory().expanduser().resolve(strict=False)
    if source != canonical_source or state != canonical_state:
        raise CodeValidationReceiptError("validation_paths_not_canonical")
    receipt_directory = state / "validation-receipts"
    filename = f"{head}-{validation_digest.removeprefix('sha256:')}.json"
    immutable_path = receipt_directory / filename
    encoded = (canonical_json(provisional) + "\n").encode("utf-8")
    if len(encoded) > _MAX_RECEIPT_BYTES:
        raise CodeValidationReceiptError("receipt_size_bound_exceeded")
    if immutable_path.exists():
        if _read_receipt(immutable_path) != provisional:
            raise CodeValidationReceiptError("immutable_receipt_collision")
    else:
        _atomic_write(immutable_path, encoded)
    _atomic_write(receipt_directory / "latest.json", encoded)
    return CodeValidationReceiptPublication(
        str(immutable_path),
        cast(str, provisional["receipt_digest"]),
        validation_digest,
        head,
    )


def _current_review_matches(result: Mapping[str, object], state: Path) -> bool:
    gates = cast(list[Mapping[str, object]], result["gates"])
    replay_gate = next(
        (gate for gate in gates if gate.get("gate_id") == "autoanalysis_replay_verdict"),
        None,
    )
    evidence = None if replay_gate is None else replay_gate.get("evidence")
    expected = evidence.get("digest") if isinstance(evidence, Mapping) else None
    if not isinstance(expected, Mapping):
        return False
    from .code_review import review_code_state

    review = review_code_state(state, limit=50)
    if review.status != "ready" or review.digest is None:
        return False
    return asdict(review.digest) == dict(expected)


def load_current_code_validation_receipt(
    *,
    source_root: Path | None = None,
    state_directory: Path | None = None,
) -> CodeValidationReceiptStatus:
    """Reuse the latest receipt only when source and publication are exact."""

    source = (source_repository_directory() if source_root is None else Path(source_root)).resolve(
        strict=True
    )
    state = (
        (self_analysis_data_directory() if state_directory is None else Path(state_directory))
        .expanduser()
        .resolve(strict=False)
    )
    path = state / "validation-receipts" / "latest.json"
    if not path.exists():
        return CodeValidationReceiptStatus("missing", "receipt_missing", str(path))
    try:
        payload = _read_receipt(path)
        result, validation_digest = _validated_result(payload)
        if Path(cast(str, result.get("source_root"))).resolve(strict=True) != source:
            raise CodeValidationReceiptError("receipt_source_root_mismatch")
        if (
            Path(cast(str, result.get("state_directory"))).expanduser().resolve(strict=False)
            != state
        ):
            raise CodeValidationReceiptError("receipt_state_directory_mismatch")
        git = result.get("git")
        if not isinstance(git, Mapping):
            raise CodeValidationReceiptError("validation_git_snapshot_missing")
        baseline = git.get("baseline")
        if not isinstance(baseline, str):
            raise CodeValidationReceiptError("validation_baseline_missing")
        from .code_change_validation import capture_git_change

        current = capture_git_change(source, baseline=baseline)
        if current.head_sha != git.get("head_sha"):
            raise CodeValidationReceiptError("receipt_head_changed")
        if current.baseline != baseline:
            raise CodeValidationReceiptError("receipt_baseline_changed")
        if list(current.changed_paths) != git.get("changed_paths"):
            raise CodeValidationReceiptError("receipt_change_set_changed")
        if current.content_digest != git.get("content_digest"):
            raise CodeValidationReceiptError("receipt_content_changed")
        if current.staged_paths or current.unstaged_paths or current.untracked_paths:
            raise CodeValidationReceiptError("receipt_worktree_not_clean")
        if not _current_review_matches(result, state):
            raise CodeValidationReceiptError("receipt_publication_displaced")
        return CodeValidationReceiptStatus(
            "reused",
            "exact_validation_receipt_reused",
            str(path),
            validation_digest,
            current.head_sha,
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        reason = str(exc) if isinstance(exc, CodeValidationReceiptError) else type(exc).__name__
        return CodeValidationReceiptStatus("stale", reason, str(path))


__all__ = [
    "CODE_VALIDATION_RECEIPT_SCHEMA",
    "CodeValidationReceiptError",
    "CodeValidationReceiptPublication",
    "CodeValidationReceiptStatus",
    "load_current_code_validation_receipt",
    "publish_code_validation_receipt",
]
