"""Read-only requests, human presentation and durable status projection."""

from .client import SharedReadClient
from .curation import CurationReadRepository, present_curation_snapshot
from .models import (
    MAX_PRESENTATION_CHARACTERS,
    MAX_QUERY_CHARACTERS,
    MAX_RESULTS_PER_SCOPE,
    ReadClient,
    ReadClientError,
    ReadOperation,
    ReadPresentation,
    ReadRequest,
)
from .presentation import present_read_payload

__all__ = [
    "MAX_PRESENTATION_CHARACTERS",
    "MAX_QUERY_CHARACTERS",
    "MAX_RESULTS_PER_SCOPE",
    "CurationReadRepository",
    "ReadClient",
    "ReadClientError",
    "ReadOperation",
    "ReadPresentation",
    "ReadRequest",
    "SharedReadClient",
    "present_curation_snapshot",
    "present_read_payload",
]
