"""Explicit policy values for Code's third-party cleanup boundary.

The classifier is advisory and lives in the Code owner.  This module only
contains the small, serializable policy that the CLI passes to the lifecycle
and the action owner binds to its preview/receipt.  In particular, selecting
``trash`` is a request for a mutation plan; it is not a filesystem
authorization and never bypasses ``--apply`` or identity checks.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Final, Literal


CodeThirdPartyAction = Literal["keep", "trash"]

THIRD_PARTY_POLICY_SCHEMA: Final = "neocortex.code-third-party-policy/v1"
THIRD_PARTY_ACTION_CHOICES: Final[tuple[CodeThirdPartyAction, ...]] = ("keep", "trash")

# These classes are eligible for proof, not for disposal by classification.
# Every physical action also requires a retained local regeneration witness.
THIRD_PARTY_KIND_CHOICES: Final[tuple[str, ...]] = (
    "dependency",
    "vendored",
    "binary",
    "generated",
    "build_artifact",
    "cache",
)
DEFAULT_THIRD_PARTY_KINDS: Final[tuple[str, ...]] = (
    "dependency",
    "vendored",
    "binary",
    "generated",
    "build_artifact",
    "cache",
)
DEFAULT_THIRD_PARTY_MIN_CONFIDENCE: Final[float] = 0.95
DEFAULT_THIRD_PARTY_MAX_ACTIONS: Final[int] = 256
MAX_THIRD_PARTY_ACTIONS: Final[int] = 10_000


@dataclass(frozen=True, slots=True)
class CodeThirdPartyPolicy:
    """Bounded, explicit third-party action policy.

    ``keep`` is the safe default and leaves origin signals advisory.  ``trash``
    requests a bounded plan; without ``--apply`` it remains a preview, while
    an applied run must still produce a fresh identity-bound plan and receipt
    for each effect.
    """

    action: CodeThirdPartyAction = "keep"
    min_confidence: float = DEFAULT_THIRD_PARTY_MIN_CONFIDENCE
    max_actions: int = DEFAULT_THIRD_PARTY_MAX_ACTIONS
    kinds: tuple[str, ...] = field(default_factory=lambda: DEFAULT_THIRD_PARTY_KINDS)

    def __post_init__(self) -> None:
        if self.action not in THIRD_PARTY_ACTION_CHOICES:
            raise ValueError("third-party action must be keep or trash")
        if (
            isinstance(self.min_confidence, bool)
            or not isinstance(self.min_confidence, (int, float))
            or not math.isfinite(float(self.min_confidence))
            or not 0.0 <= float(self.min_confidence) <= 1.0
        ):
            raise ValueError("third-party minimum confidence must be between 0 and 1")
        if (
            isinstance(self.max_actions, bool)
            or not isinstance(self.max_actions, int)
            or not 1 <= self.max_actions <= MAX_THIRD_PARTY_ACTIONS
        ):
            raise ValueError(
                f"third-party maximum actions must be between 1 and {MAX_THIRD_PARTY_ACTIONS}"
            )
        if not self.kinds:
            raise ValueError("third-party kinds cannot be empty")
        if len(set(self.kinds)) != len(self.kinds):
            raise ValueError("third-party kinds must be unique")
        if any(kind not in THIRD_PARTY_KIND_CHOICES for kind in self.kinds):
            raise ValueError("third-party kind is unsupported")

    @property
    def mutation_requested(self) -> bool:
        """Whether the caller requested third-party trash candidates."""

        return self.action == "trash"

    def admits(self, kind: str, confidence: float) -> bool:
        """Return whether one advisory classification enters this policy."""

        return bool(
            self.action == "trash"
            and kind in self.kinds
            and isinstance(confidence, (int, float))
            and not isinstance(confidence, bool)
            and math.isfinite(float(confidence))
            and float(confidence) >= self.min_confidence
        )

    def to_dict(self) -> dict[str, object]:
        """Return the bounded payload persisted with a run manifest."""

        return {
            "schema": THIRD_PARTY_POLICY_SCHEMA,
            "action": self.action,
            "min_confidence": self.min_confidence,
            "max_actions": self.max_actions,
            "kinds": list(self.kinds),
            # This is intentionally a request marker, not an authorization
            # claim.  The action/receipt owner owns the real effect gate.
            "mutation_requested": self.mutation_requested,
            "requires_regeneration_proof": True,
        }


def default_code_third_party_policy() -> CodeThirdPartyPolicy:
    """Return a fresh immutable default for CLI/configuration consumers."""

    return CodeThirdPartyPolicy()


__all__ = [
    "DEFAULT_THIRD_PARTY_KINDS",
    "DEFAULT_THIRD_PARTY_MAX_ACTIONS",
    "DEFAULT_THIRD_PARTY_MIN_CONFIDENCE",
    "MAX_THIRD_PARTY_ACTIONS",
    "THIRD_PARTY_ACTION_CHOICES",
    "THIRD_PARTY_KIND_CHOICES",
    "THIRD_PARTY_POLICY_SCHEMA",
    "CodeThirdPartyAction",
    "CodeThirdPartyPolicy",
    "default_code_third_party_policy",
]
