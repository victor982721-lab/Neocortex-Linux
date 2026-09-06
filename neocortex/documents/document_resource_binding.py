"""Owner-aware resource references, separate from physical effect identity.

No decoder guesses a numeric radix from the characters in an identifier.  The
producer supplies its codec, while historical Code rows without that evidence
remain unresolved.  Resource and file identifiers are never replaced.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from neocortex.foundation.file_identity import FileIdentity, FileIdentityEncoding
from neocortex.platform.policy import physical_identity_scheme_for_birthtime

BINDING_SCHEMA = "neocortex.document-resource-binding/v1"
_BINDING_KEYS = frozenset(
    {
        "schema",
        "source_kind",
        "file_key",
        "representation_kind",
        "resource_ref",
        "physical_identity",
        "physical_anchor_path",
        "physical_anchor_revision",
        "archive_member",
        "representation_metadata",
    }
)


class ResourceBindingError(ValueError):
    """A localizable source identity refusal, not evidence of source corruption."""

    def __init__(
        self,
        message: str,
        *,
        field: str,
        encoding: str,
        value: object = None,
        code: str = "invalid_resource_identity",
    ) -> None:
        super().__init__(message)
        self.field = field
        self.encoding = encoding
        self.value = value
        self.code = code


def _integer(value: object, *, field: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ResourceBindingError(
            f"{field} must be an integer >= {minimum}", field=field, encoding="integer", value=value
        )
    return value


def physical_identity_from_components(
    volume_id: object, file_id: object, *, encoding: str
) -> FileIdentity:
    """Decode exactly the codec owned by the source; enforce unsigned 128 bits."""
    values: list[int] = []
    for field, value in (("volume_id", volume_id), ("file_id", file_id)):
        try:
            if encoding == "unsigned-128-le":
                if not isinstance(value, (bytes, bytearray, memoryview)) or len(value) != 16:
                    raise ValueError("expected exactly 16 little-endian bytes")
                number = int.from_bytes(value, "little")
            elif encoding == "integer":
                if type(value) is not int:
                    raise ValueError("expected an integer")
                number = value
            elif encoding == "code-owner-hex":
                if (
                    not isinstance(value, str)
                    or not value
                    or len(value) > 32
                    or any(char not in "0123456789abcdefABCDEF" for char in value)
                ):
                    raise ValueError("expected an unprefixed hexadecimal Code component")
                number = int(value, 16)
            elif encoding == FileIdentityEncoding.LEGACY_DECIMAL:
                if (
                    not isinstance(value, str)
                    or not value
                    or not value.isascii()
                    or (value != "0" and (value[0] not in "123456789" or not value.isdecimal()))
                ):
                    raise ValueError("expected canonical unsigned decimal text")
                number = int(value, 10)
            else:
                raise ValueError("the source identity codec is unresolved")
            FileIdentity(number, 0)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ResourceBindingError(
                str(exc), field=field, encoding=encoding, value=value
            ) from exc
        values.append(number)
    return FileIdentity(*values)


def _physical_payload(identity: FileIdentity, birthtime_ns: int) -> dict[str, object]:
    birth = _integer(birthtime_ns, field="birthtime_ns", minimum=-1)
    return {"packed_key": identity.packed_key, "birthtime_ns": birth}


def build_resource_binding(
    *,
    source_kind: str,
    file_key: str,
    path: str,
    identity: FileIdentity | None,
    birthtime_ns: int,
    size: int,
    mtime_ns: int,
    archive_member: Mapping[str, object] | None = None,
    anchor_path: str | None = None,
    representation_kind: str | None = None,
    representation_metadata: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    """Use existing ResourceRef/FileIdentity contracts without inventing another ID."""
    from neocortex.knowledge.knowledge_contracts import PhysicalIdentityRef, ResourceRef

    is_archive = (
        representation_kind == "archive_member"
        if representation_kind is not None
        else source_kind == "archive"
    )
    physical = None if identity is None else _physical_payload(identity, birthtime_ns)
    if is_archive:
        resource = ResourceRef(
            f"resource:archive:{file_key}", source_kind, source_kind, current_path=path
        )
    else:
        if identity is None:
            raise ResourceBindingError(
                "physical source identity is unresolved",
                field="physical_identity",
                encoding="unresolved",
            )
        value = f"{identity.volume_id}:{identity.file_id}:{birthtime_ns}"
        resource = ResourceRef(
            f"resource:file:{value}",
            source_kind,
            source_kind,
            PhysicalIdentityRef(physical_identity_scheme_for_birthtime(birthtime_ns), value, 1),
            path,
        )
    payload = {
        "schema": BINDING_SCHEMA,
        "source_kind": source_kind,
        "file_key": file_key,
        "representation_kind": "archive_member" if is_archive else "physical_file",
        "resource_ref": resource.to_dict(),
        "physical_identity": physical,
        "physical_anchor_path": (anchor_path if is_archive else path) if physical else None,
        "physical_anchor_revision": None
        if physical is None
        else {
            "size": _integer(size, field="size"),
            "mtime_ns": _integer(mtime_ns, field="mtime_ns"),
        },
        "archive_member": None if archive_member is None else dict(archive_member),
        "representation_metadata": {}
        if representation_metadata is None
        else dict(representation_metadata),
    }
    return parse_resource_binding(json.dumps(payload))


def parse_resource_binding(raw: object) -> dict[str, Any]:
    """Reject malformed references; callers handle a legacy NULL explicitly."""
    from neocortex.knowledge.knowledge_contracts import PhysicalIdentityRef

    try:
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > 32_768:
            raise ValueError("resource binding must be bounded JSON text")
        payload = json.loads(raw, object_pairs_hook=_unique_object)
        if not isinstance(payload, dict) or set(payload) != _BINDING_KEYS:
            raise ValueError("resource binding fields do not match the contract")
        if payload["schema"] != BINDING_SCHEMA:
            raise ValueError("resource binding schema is unsupported")
        for field in ("source_kind", "file_key"):
            if not isinstance(payload[field], str) or not payload[field]:
                raise ValueError(f"resource binding {field} is missing")
        virtual = payload["representation_kind"] == "archive_member"
        roles = payload["representation_metadata"]
        if not isinstance(roles, dict) or set(roles) - {
            "document_role",
            "logical_document_chain",
            "independently_organizable",
            "independently_disposable",
        }:
            raise ValueError("representation metadata is invalid")
        for field in ("independently_organizable", "independently_disposable"):
            if field in roles and type(roles[field]) is not bool:
                raise ValueError("representation eligibility must be boolean")
        if "document_role" in roles and roles["document_role"] not in {
            "archive_member",
            "document_component",
            "logical_document",
        }:
            raise ValueError("archive document role is unsupported")
        if roles.get("logical_document_chain") is not None and not isinstance(
            roles["logical_document_chain"], str
        ):
            raise ValueError("logical document chain is invalid")
        if roles.get("document_role") == "document_component" and (
            roles.get("independently_organizable") or roles.get("independently_disposable")
        ):
            raise ValueError("a document component is not independently organizable or disposable")
        if not virtual and payload["representation_kind"] != "physical_file":
            raise ValueError("resource representation is unsupported")
        if virtual and payload["source_kind"] != "archive":
            raise ValueError("resource owner and representation disagree")
        if (
            not virtual
            and payload["source_kind"] == "archive"
            and (
                roles.get("document_role") != "logical_document"
                or roles.get("logical_document_chain") != ""
            )
        ):
            raise ValueError(
                "only an explicitly identified outer logical document has physical archive identity"
            )
        ref = payload["resource_ref"]
        if not isinstance(ref, dict) or ref.get("source_kind") != payload["source_kind"]:
            raise ValueError("resource reference owner is inconsistent")
        if set(ref) - {
            "resource_id",
            "source_kind",
            "owner",
            "physical_identity",
            "current_path",
            "disposition",
            "canonical_resource_id",
            "kind",
            "schema_version",
        }:
            raise ValueError("resource reference has unsupported fields")
        if (
            ref.get("kind") != "resource_ref"
            or type(ref.get("schema_version")) is not int
            or ref["schema_version"] != 1
        ):
            raise ValueError("resource reference schema is unsupported")
        if ref.get("owner") != payload["source_kind"] or not isinstance(
            ref.get("current_path"), str
        ):
            raise ValueError("resource reference provenance is missing")
        physical = payload["physical_identity"]
        if physical is None:
            if (
                not virtual
                or payload["physical_anchor_path"] is not None
                or payload["physical_anchor_revision"] is not None
            ):
                raise ValueError("unresolved physical anchor is inconsistent")
        else:
            if not isinstance(physical, dict) or set(physical) != {"packed_key", "birthtime_ns"}:
                raise ValueError("physical identity fields are invalid")
            identity = FileIdentity.decode(
                physical["packed_key"], encoding=FileIdentityEncoding.PACKED_HEX_V1
            )
            birth = _integer(physical["birthtime_ns"], field="birthtime_ns", minimum=-1)
            anchor = payload["physical_anchor_path"]
            if not isinstance(anchor, str) or not anchor.startswith("/"):
                raise ValueError("physical anchor must be absolute")
            revision = payload["physical_anchor_revision"]
            if not isinstance(revision, dict) or set(revision) != {"size", "mtime_ns"}:
                raise ValueError("physical anchor revision is missing")
            for field in ("size", "mtime_ns"):
                _integer(revision[field], field=field)
            if not virtual:
                value = f"{identity.volume_id}:{identity.file_id}:{birth}"
                expected = PhysicalIdentityRef(
                    physical_identity_scheme_for_birthtime(birth), value, 1
                ).to_dict()
                if (
                    ref.get("physical_identity") != expected
                    or ref.get("resource_id") != f"resource:file:{value}"
                    or ref["current_path"] != anchor
                ):
                    raise ValueError("resource and physical reference disagree")
        member = payload["archive_member"]
        if virtual:
            if (
                ref.get("physical_identity") is not None
                or ref.get("resource_id") != f"resource:archive:{payload['file_key']}"
            ):
                raise ValueError("archive member cannot masquerade as a filesystem object")
            if member is not None:
                if not isinstance(member, dict) or set(member) != {
                    "container_key",
                    "container_path",
                    "member_chain",
                }:
                    raise ValueError("archive provenance is invalid")
                if (
                    not all(isinstance(value, str) for value in member.values())
                    or not member["container_key"]
                    or not member["container_path"]
                ):
                    raise ValueError("archive provenance is incomplete")
                if physical is not None and (
                    member["container_path"] != payload["physical_anchor_path"]
                    or member["container_key"] != physical["packed_key"]
                ):
                    raise ValueError("archive provenance disagrees with its physical anchor")
            elif physical is not None:
                raise ValueError("resolved archive anchor requires member provenance")
        elif member is not None:
            raise ValueError("physical file cannot carry archive provenance")
        return payload
    except (TypeError, ValueError, KeyError, OverflowError) as exc:
        if isinstance(exc, ResourceBindingError):
            raise
        raise ResourceBindingError(
            str(exc), field="resource_binding_json", encoding=BINDING_SCHEMA, value=raw
        ) from exc


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate resource binding JSON field")
        result[key] = value
    return result


def binding_curation_identity(binding: Mapping[str, Any]) -> dict[str, object] | None:
    """Only the member itself, never its archive container, can be an effect source."""
    if binding["representation_kind"] != "physical_file":
        return None
    physical = binding["physical_identity"]
    identity = FileIdentity.decode(
        physical["packed_key"], encoding=FileIdentityEncoding.PACKED_HEX_V1
    )
    return {
        "volume_id": format(identity.volume_id, "x"),
        "file_id": format(identity.file_id, "x"),
        "birthtime_ns": physical["birthtime_ns"],
    }


def legacy_resource_binding(
    *,
    source_kind: str,
    file_key: str,
    path: str,
    volume_id: object,
    file_id: object,
    birthtime_ns: int,
    size: int,
    mtime_ns: int,
) -> dict[str, Any]:
    """Read known legacy contracts without modifying their stored fields.

    Archive owner keys are valid logical references even when the old plan
    omitted its container anchor. Code's untagged decimal/hex fields are not
    guessed, and require a new owner-backed binding before physical use.
    """
    if source_kind == "archive":
        if (
            volume_id != "archive"
            or not file_key.startswith("archive:")
            or file_key != f"archive:{file_id}"
        ):
            raise ResourceBindingError(
                "archive owner identity is inconsistent",
                field="file_key",
                encoding="archive-owner-key",
                value=file_key,
            )
        return build_resource_binding(
            source_kind=source_kind,
            file_key=file_key,
            path=path,
            identity=None,
            birthtime_ns=birthtime_ns,
            size=size,
            mtime_ns=mtime_ns,
        )
    if source_kind == "code":
        raise ResourceBindingError(
            "legacy Code identity encoding requires an owner-backed binding",
            field="volume_id,file_id",
            encoding="unresolved",
            value=[volume_id, file_id],
            code="identity_encoding_unresolved",
        )
    identity = physical_identity_from_components(volume_id, file_id, encoding="legacy-decimal")
    return build_resource_binding(
        source_kind=source_kind,
        file_key=file_key,
        path=path,
        identity=identity,
        birthtime_ns=birthtime_ns,
        size=size,
        mtime_ns=mtime_ns,
        representation_kind="physical_file",
    )


__all__ = [
    "BINDING_SCHEMA",
    "ResourceBindingError",
    "binding_curation_identity",
    "build_resource_binding",
    "legacy_resource_binding",
    "parse_resource_binding",
    "physical_identity_from_components",
]
