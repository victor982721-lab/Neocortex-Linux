"""Focused contract checks for the opt-in Knowledge search projection API."""

from __future__ import annotations

import inspect
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

from neocortex.api import public, read_api
from neocortex.knowledge import knowledge_evidence_projection
from neocortex.knowledge.knowledge_read_budget import KnowledgeReadBudget


def test_projection_api_is_lazy_and_manifested() -> None:
    completed = subprocess.run(
        (
            sys.executable,
            "-B",
            "-c",
            textwrap.dedent(
                """
                import sys
                from neocortex.api import public
                import neocortex.sdk as sdk

                if "neocortex.api.read_api" in sys.modules:
                    raise SystemExit("read_api imported before projection access")
                if "knowledge_search_projection_payload" not in public.__all__:
                    raise SystemExit("public projection export missing")
                if "knowledge_search_projection_payload" not in sdk.__all__:
                    raise SystemExit("SDK projection export missing")
                value = public.knowledge_search_projection_payload
                if value is not sdk.knowledge_search_projection_payload:
                    raise SystemExit("facades do not preserve identity")
                if "neocortex.api.read_api" not in sys.modules:
                    raise SystemExit("projection did not resolve read_api lazily")
                print("PROJECTION_FACADE_LAZY_OK")
                """
            ),
        ),
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "PROJECTION_FACADE_LAZY_OK"


def test_projection_api_matches_canonical_signature() -> None:
    canonical = read_api.knowledge_search_projection_payload
    assert public.knowledge_search_projection_payload is canonical
    import neocortex.sdk as sdk

    assert sdk.knowledge_search_projection_payload is canonical
    assert (
        inspect.signature(public.knowledge_search_projection_payload)
        == inspect.signature(canonical)
    )
    assert (
        inspect.signature(sdk.knowledge_search_projection_payload)
        == inspect.signature(canonical)
    )


def test_projection_api_preserves_scope_results_and_forwards_budget(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(read_api, "default_state_directory", lambda: tmp_path)
    monkeypatch.setattr(
        read_api,
        "_read_epoch_for_bindings",
        lambda bindings, selected: {"scope": selected.value, "status": "observed"},
    )

    budget = KnowledgeReadBudget(max_rows=17)
    calls: list[tuple[str, object]] = []

    class FakeService:
        def search(self, request, *, cancellation_check=None, read_budget=None):
            assert request.text == "relay"
            assert request.limit == 3
            calls.append((request.retrieval_mode.value, read_budget))
            return SimpleNamespace(complete=True)

    monkeypatch.setattr(read_api, "_service", lambda binding: FakeService())
    monkeypatch.setattr(read_api, "knowledge_search_exit_code", lambda result: 0)

    def project(result, *, scope, read_budget):
        assert result.complete is True
        return {
            "schema": "neocortex.knowledge-evidence-projection/v1",
            "scope": scope,
            "snapshot": {"status": "captured"},
            "coverage": {"answer_sufficiency": "not_assessed"},
            "read_budget": read_budget.to_dict(),
        }

    monkeypatch.setattr(knowledge_evidence_projection, "project_knowledge_search", project)
    payload = read_api.knowledge_search_projection_payload(
        " relay ",
        "personal",
        limit=3,
        mode="discovery",
        request_id="projection-test",
        read_budget=budget,
    )

    assert payload["schema"] == "neocortex.read-api/v1"
    assert payload["kind"] == "neocortex_scoped_search"
    assert payload["operation"] == "search"
    assert payload["read_only"] is True
    assert payload["projection"] == "neocortex.knowledge-evidence-projection/v1"
    assert payload["read_budget"] == budget.to_dict()
    assert payload["result"]["scopes"] == payload["scopes"]
    assert len(payload["scopes"]) == 1
    entry = payload["scopes"][0]
    assert entry["scope"] == "personal"
    assert entry["result"]["schema"] == "neocortex.knowledge-evidence-projection/v1"
    assert entry["result"]["scope"] == "personal"
    assert entry["result"]["snapshot"] == {"status": "captured"}
    assert entry["result"]["coverage"]["answer_sufficiency"] == "not_assessed"
    assert entry["result"]["read_budget"] == budget.to_dict()
    assert calls == [("discovery", budget)]
