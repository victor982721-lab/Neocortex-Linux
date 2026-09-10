"""Typed authorization contracts for the local curation lifecycle."""

from __future__ import annotations

from .principal import (
    AUTHENTICATED_PRINCIPAL_AUTH_METHOD,
    AUTHENTICATED_PRINCIPAL_SCHEMA,
    AuthenticatedPrincipal,
    PrincipalValidationError,
    principal_proof_digest,
    require_authenticated_principal,
)

__all__ = (
    "AUTHENTICATED_PRINCIPAL_AUTH_METHOD",
    "AUTHENTICATED_PRINCIPAL_SCHEMA",
    "AuthenticatedPrincipal",
    "PrincipalValidationError",
    "principal_proof_digest",
    "require_authenticated_principal",
)
