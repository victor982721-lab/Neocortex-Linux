from __future__ import annotations

from _04_Nucleo_Operativo.logical_owner_contracts import (
    LOGICAL_OWNER_SPECS,
    PACKAGE_OWNER_SPECS,
    logical_owner_registry_fingerprint,
    logical_owner_registry_payload,
    matching_logical_owners,
    matching_package_owners,
    package_owner_registry_fingerprint,
    package_owner_registry_payload,
)


def test_registry_maps_only_explicit_owner_selectors_without_a_default() -> None:
    assert matching_logical_owners("_04_Nucleo_Operativo.text_route") == ("text",)
    assert matching_logical_owners("_04_Nucleo_Operativo.semantic_sources") == ("semantic",)
    assert matching_logical_owners("_04_Nucleo_Operativo.knowledge_snapshot") == ("knowledge",)
    assert matching_logical_owners("_04_Nucleo_Operativo.review_task_repository") == ("review",)
    assert matching_logical_owners("neocortex.review_task_cli_adapter") == ("review",)
    assert matching_logical_owners("_04_Nucleo_Operativo.retention_plan") == ("retention",)
    assert matching_logical_owners("_04_Nucleo_Operativo.framework_state") == ("framework",)
    assert matching_logical_owners("_04_Nucleo_Operativo.actions") == ("orchestration",)
    assert matching_logical_owners("_04_Nucleo_Operativo.archive_route") == ("archive",)
    assert matching_logical_owners("_04_Nucleo_Operativo.capabilities.formats.archive.route") == (
        "archive",
    )
    assert matching_logical_owners("_04_Nucleo_Operativo.audio_route") == ("audio",)
    assert matching_logical_owners("_04_Nucleo_Operativo.capabilities.formats.audio.route") == (
        "audio",
    )
    assert matching_logical_owners("_04_Nucleo_Operativo.code_review") == ("code-analysis",)
    assert matching_logical_owners("_04_Nucleo_Operativo.image_route") == ("image",)
    assert matching_logical_owners("_04_Nucleo_Operativo.capabilities.formats.docx.route") == (
        "docx",
    )
    assert matching_logical_owners("_04_Nucleo_Operativo.capabilities.formats.image.route") == (
        "image",
    )
    assert matching_logical_owners("_04_Nucleo_Operativo.pdf_route") == ("pdf",)
    assert matching_logical_owners("_04_Nucleo_Operativo.video_route") == ("video",)
    assert matching_logical_owners("_04_Nucleo_Operativo.capabilities.formats.video.route") == (
        "video",
    )
    assert matching_logical_owners("_02_Deduplicacion.inventory") == ("inventory",)
    assert matching_logical_owners("_05_Interfaz.main_window") == ("interface",)
    assert matching_logical_owners("neocortex.capability_broker") == ("capability",)
    assert matching_logical_owners("_04_Nucleo_Operativo.textual_similarity") == ()


def test_registry_is_canonical_nonoverlapping_and_fingerprinted() -> None:
    payload = logical_owner_registry_payload()

    assert payload["coverage_policy"] == "explicit-partial-no-default-owner-v2"
    assert tuple(item.owner_id for item in LOGICAL_OWNER_SPECS) == tuple(
        sorted(item.owner_id for item in LOGICAL_OWNER_SPECS)
    )
    assert len({item.owner_id for item in LOGICAL_OWNER_SPECS}) == len(LOGICAL_OWNER_SPECS)
    assert logical_owner_registry_fingerprint() == logical_owner_registry_fingerprint()
    assert logical_owner_registry_fingerprint().startswith("logical-owner-contract-v2:sha256:")


def test_package_ownership_is_a_separate_explicit_registry() -> None:
    payload = package_owner_registry_payload()

    assert payload["coverage_policy"] == "explicit-package-tree-no-default-owner-v1"
    assert tuple(item.package_owner_id for item in PACKAGE_OWNER_SPECS) == tuple(
        sorted(item.package_owner_id for item in PACKAGE_OWNER_SPECS)
    )
    assert matching_package_owners("_04_Nucleo_Operativo.text_route") == ("core",)
    assert matching_package_owners("_02_Deduplicacion.inventory") == ("deduplication",)
    assert matching_package_owners("neocortex.cli") == ("public-api",)
    assert matching_package_owners("third_party.module") == ()
    assert package_owner_registry_fingerprint().startswith("package-owner-contract-v1:sha256:")
    assert payload["schema"] == "neocortex.package-owner-contract/v1"
