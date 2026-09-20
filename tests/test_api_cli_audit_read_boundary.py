"""Read envelopes reject ambiguous data and retain dependency diagnostics."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

import pytest

from neocortex.api import read_api
from neocortex.api.read_contract import (
    ReadContractError,
    ReadOperation,
    normalize_read_payload,
    sanitize_untrusted_payload,
    validate_read_payload,
)


TEST_CAPABILITIES = ("base",)


def _status_payload(code: int = 0) -> dict[str, object]:
    return normalize_read_payload(
        {"exit_code": code, "scopes": []},
        ReadOperation.STATUS,
        scope="personal",
        allow_legacy_identity=True,
    )


@pytest.mark.parametrize("code", [0, 3])
def test_success_or_empty_cannot_carry_a_nonnull_error(code: int) -> None:
    payload = _status_payload(code)
    payload["error"] = {"code": "error", "message": "producer failed"}
    with pytest.raises(ReadContractError, match="error"):
        validate_read_payload(payload, ReadOperation.STATUS, scope="personal")


@pytest.mark.parametrize(
    "keys",
    [
        (1, "1"),
        ("x\t", "x "),
        ("x", "\x1b[31mx\x1b[0m"),
        ("a" * 512 + "x", "a" * 512 + "y"),
        ("two  words", "two words"),
    ],
)
def test_payload_key_collisions_are_rejected_without_overwriting(
    keys: tuple[object, object],
) -> None:
    source = {keys[0]: "first", keys[1]: "second"}
    with pytest.raises(ReadContractError, match=r"keys.*collid"):
        sanitize_untrusted_payload({"nested": source})
    assert list(source.values()) == ["first", "second"]


def test_noncolliding_sanitized_keys_remain_supported() -> None:
    assert sanitize_untrusted_payload({1: "first", "x\t": "second"}) == {
        "1": "first",
        "x": "second",
    }


@pytest.mark.parametrize(
    ("operation", "function", "arguments", "reader"),
    [
        (ReadOperation.STATUS, "status_payload", (), "_service"),
        (ReadOperation.SEARCH, "search_payload", ("fixture",), "_service"),
        (ReadOperation.CONTEXT, "context_payload", ("fixture",), "_service"),
        (ReadOperation.EVIDENCE, "evidence_payload", ("fixture", "citation-1"), "_service"),
        (ReadOperation.LINEAGE, "lineage_payload", ("fixture",), "inspect_derivation_lineage"),
        (
            ReadOperation.ASSET_HEALTH,
            "asset_health_payload",
            ("resource:file:1:2:-1",),
            "inspect_knowledge_asset_health",
        ),
    ],
)
def test_each_published_reader_reports_missing_dependency_in_its_envelope(
    operation: ReadOperation,
    function: str,
    arguments: tuple[str, ...],
    reader: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state = tmp_path / "absent-state"
    bindings = (read_api.ScopeBinding(read_api.ReadScope.PERSONAL, state),)
    monkeypatch.setattr(read_api, "scope_bindings", lambda _scope: bindings)

    def missing(*_args: object, **_kwargs: object) -> Any:
        raise ModuleNotFoundError("No module named 'xxhash'", name="xxhash")

    monkeypatch.setattr(read_api, reader, missing)
    payload = getattr(read_api, function)(*arguments, scope="personal")
    validate_read_payload(payload, operation, scope="personal")
    assert payload["exit_code"] == 1
    assert payload["error"] is not None
    assert payload["read_only"] is True
    entry = payload["scopes"][0]
    assert entry["exit_code"] == 1
    assert "xxhash" in entry["reason"]
    assert "declared dependencies" in entry["reason"]
    assert not state.exists()


def test_sha256_backend_is_available_in_a_fresh_process(
    tmp_path: Path,
) -> None:
    # Verify the canonical stdlib backend in a fresh interpreter without
    # changing the canonical venv or creating live state.
    script = """
import json
from pathlib import Path
import sys

from neocortex.foundation.hash_compat import (
    HASH_BACKEND,
    sha256,
)
assert HASH_BACKEND == "hashlib"
payload = b"sha256-payload" * 128
one_shot = sha256.sha256_128_hexdigest(payload)
incremental = sha256.sha256_128()
incremental.update(payload[:17])
incremental.update(payload[17:])
assert incremental.hexdigest() == one_shot
assert len(one_shot) == 32
assert len(sha256.sha256_64_hexdigest(payload, seed=1)) == 16
from neocortex.api import read_api
from neocortex.api.read_contract import ReadOperation, validate_read_payload

state = Path(sys.argv[1])
read_api.scope_bindings = lambda _scope: (
    read_api.ScopeBinding(read_api.ReadScope.PERSONAL, state),
)
outcomes = {}
for operation, arguments in (
    ("status", ()),
    ("search", ("fixture",)),
    ("context", ("fixture",)),
    ("evidence", ("fixture", "C1")),
    ("lineage", ("fixture",)),
    ("asset_health", ("resource:file:1:2:-1",)),
):
    payload = getattr(read_api, operation + "_payload")(*arguments, scope="personal")
    validate_read_payload(payload, ReadOperation(operation), scope="personal")
    assert payload["read_only"] is True
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "ModuleNotFoundError" not in serialized
    assert "base_xxhash_unavailable" not in serialized
    outcomes[operation] = payload["exit_code"]
assert not state.exists()
print(json.dumps({"backend": HASH_BACKEND, "outcomes": outcomes}))
"""
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment.update(
        HOME=str(tmp_path / "home"),
        XDG_STATE_HOME=str(tmp_path / "state"),
        XDG_DATA_HOME=str(tmp_path / "data"),
        XDG_CACHE_HOME=str(tmp_path / "cache"),
        XDG_CONFIG_HOME=str(tmp_path / "config"),
        PYTHONDONTWRITEBYTECODE="1",
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "absent-state")],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["backend"] == "hashlib"
    assert set(payload["outcomes"]) == {
        "status",
        "search",
        "context",
        "evidence",
        "lineage",
        "asset_health",
    }
    assert all(code in {0, 3, 4} for code in payload["outcomes"].values())
    assert not tuple(tmp_path.iterdir())
