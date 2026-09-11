"""Archive-root evidence must stay distinct from virtual ZIP members."""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import replace
import sqlite3
from pathlib import Path
from typing import cast

import pytest

from neocortex.knowledge import knowledge_search_content as content
from neocortex.knowledge import knowledge_evidence_lookup as lookup
from neocortex.knowledge.knowledge_contracts import (
    EvidenceMethod,
    EvidenceRef,
    PhysicalIdentityRef,
    ResourceRef,
)
from neocortex.semantic.semantic_models import (
    EmbeddingModality,
    ResolvedSearchHit,
    SearchHit,
)


def _resolved(
    *,
    section_kind: str,
    section_id: str,
    path: str,
    source_identity: str,
    provenance: dict[str, object],
    published_revision_id: int | None = None,
    current_revision_id: int | None = None,
) -> ResolvedSearchHit:
    text = "contenido durable de documento"
    return ResolvedSearchHit(
        hit=SearchHit(
            ref_id=1,
            entity_id=f"chunk:{source_identity}",
            item_id=f"item:archive:{source_identity}",
            indexed_model_signature="archive-fixture-model",
            vector_space="archive-fixture-space",
            modality=EmbeddingModality.TEXT,
            score=0.8,
            generation_id=1,
        ),
        path=path,
        source_kind="archive",
        source_identity=source_identity,
        section_kind=section_kind,
        section_id=section_id,
        start_char=0,
        end_char=len(text),
        snippet=text,
        source_revision={
            "size": 42,
            "mtime_ns": 10,
            "birthtime_ns": -1,
            "processing_signature": "archive-fixture-v1",
            "last_seen_run_id": 1,
        },
        section_provenance=provenance,
        source_status="indexed",
        published_revision_id=published_revision_id,
        current_revision_id=current_revision_id,
    )


def _archive_provenance(
    *,
    container_path: str,
    container_key: str,
    member_chain: str,
    member_path: str,
    archive_depth: int,
    document_role: str,
    logical_document_chain: str | None,
    inside_zip: bool,
) -> dict[str, object]:
    return {
        "adapter": "semantic-source-fixture-v1",
        "inside_zip": inside_zip,
        "container_path": container_path,
        "container_key": container_key,
        "member_chain": member_chain,
        "member_path": member_path,
        "archive_depth": archive_depth,
        "content_kind": "text",
        "media_type": "text/plain",
        "container_status": "complete",
        "document_role": document_role,
        "logical_document_chain": logical_document_chain,
    }


def _archive_row(
    *,
    path: str,
    container_path: str,
    member_chain: str,
    member_path: str,
    archive_depth: int,
    document_role: str,
    logical_document_chain: str | None,
) -> dict[str, object]:
    return {
        "path": path,
        "container_path": container_path,
        "member_chain": member_chain,
        "member_path": member_path,
        "archive_depth": archive_depth,
        "document_role": document_role,
        "logical_document_chain": logical_document_chain,
        "container_key": "container-fixture",
        "content_kind": "text",
        "media_type": "text/plain",
        "container_status": "complete",
    }


def _materialize(
    resolved: ResolvedSearchHit,
) -> tuple[ResourceRef, EvidenceRef]:
    resource, warnings = content._resource_from_resolved(
        resolved,
        resolved_physical_identity_fn=lambda _value: "1:2:-1",
        int_provenance_fn=content.int_provenance,
        lexical_owner_formats={"office": frozenset()},
        resource_ref_type=ResourceRef,
        physical_identity_ref_type=PhysicalIdentityRef,
    )
    assert warnings == ("physical_identity_unresolved",)
    evidence = content._evidence_from_resolved(
        resolved,
        resource_id=resource.resource_id,
        revision_id="revision:archive-fixture",
        generation=1,
        producer="semantic-fixture",
        int_provenance_fn=content.int_provenance,
        evidence_ref_type=EvidenceRef,
        extracted_method=EvidenceMethod.EXTRACTED,
    )
    return resource, evidence


def test_archive_root_uses_real_path_body_and_inside_zip_zero() -> None:
    root_path = "/fixtures/Informe.ott"
    resolved = _resolved(
        section_kind="archive_document",
        section_id="body",
        path=root_path,
        source_identity="archive:logical-fixture",
        provenance=_archive_provenance(
            container_path=root_path,
            container_key="container-fixture",
            member_chain="",
            member_path="",
            archive_depth=0,
            document_role="logical_document",
            logical_document_chain="",
            inside_zip=False,
        ),
    )

    resource, evidence = _materialize(resolved)

    assert resource.current_path == root_path
    assert resource.physical_identity is None
    assert resource.resource_id == "resource:archive:archive:logical-fixture"
    assert evidence.section_kind == "archive_document"
    assert evidence.section_id == "body"
    assert dict(evidence.identifiers)["inside_zip"] == "0"
    assert "!/body" not in (resource.current_path or "")
    lookup._validate_archive_owner_locator(
        _archive_row(
            path=root_path,
            container_path=root_path,
            member_chain="",
            member_path="",
            archive_depth=0,
            document_role="logical_document",
            logical_document_chain="",
        ),  # type: ignore[arg-type]
        resolved,
    )


def test_archive_member_keeps_virtual_locator_and_inside_zip_one() -> None:
    container_path = "/fixtures/contenedor.zip"
    member_chain = "docs/Informe.txt"
    resolved = _resolved(
        section_kind="archive_member",
        section_id=member_chain,
        path=f"{container_path}!/{member_chain}",
        source_identity="archive:member-fixture",
        provenance=_archive_provenance(
            container_path=container_path,
            container_key="container-fixture",
            member_chain=member_chain,
            member_path=member_chain,
            archive_depth=1,
            document_role="archive_member",
            logical_document_chain=None,
            inside_zip=True,
        ),
    )

    resource, evidence = _materialize(resolved)

    assert resource.current_path == f"{container_path}!/{member_chain}"
    assert resource.physical_identity is None
    assert evidence.section_kind == "archive_member"
    assert evidence.section_id == member_chain
    assert dict(evidence.identifiers)["inside_zip"] == "1"

    lookup._validate_archive_owner_locator(
        cast(
            sqlite3.Row,
            _archive_row(
                path=resource.current_path,
                container_path=container_path,
                member_chain=member_chain,
                member_path=member_chain,
                archive_depth=1,
                document_role="archive_member",
                logical_document_chain=None,
            ),
        ),
        resolved,
    )


@pytest.mark.parametrize(
    "mutation",
    (
        lambda value: replace(value, path="/fixtures/Informe.ott!/body"),
        lambda value: replace(value, section_kind="archive_member", section_id=""),
        lambda value: replace(
            value,
            section_provenance={**value.section_provenance, "inside_zip": True},
        ),
        lambda value: replace(
            value,
            section_provenance={**value.section_provenance, "logical_document_chain": None},
        ),
    ),
)
def test_forged_archive_root_locator_is_rejected(mutation) -> None:
    root_path = "/fixtures/Informe.ott"
    resolved = _resolved(
        section_kind="archive_document",
        section_id="body",
        path=root_path,
        source_identity="archive:logical-fixture",
        provenance=_archive_provenance(
            container_path=root_path,
            container_key="container-fixture",
            member_chain="",
            member_path="",
            archive_depth=0,
            document_role="logical_document",
            logical_document_chain="",
            inside_zip=False,
        ),
    )
    forged = mutation(resolved)

    if forged.section_provenance.get("inside_zip") is True:
        with pytest.raises(ValueError, match="inside_zip"):
            _materialize(forged)
    else:
        with pytest.raises(lookup.EvidenceLookupError, match="evidence_locator_changed"):
            lookup._validate_archive_owner_locator(
                cast(
                    sqlite3.Row,
                    _archive_row(
                        path=root_path,
                        container_path=root_path,
                        member_chain="",
                        member_path="",
                        archive_depth=0,
                        document_role="logical_document",
                        logical_document_chain="",
                    ),
                ),
                forged,
            )


def test_archive_root_rejects_non_integer_depth_in_owner_row() -> None:
    resolved = _resolved(
        section_kind="archive_document",
        section_id="body",
        path="/fixtures/Informe.ott",
        source_identity="archive:logical-fixture",
        provenance=_archive_provenance(
            container_path="/fixtures/Informe.ott",
            container_key="container-fixture",
            member_chain="",
            member_path="",
            archive_depth=0,
            document_role="logical_document",
            logical_document_chain="",
            inside_zip=False,
        ),
    )

    with pytest.raises(lookup.EvidenceLookupError, match="evidence_locator_changed"):
        lookup._validate_archive_owner_locator(
            _archive_row(
                path="/fixtures/Informe.ott",
                container_path="/fixtures/Informe.ott",
                member_chain="",
                member_path="",
                archive_depth=0.5,  # type: ignore[arg-type]
                document_role="logical_document",
                logical_document_chain="",
            ),
            resolved,
        )


def test_changed_semantic_revision_is_rejected_before_archive_hydration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolved = _resolved(
        section_kind="archive_document",
        section_id="body",
        path="/fixtures/Informe.ott",
        source_identity="archive:logical-fixture",
        provenance=_archive_provenance(
            container_path="/fixtures/Informe.ott",
            container_key="container-fixture",
            member_chain="",
            member_path="",
            archive_depth=0,
            document_role="logical_document",
            logical_document_chain="",
            inside_zip=False,
        ),
        published_revision_id=1,
        current_revision_id=2,
    )

    class Cursor:
        def fetchall(self) -> list[dict[str, object]]:
            return [
                {
                    "member_id": 1,
                    "entity_id": "chunk:archive:logical-fixture",
                    "item_id": "item:archive:archive:logical-fixture",
                    "model_signature": "model",
                    "vector_space": "space",
                    "generation_id": 1,
                    "text_chars": 10,
                }
            ]

    class Connection:
        def execute(self, *_args: object, **_kwargs: object) -> Cursor:
            return Cursor()

    monkeypatch.setattr(lookup, "semantic_read_context", lambda: nullcontext())
    monkeypatch.setattr(
        lookup,
        "semantic_database",
        lambda *_args, **_kwargs: nullcontext(Connection()),
    )
    monkeypatch.setattr(
        lookup,
        "_observe_owner",
        lambda _connection, _owner: {"publications": []},
    )
    monkeypatch.setattr(lookup, "resolve_search_hits", lambda *_args, **_kwargs: (resolved,))

    with pytest.raises(lookup.EvidenceLookupError, match="owner_revision_changed"):
        lookup._semantic_evidence(
            Path("/fixtures/semantic.sqlite3"),
            {"retrieval_publication": []},
            {"generation": 1},
            cast(
                sqlite3.Row,
                _archive_row(
                    path="/fixtures/Informe.ott",
                    container_path="/fixtures/Informe.ott",
                    member_chain="",
                    member_path="",
                    archive_depth=0,
                    document_role="logical_document",
                    logical_document_chain="",
                ),
            ),
            "archive",
            "archive",
            "archive:logical-fixture",
            "chunk:archive:logical-fixture",
            {},
        )
