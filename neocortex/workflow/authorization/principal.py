"""Trusted-boundary contract for an authenticated curation principal.

The curation lifecycle currently accepts a human ``actor`` label for the local
CLI/API review flow.  That label is useful audit text, but it is not proof of
identity and must never be accepted as an MCP authorization principal.  This
module defines the small, deliberately unconnected contract a future trusted
adapter must provide.  It does not authenticate a user, persist credentials,
or register an MCP tool.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Callable


AUTHENTICATED_PRINCIPAL_SCHEMA = "neocortex.authenticated-principal/v1"
AUTHENTICATED_PRINCIPAL_AUTH_METHOD = "trusted-local-human-session"
MAX_PRINCIPAL_IDENTIFIER_CHARS = 256
MAX_PRINCIPAL_ISSUER_CHARS = 256
MAX_PRINCIPAL_SESSION_CHARS = 256
MAX_PRINCIPAL_JSON_BYTES = 4_096


class PrincipalValidationError(ValueError):
    """A value did not cross the authenticated-principal trust boundary."""


def _required_text(label: str, value: object, *, limit: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.strip() != value
        or len(value) > limit
    ):
        raise PrincipalValidationError(f"{label} must be a bounded trimmed string")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise PrincipalValidationError(f"{label} contains a control character")
    return value


def _positive_integer(label: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise PrincipalValidationError(f"{label} must be a positive integer")
    return value


def _sha256_digest(label: str, value: object) -> str:
    text = _required_text(label, value, limit=71)
    if (
        len(text) != 71
        or not text.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in text[7:])
    ):
        raise PrincipalValidationError(f"{label} must be sha256:<64 lowercase hex characters>")
    return text


_TRUSTED_ATTESTATION = object()


@dataclass(frozen=True, slots=True)
class AuthenticatedPrincipal:
    """An assertion produced by a trusted local authentication adapter.

    The private attestation marker is intentionally not representable by a
    JSON mapping.  A future MCP/session adapter must call
    :meth:`from_trusted_context`; passing an ``actor`` string or a decoded
    client payload is rejected by :func:`require_authenticated_principal`.
    This object is a contract boundary, not a cryptographic authenticator.
    """

    principal_id: str
    issuer: str
    session_id: str
    issued_ns: int
    expires_ns: int
    proof_digest: str
    _attestation: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._attestation is not _TRUSTED_ATTESTATION:
            raise PrincipalValidationError(
                "authenticated principal must originate from a trusted context"
            )
        _required_text(
            "principal_id", self.principal_id, limit=MAX_PRINCIPAL_IDENTIFIER_CHARS
        )
        _required_text("issuer", self.issuer, limit=MAX_PRINCIPAL_ISSUER_CHARS)
        _required_text("session_id", self.session_id, limit=MAX_PRINCIPAL_SESSION_CHARS)
        issued_ns = _positive_integer("issued_ns", self.issued_ns)
        expires_ns = _positive_integer("expires_ns", self.expires_ns)
        if expires_ns <= issued_ns:
            raise PrincipalValidationError("expires_ns must be later than issued_ns")
        object.__setattr__(self, "issued_ns", issued_ns)
        object.__setattr__(self, "expires_ns", expires_ns)
        _sha256_digest("proof_digest", self.proof_digest)
        if len(self.to_json().encode("utf-8")) > MAX_PRINCIPAL_JSON_BYTES:
            raise PrincipalValidationError("authenticated principal exceeds its byte limit")

    @classmethod
    def from_trusted_context(
        cls,
        *,
        principal_id: str,
        issuer: str,
        session_id: str,
        issued_ns: int,
        expires_ns: int,
        proof_digest: str,
    ) -> "AuthenticatedPrincipal":
        """Construct one assertion supplied by a trusted adapter.

        No caller-facing parser converts arbitrary JSON into this object.  The
        future adapter owns the authentication proof and must pass its digest
        as evidence; this package only validates shape and lifetime.
        """

        return cls(
            principal_id=principal_id,
            issuer=issuer,
            session_id=session_id,
            issued_ns=issued_ns,
            expires_ns=expires_ns,
            proof_digest=proof_digest,
            _attestation=_TRUSTED_ATTESTATION,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": AUTHENTICATED_PRINCIPAL_SCHEMA,
            "schema_version": 1,
            "principal_id": self.principal_id,
            "issuer": self.issuer,
            "session_id": self.session_id,
            "authentication_method": AUTHENTICATED_PRINCIPAL_AUTH_METHOD,
            "issued_ns": self.issued_ns,
            "expires_ns": self.expires_ns,
            "proof_digest": self.proof_digest,
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )


ClockNs = Callable[[], int]


def require_authenticated_principal(
    value: object,
    *,
    now_ns: int | None = None,
    clock_ns: ClockNs | None = None,
) -> AuthenticatedPrincipal:
    """Accept only a live trusted assertion, never an actor-shaped payload.

    ``now_ns``/``clock_ns`` are injectable solely for deterministic tests.  A
    missing clock uses ``time.time_ns`` lazily so importing this contract has
    no runtime side effects.
    """

    if type(value) is not AuthenticatedPrincipal:
        raise PrincipalValidationError(
            "actor text or client JSON is not an authenticated principal"
        )
    principal = value
    if principal._attestation is not _TRUSTED_ATTESTATION:
        raise PrincipalValidationError("authenticated principal attestation is invalid")
    if now_ns is not None and clock_ns is not None:
        raise PrincipalValidationError("provide now_ns or clock_ns, not both")
    if now_ns is None:
        if clock_ns is None:
            import time

            clock_ns = time.time_ns
        now_ns = clock_ns()
    _positive_integer("now_ns", now_ns)
    if now_ns < principal.issued_ns:
        raise PrincipalValidationError("authenticated principal is not active yet")
    if now_ns >= principal.expires_ns:
        raise PrincipalValidationError("authenticated principal has expired")
    return principal


def principal_proof_digest(proof: object) -> str:
    """Hash opaque proof metadata without accepting it as authentication."""

    try:
        encoded = json.dumps(
            proof,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise PrincipalValidationError("principal proof is not canonical JSON") from exc
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


__all__ = (
    "AUTHENTICATED_PRINCIPAL_AUTH_METHOD",
    "AUTHENTICATED_PRINCIPAL_SCHEMA",
    "AuthenticatedPrincipal",
    "PrincipalValidationError",
    "principal_proof_digest",
    "require_authenticated_principal",
)
