"""Public API/CLI fail-closed checks for the 0.11 application slice."""

from __future__ import annotations

import json

from neocortex.api.curation_application_api import (
    curation_apply_payload,
    curation_reconcile_payload,
)
from neocortex.api.cli.human import run_human_command


def test_public_apply_requires_exact_confirmation_before_backend() -> None:
    payload = curation_apply_payload(
        "grant-fixture",
        confirm_grant_id="other-grant",
        request_id="apply-confirmation",
    )
    assert payload["status"] == "blocked"
    assert payload["error"]["code"] == "invalid_request"  # type: ignore[index]
    assert payload["exit_code"] == 2


def test_public_apply_without_injected_backend_is_fail_closed(capsys) -> None:
    exit_code = run_human_command(
        (
            "curate",
            "apply",
            "grant-fixture",
            "--confirm-grant-id",
            "grant-fixture",
            "--json",
        )
    )
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert payload["error"]["code"] == "backend_unavailable"
    assert payload["effects"]["corpus"] == "none"


def test_reconcile_requires_an_explicit_confirmation() -> None:
    payload = curation_reconcile_payload(actor="victor", request_id="reconcile-no-confirm")
    assert payload["status"] == "blocked"
    assert payload["error"]["code"] == "invalid_request"  # type: ignore[index]
