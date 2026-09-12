"""Public API/SDK envelope and delegation contracts for state reset."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import neocortex.sdk as sdk
from neocortex.api import public
from neocortex.api import state_reset


_DIGEST = "sha256:" + "a" * 64


@dataclass(frozen=True, slots=True)
class _Plan:
    scope: str
    plan_digest: str = _DIGEST

    def as_payload(self, *, mode: str = "preview") -> dict[str, object]:
        return {
            "schema": state_reset.STATE_RESET_SCHEMA,
            "mode": mode,
            "scope": self.scope,
            "state_directory": "/fixture/state",
            "plan_digest": self.plan_digest,
            "file_count": 1,
        }


@dataclass(frozen=True, slots=True)
class _Result:
    scope: str
    plan_digest: str = _DIGEST

    def as_payload(self, *, mode: str = "applied") -> dict[str, object]:
        return {
            "schema": state_reset.STATE_RESET_SCHEMA,
            "mode": mode,
            "scope": self.scope,
            "state_directory": "/fixture/state",
            "plan_digest": self.plan_digest,
            "verified": True,
        }


def _engine_spy(calls: list[tuple[str, object]]) -> SimpleNamespace:
    def plan_state_reset(
        state_directory: Path,
        *,
        scope: str,
    ) -> _Plan:
        calls.append(("plan", (state_directory, scope)))
        return _Plan(scope)

    def execute_state_reset(
        state_directory: Path,
        *,
        scope: str,
        backup_directory: Path | None,
        apply: bool,
        confirmation: str | None,
        plan_digest: str | None,
    ) -> _Result:
        calls.append(
            (
                "execute",
                (state_directory, scope, backup_directory, apply, confirmation, plan_digest),
            )
        )
        return _Result(scope, plan_digest or "")

    return SimpleNamespace(
        plan_state_reset=plan_state_reset,
        execute_state_reset=execute_state_reset,
    )


def test_public_and_sdk_exports_are_lazy_identity_preserving() -> None:
    assert state_reset.STATE_RESET_API_SCHEMA == "neocortex.state-reset/v1"
    assert state_reset.STATE_RESET_CONFIRMATION == "RESET_STATE"
    assert state_reset.STATE_RESET_SCOPES == ("runs", "runs-and-caches", "all")
    assert public.state_reset_payload is state_reset.state_reset_payload
    assert sdk.state_reset_payload is state_reset.state_reset_payload
    assert public.STATE_RESET_API_SCHEMA == state_reset.STATE_RESET_API_SCHEMA
    assert sdk.STATE_RESET_SCOPES == state_reset.STATE_RESET_SCOPES
    assert set(state_reset.__all__) <= set(dir(state_reset))
    assert set(public.__all__) <= set(dir(public))
    assert set(sdk.__all__) <= set(dir(sdk))


def test_preview_delegates_only_to_plan_and_preserves_selected_scope(
    monkeypatch,
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(state_reset, "_engine", lambda: _engine_spy(calls))
    state_directory = tmp_path / "state"
    state_directory.mkdir()

    payload = state_reset.state_reset_payload(
        state_directory,
        scope="runs-and-caches",
        request_id="fixture-preview",
    )

    assert payload["schema"] == state_reset.STATE_RESET_SCHEMA
    assert payload["kind"] == "state-reset"
    assert payload["operation"] == "state-reset"
    assert payload["request_id"] == "fixture-preview"
    assert payload["scope"] == "runs-and-caches"
    assert payload["read_only"] is True
    assert payload["status"] == "preview"
    result = payload["result"]
    assert isinstance(result, dict)
    assert result["mode"] == "preview"
    assert result["plan_digest"] == _DIGEST
    assert result["requires_confirmation"] is True
    assert calls == [("plan", (state_directory, "runs-and-caches"))]


def test_apply_requires_exact_token_and_digest_before_engine_execution(
    monkeypatch,
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(state_reset, "_engine", lambda: _engine_spy(calls))
    state_directory = tmp_path / "state"
    state_directory.mkdir()

    missing_token = state_reset.state_reset_payload(
        state_directory,
        scope="runs",
        apply=True,
        plan_digest=_DIGEST,
    )
    assert missing_token["status"] == "error"
    assert missing_token["read_only"] is False
    assert missing_token["error"]["code"] == "invalid_request"
    assert calls == []

    preview = state_reset.state_reset_payload(state_directory, scope="runs")
    assert preview["status"] == "preview"

    applied = state_reset.state_reset_payload(
        state_directory,
        scope="runs",
        apply=True,
        confirmation="RESET_STATE",
        plan_digest=_DIGEST,
        backup_directory=tmp_path / "backup",
        request_id="fixture-apply",
    )

    assert applied["status"] == "complete"
    assert applied["read_only"] is False
    assert applied["request_id"] == "fixture-apply"
    result = applied["result"]
    assert isinstance(result, dict)
    assert result["mode"] == "applied"
    assert result["verified"] is True
    assert calls[-1] == (
        "execute",
        (
            state_directory,
            "runs",
            tmp_path / "backup",
            True,
            "RESET_STATE",
            _DIGEST,
        ),
    )


def test_invalid_preview_controls_never_load_or_call_engine(monkeypatch, tmp_path: Path) -> None:
    calls: list[str] = []

    def unavailable_engine() -> object:
        calls.append("engine")
        raise AssertionError("invalid request loaded the reset engine")

    monkeypatch.setattr(state_reset, "_engine", unavailable_engine)
    payload = state_reset.state_reset_payload(
        tmp_path / "state",
        scope="not-a-scope",  # type: ignore[arg-type]
        confirmation="RESET_STATE",
    )

    assert payload["status"] == "error"
    assert payload["read_only"] is True
    assert payload["error"]["code"] == "invalid_request"
    assert calls == []
