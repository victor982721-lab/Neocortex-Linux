"""A retained route input never silently crosses an interest-policy change."""

from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path

import pytest

from neocortex.runtime.models import FrameworkConfig
from neocortex.runtime.orchestration.orchestrator import FrameworkOrchestrator
from neocortex.workflow.actions.corpus_admission import CorpusAdmissionPolicy


def _source(roots: tuple[Path, ...], scope: str = "projects") -> dict[str, object]:
    policy = CorpusAdmissionPolicy(roots, scope)
    return {"configuration": {
        "corpus_admission": policy.to_dict(),
        "corpus_admission_signature": policy.signature,
    }}


def test_same_normalized_interest_policy_is_replayable(tmp_path: Path) -> None:
    roots = (tmp_path / "owned-a", tmp_path / "owned-b")
    orchestrator = FrameworkOrchestrator(FrameworkConfig(code_project_roots=roots))
    state = SimpleNamespace(read_run_manifest=lambda _run: _source(tuple(reversed(roots))))
    orchestrator._require_source_admission_policy(state, 1)


@pytest.mark.parametrize(
    "change", ["roots", "scope", "generated", "vendored", "digest", "legacy", "missing"],
)
def test_changed_or_missing_policy_abstains_before_route_input_consumption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    roots = (tmp_path / "owned",)
    source = _source(roots)
    if change == "roots":
        source = _source((tmp_path / "foreign",))
    elif change == "scope":
        source = _source(roots, "broad")
    elif change in {"generated", "vendored"}:
        policy = CorpusAdmissionPolicy(
            roots, include_generated=change == "generated", include_vendored=change == "vendored",
        )
        source = {"configuration": {
            "corpus_admission": policy.to_dict(),
            "corpus_admission_signature": policy.signature,
        }}
    elif change == "digest":
        configuration = source["configuration"]
        assert isinstance(configuration, dict)
        configuration["corpus_admission_signature"] = "changed"
    elif change == "legacy":
        source = {"configuration": {}}
    state = SimpleNamespace(read_run_manifest=lambda _run: None if change == "missing" else source)
    orchestrator = FrameworkOrchestrator(FrameworkConfig(code_project_roots=roots))
    monkeypatch.setattr(orchestrator, "_route_only_source_run", lambda *_args: (1, 1))

    def no_routes(*_args: object) -> None:
        pytest.fail("route inputs were consumed despite a changed policy")

    monkeypatch.setattr(orchestrator, "_select_route_only_routes", no_routes)
    with pytest.raises(ValueError, match="corpus admission policy"):
        orchestrator._prepare_route_only_source(state, object())
