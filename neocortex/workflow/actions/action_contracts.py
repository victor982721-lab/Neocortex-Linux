"""Small shared contracts for workflow action mixins."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

TRASH_BATCH_SIZE = 256
ReserveWork = Callable[[str, int, int], None]
CONTENT_PREFIX_BYTES = 64 * 1024
REDLIST_REASON_EXAMPLE_LIMIT = 24
REDLIST_REASON_CODE_LIMIT = 64
DetectionKey = tuple[int, int, int, int, int]
IDENTIFY_PROGRESS_ITEM_STEP = 100
IDENTIFY_PROGRESS_INTERVAL_NS = 100_000_000


def _redlist_reason_code(value: object) -> str:
    """Map an untrusted action diagnostic to one bounded reason code.

    Redlist diagnostics are persisted in the Framework owner.  Do not copy
    backend messages (which may contain paths, helper output, or arbitrary
    bytes) into the durable counters.  The detailed message remains owned by
    the file-action row when that row crossed a real frontier.
    """

    text = str(value or "").casefold()
    known = (
        "destination_exists",
        "outside_root",
        "protected_content",
        "internal_path",
        "symbolic_link",
        "reparse",
        "source_disappeared",
        "metadata_changed",
        "identity_drift",
        "backend_unavailable",
        "preflight_failed",
        "effect_ambiguous",
        "receipt_invalid",
        "receipt_missing",
        "source_changed",
        "cancelled",
        "budget_exhausted",
    )
    aliases = {
        "destination exists": "destination_exists",
        "outside": "outside_root",
        "escapes root": "outside_root",
        "protected content": "protected_content",
        "internal framework path": "internal_path",
        "symbolic link": "symbolic_link",
        "symlink": "symbolic_link",
        "reparse": "reparse",
        "source disappeared": "source_disappeared",
        "metadata changed": "metadata_changed",
        "source changed": "source_changed",
        "identity": "identity_drift",
        "backend": "backend_unavailable",
        "preflight": "preflight_failed",
        "ambiguous": "effect_ambiguous",
        "recovery": "effect_ambiguous",
        "receipt": "receipt_invalid",
        "cancel": "cancelled",
        "budget": "budget_exhausted",
    }
    for token in known:
        if token in text:
            return token
    for token, code in aliases.items():
        if token in text:
            return code
    return "unspecified"


class RedlistPrepassError(RuntimeError):
    """A redlist effect stopped with bounded per-action recovery evidence."""

    def __init__(
        self,
        *,
        matched: int,
        applied: int,
        failed: int,
        protected: int,
        blocked: int = 0,
        failed_pre_effect: int = 0,
        recovery_required: int = 0,
        reason_codes: dict[str, int] | None = None,
        examples: tuple[dict[str, object], ...] = (),
    ) -> None:
        self.matched = matched
        self.applied = applied
        self.failed = failed
        self.protected = protected
        self.blocked = blocked
        self.failed_pre_effect = failed_pre_effect
        self.recovery_required = recovery_required
        self.reason_codes = {} if reason_codes is None else dict(reason_codes)
        self.examples = tuple(examples)
        super().__init__(
            "redlist prepass incomplete: "
            f"matched={matched} applied={applied} failed={failed} "
            f"blocked={blocked} protected={protected} "
            f"failed_pre_effect={failed_pre_effect} recovery_required={recovery_required}"
        )
_LEGAL_METADATA_NAMES = frozenset(
    {
        "authors",
        "authors.txt",
        "changelog",
        "changelog.md",
        "copying",
        "copying.md",
        "license",
        "license.txt",
        "licenses",
        "licenses.txt",
        "licenses.md",
        "licence",
        "licence.txt",
        "notice",
        "notice.txt",
        "readme",
        "readme.md",
    }
)
_LEGAL_METADATA_PREFIXES = (
    "license",
    "licence",
    "copying",
    "notice",
    "authors",
)
_PROTECTED_EFFECT_NAMES = frozenset(
    {
        ".env",
        "authorized_keys",
        "credentials",
        "credentials.json",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_rsa",
        "known_hosts",
        "password",
        "passwords",
        "secret",
        "secrets",
        "secrets.json",
        "token",
        "token.json",
    }
)
_PROTECTED_EFFECT_SUFFIXES = frozenset(
    {".asc", ".gpg", ".jks", ".key", ".keystore", ".p12", ".pem", ".pfx"}
)
_FIXTURE_COMPONENTS = frozenset({"fixture", "fixtures", "test_data", "testdata"})
TRASH_IDENTITY_ABSTENTION = (
    "Recycle Bin mutation abstained: the available Send2Trash backends resolve "
    "the source by path and cannot bind the observed file identity to the syscall"
)
# Compatibility probe for existing diagnostic/test consumers that monkeypatch
# the removed path backend to assert it is never invoked. Production code never
# reads or calls this sentinel.
send2trash: None = None


def _is_legal_metadata_name(path: str | Path) -> bool:
    """Keep license/notice attribution files out of an origin cleanup plan."""

    name = Path(path).name.casefold()
    if name in _LEGAL_METADATA_NAMES:
        return True
    if name.endswith("notices") and ("-" in name or "_" in name):
        return True
    return any(
        name.startswith(prefix)
        and len(name) > len(prefix)
        and name[len(prefix)] in {"-", "_", "."}
        for prefix in _LEGAL_METADATA_PREFIXES
    )
