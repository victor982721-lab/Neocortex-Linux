from __future__ import annotations

from _04_Nucleo_Operativo.logical_owner_contracts import (
    LOGICAL_OWNER_SPECS,
    logical_owner_registry_fingerprint,
    logical_owner_registry_payload,
    matching_logical_owners,
)


def test_registry_maps_only_explicit_owner_selectors_without_a_default() -> None:
    assert matching_logical_owners("_04_Nucleo_Operativo.text_route") == ("text",)
    assert matching_logical_owners("_04_Nucleo_Operativo.semantic_sources") == ("semantic",)
    assert matching_logical_owners("_04_Nucleo_Operativo.knowledge_snapshot") == ("knowledge",)
    assert matching_logical_owners("_04_Nucleo_Operativo.review_task_repository") == ("review",)
    assert matching_logical_owners("neocortex.review_task_cli_adapter") == ("review",)
    assert matching_logical_owners("_04_Nucleo_Operativo.retention_plan") == ("retention",)
    assert matching_logical_owners("_04_Nucleo_Operativo.framework_state") == ("framework",)
    assert matching_logical_owners("_04_Nucleo_Operativo.actions") == ()
    assert matching_logical_owners("_04_Nucleo_Operativo.textual_similarity") == ()


def test_registry_is_canonical_nonoverlapping_and_fingerprinted() -> None:
    payload = logical_owner_registry_payload()

    assert payload["coverage_policy"] == "explicit-partial-no-default-owner-v1"
    assert tuple(item.owner_id for item in LOGICAL_OWNER_SPECS) == tuple(
        sorted(item.owner_id for item in LOGICAL_OWNER_SPECS)
    )
    assert len({item.owner_id for item in LOGICAL_OWNER_SPECS}) == len(LOGICAL_OWNER_SPECS)
    assert logical_owner_registry_fingerprint() == logical_owner_registry_fingerprint()
    assert logical_owner_registry_fingerprint().startswith("logical-owner-contract-v1:sha256:")
