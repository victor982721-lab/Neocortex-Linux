"""Parity checks for the lazy Python read facades."""

from __future__ import annotations

import inspect
import subprocess
import sys
import textwrap
from pathlib import Path

import neocortex.sdk as sdk
from neocortex.api import public
from neocortex.api import read_api


READ_PAYLOADS = (
    "status_payload",
    "search_payload",
    "context_payload",
    "evidence_payload",
    "operational_query_payload",
    "asset_health_payload",
    "code_search_payload",
    "lineage_payload",
    "knowledge_search_projection_payload",
)

_ENVELOPE_KINDS = {
    "status_payload": ("neocortex_scoped_status", "status"),
    "search_payload": ("neocortex_scoped_search", "search"),
    "context_payload": ("neocortex_scoped_context", "context"),
    "evidence_payload": ("neocortex_evidence", "evidence"),
    "operational_query_payload": (
        "neocortex_scoped_operational_query",
        "operational_query",
    ),
    "asset_health_payload": ("neocortex_scoped_asset_health", "asset_health"),
    "code_search_payload": ("neocortex_scoped_code_search", "inspect_code"),
    "lineage_payload": ("neocortex_scoped_derivation_lineage", "lineage"),
    "knowledge_search_projection_payload": ("neocortex_scoped_search", "search"),
}


def test_read_facades_are_manifested_without_eager_read_api_import() -> None:
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

                names = (
                    "status_payload",
                    "search_payload",
                    "context_payload",
                    "evidence_payload",
                    "operational_query_payload",
                    "asset_health_payload",
                    "code_search_payload",
                    "lineage_payload",
                    "knowledge_search_projection_payload",
                )
                if "neocortex.api.read_api" in sys.modules:
                    raise SystemExit("read_api imported before a facade access")
                if not set(names).issubset(public.__all__):
                    raise SystemExit("public manifest is incomplete")
                if not set(names).issubset(sdk.__all__):
                    raise SystemExit("SDK manifest is incomplete")
                getattr(public, "status_payload")
                if "neocortex.api.read_api" not in sys.modules:
                    raise SystemExit("public facade did not resolve read_api lazily")
                print("READ_FACADE_LAZY_OK")
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
    assert completed.stdout.strip() == "READ_FACADE_LAZY_OK"


def test_read_facades_preserve_canonical_identity_and_signatures() -> None:
    for name in READ_PAYLOADS:
        canonical = getattr(read_api, name)
        assert getattr(public, name) is canonical
        assert getattr(sdk, name) is canonical
        assert inspect.signature(getattr(public, name)) == inspect.signature(canonical)
        assert inspect.signature(getattr(sdk, name)) == inspect.signature(canonical)


def test_read_facades_preserve_typed_envelopes_without_state_mutation(
    monkeypatch,
    tmp_path: Path,
) -> None:
    state_directory = tmp_path / "published-state"
    monkeypatch.setattr(read_api, "default_state_directory", lambda: state_directory)

    arguments = {
        "status_payload": ("personal",),
        "search_payload": ("query", "personal"),
        "context_payload": ("query", "personal"),
        "evidence_payload": ("query", "citation", "personal"),
        "operational_query_payload": ("query", "personal"),
        "asset_health_payload": ("resource:file:1:2:-1", "personal"),
        "code_search_payload": ("query", "personal"),
        "lineage_payload": ("revision:fixture", "personal"),
        "knowledge_search_projection_payload": ("query", "personal"),
    }

    for index, facade in enumerate((public, sdk)):
        for name in READ_PAYLOADS:
            payload = getattr(facade, name)(
                *arguments[name],
                request_id=f"read-facade-{index}-{name}",
            )
            kind, operation = _ENVELOPE_KINDS[name]
            assert payload["schema"] == "neocortex.read-api/v1"
            assert payload["kind"] == kind
            assert payload["operation"] == operation
            assert payload["read_only"] is True
            assert isinstance(payload["result"], dict)
            assert isinstance(payload["scopes"], list)

    assert not state_directory.exists()
