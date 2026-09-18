"""Durable Archive provenance and bounded, read-only reconstruction proofs.

A proof is evidence for a lifecycle owner, never permission to unlink anything.
The owner must revalidate it beside its physical effect and preserve its receipt
and manifest until every dependent output has been accounted for.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import time
import uuid
import zipfile
import zlib
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import IO, Any, BinaryIO

from neocortex.platform.zip_safety import inspect_zip_stream, ZipStructureError

from .materialization import (
    ARCHIVE_STAGE_CHUNK_BYTES,
    ArchiveManifest,
    ArchiveMaterializationLimits,
    _digest_manifest,
    _entry_identity,
    _registered_scratch_workspace,
)

ARCHIVE_REBUILD_POLICY = "neocortex.archive-rebuild/v1"
MAX_ARCHIVE_MANIFEST_BYTES = 64 * 1024 * 1024
ARCHIVE_PROVENANCE_OWNER = "archive-materialization"


class ArchiveRebuildError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


def _identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (metadata.st_dev, metadata.st_ino, metadata.st_mode, metadata.st_size,
            metadata.st_mtime_ns, metadata.st_ctime_ns)


def _absolute(path: str | os.PathLike[str]) -> Path:
    value = Path(path)
    if not value.is_absolute() or ".." in value.parts:
        raise ArchiveRebuildError("unsafe_path", "an absolute path without traversal is required")
    return value


@contextmanager
def _open_directory(path: Path, *, create: bool = False) -> Iterator[int]:
    """Anchor every path component with O_NOFOLLOW, including ancestors."""
    path = _absolute(path)
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in path.parts[1:]:
            if create:
                try:
                    os.mkdir(component, 0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def _open_regular(path: Path) -> Iterator[BinaryIO]:
    with _open_directory(path.parent) as parent:
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=parent)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ArchiveRebuildError("unsafe_path", "expected a regular file")
            stream = os.fdopen(descriptor, "rb", buffering=0)
        except BaseException:
            os.close(descriptor)
            raise
        with stream:
            yield stream


def _canonical(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=True, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _publish_json(path: Path, payload: Mapping[str, Any]) -> None:
    encoded = _canonical(payload)
    if len(encoded) > MAX_ARCHIVE_MANIFEST_BYTES:
        raise ArchiveRebuildError("manifest_budget", "Archive manifest exceeds its durable bound")
    with _open_directory(path.parent, create=True) as parent:
        temporary = f".{path.name}.{uuid.uuid4().hex}.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent,
                        follow_symlinks=False)
            except FileExistsError:
                with _open_regular(path) as existing:
                    if existing.read(MAX_ARCHIVE_MANIFEST_BYTES + 1) != encoded:
                        raise ArchiveRebuildError("manifest_collision", "durable manifest differs") from None
            os.unlink(temporary, dir_fd=parent)
            os.fsync(parent)
        except BaseException:
            try:
                os.unlink(temporary, dir_fd=parent)
            except OSError:
                pass
            raise


@dataclass(frozen=True, slots=True)
class ArchiveManifestReference:
    path: str
    digest: str
    identity: tuple[int, ...]
    artifact_id: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ArchiveManifestReference:
        return cls(str(value["path"]), str(value["digest"]), tuple(value["identity"]),
                   value.get("artifact_id"))


@dataclass(frozen=True, slots=True)
class ArchiveSurvivingInput:
    """One location explicitly supplied by the authorized inventory/catalog."""
    path: str | Path
    sha256: str | None = None
    identity: tuple[int, ...] | None = None


@dataclass(slots=True)
class ArchiveRebuildBudget:
    limits: ArchiveMaterializationLimits = field(default_factory=ArchiveMaterializationLimits)
    checkpoint: Callable[[], None] | None = None
    scratch_directory: Path | None = None
    started: float = field(default_factory=time.monotonic)
    bytes_read: int = 0
    max_read_bytes: int | None = None
    expanded_bytes: int = 0
    members: int = 0
    temp_bytes: int = 0
    max_memory_spool_bytes: int = 8 * 1024 * 1024
    memory_spool_bytes: int = 0
    memory_spools: dict[tuple[str, str], io.BytesIO] = field(default_factory=dict)
    # A cache is scoped to one explicit assessment, and fences are rechecked
    # on every reuse. It is never a durable substitute for current evidence.
    source_cache: dict[str, tuple[tuple[int, ...], str]] = field(default_factory=dict)
    structure_cache: dict[tuple[str, ...], Any] = field(default_factory=dict)
    member_cache: dict[tuple[Any, ...], tuple[int, int, str]] = field(default_factory=dict)

    def check(self) -> None:
        if self.checkpoint is not None:
            try:
                self.checkpoint()
            except Exception as exc:
                raise ArchiveRebuildError("cancelled", "Archive proof was cancelled") from exc
        if time.monotonic() - self.started > self.limits.timeout_seconds:
            raise ArchiveRebuildError("timeout", "Archive proof deadline exceeded")


@dataclass(frozen=True, slots=True)
class ArchiveRebuildProof:
    output_path: str
    manifest_ref: ArchiveManifestReference | None
    source_path: str | None = None
    output_identity: tuple[int, ...] | None = None
    output_sha256: str | None = None
    source_identity: tuple[int, ...] | None = None
    source_sha256: str | None = None
    member_identities: tuple[str, ...] = ()
    verified_size: int | None = None
    verified_crc32: int | None = None
    source_is_outside_retirement_set: bool = False
    coverage: str = "unproved"
    blocker: str | None = None
    detail: str | None = None
    policy: str = ARCHIVE_REBUILD_POLICY

    @property
    def rebuildable(self) -> bool:
        return (self.blocker is None and self.coverage == "complete"
                and self.source_is_outside_retirement_set)

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "rebuildable": self.rebuildable}


@dataclass(frozen=True, slots=True)
class ArchiveMaterializationRebuildProof:
    destination: str
    outputs: tuple[ArchiveRebuildProof, ...]
    unknown_paths: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    directory_identities: tuple[tuple[str, tuple[int, ...]], ...] = ()
    policy: str = ARCHIVE_REBUILD_POLICY

    @property
    def rebuildable(self) -> bool:
        return not self.blockers and not self.unknown_paths and all(p.rebuildable for p in self.outputs)

    def to_dict(self) -> dict[str, Any]:
        return {"destination": self.destination, "outputs": [p.to_dict() for p in self.outputs],
                "unknown_paths": list(self.unknown_paths), "blockers": list(self.blockers),
                "directory_identities": self.directory_identities,
                "policy": self.policy, "rebuildable": self.rebuildable}


def _read_manifest(ref: ArchiveManifestReference, budget: ArchiveRebuildBudget) -> dict[str, Any]:
    budget.check()
    with _open_regular(_absolute(ref.path)) as stream:
        before = _identity(os.fstat(stream.fileno()))
        if before != tuple(ref.identity):
            raise ArchiveRebuildError("manifest_changed", "manifest identity differs from its reference")
        chunks: list[bytes] = []
        count = 0
        while chunk := stream.read(ARCHIVE_STAGE_CHUNK_BYTES):
            budget.check()
            count += len(chunk)
            if count > MAX_ARCHIVE_MANIFEST_BYTES:
                raise ArchiveRebuildError("manifest_budget", "manifest exceeds the read bound")
            chunks.append(chunk)
        payload = b"".join(chunks)
        if before != _identity(os.fstat(stream.fileno())):
            raise ArchiveRebuildError("manifest_changed", "manifest changed during read")
    value = json.loads(payload)
    if not isinstance(value, dict) or value.get("schema") != "neocortex.archive-manifest/v2":
        raise ArchiveRebuildError("legacy_manifest", "manifest has no structural provenance")
    unsigned = dict(value)
    unsigned["manifest_digest"] = ""
    digest = hashlib.sha256(_canonical(unsigned)).hexdigest()
    if digest != ref.digest or value.get("manifest_digest") != ref.digest:
        raise ArchiveRebuildError("manifest_changed", "manifest digest differs from its reference")
    if value.get("apply") is not True or value.get("status") != "complete" or value.get("errors"):
        raise ArchiveRebuildError("coverage_partial", "only a complete applied manifest proves outputs")
    source_identity = value.get("source_identity")
    source_hash = value.get("source_sha256")
    entries, outputs = value.get("entries"), value.get("outputs")
    if (not isinstance(source_identity, list) or len(source_identity) != 6
            or any(type(item) is not int for item in source_identity)
            or not isinstance(source_hash, str) or len(source_hash) != 64
            or any(char not in "0123456789abcdef" for char in source_hash)
            or not isinstance(entries, list) or not isinstance(outputs, list)):
        raise ArchiveRebuildError("manifest_invalid", "manifest lacks complete source and output identities")
    if len(entries) > budget.limits.max_members or len(outputs) > budget.limits.max_members:
        raise ArchiveRebuildError("budget", "manifest member coverage exceeds its bound")
    by_identity = {entry["identity"]: entry for entry in entries}
    if len(by_identity) != len(entries):
        raise ArchiveRebuildError("ambiguous_chain", "manifest repeats a structural identity")
    if any(entry.get("status") != "validated" for entry in entries):
        raise ArchiveRebuildError("coverage_partial", "manifest contains unverified members")
    functional = value["classification"].get("unit_kind") in {"office", "odf", "epub", "project"}
    if functional:
        if len(outputs) != 1 or outputs[0].get("status") not in {"applied", "reused"}:
            raise ArchiveRebuildError("coverage_partial", "functional unit has no complete published output")
    else:
        by_output = {output["entry_identity"]: output for output in outputs}
        if len(by_output) != len(outputs) or set(by_output) != set(by_identity):
            raise ArchiveRebuildError("coverage_partial", "manifest omits or duplicates member outputs")
        parents = {entry.get("parent_identity") for entry in entries}
        for identity, output in by_output.items():
            budget.check()
            if output.get("status") in {"applied", "reused"}:
                continue
            if (output.get("status") == "skipped" and identity in parents
                    and by_identity[identity].get("content_kind") == "storage_archive"):
                continue
            raise ArchiveRebuildError("coverage_partial", "manifest output was not published")
    budget.check()
    return value


def _read_digest(stream: IO[bytes], budget: ArchiveRebuildBudget, *, max_bytes: int,
                 expanded: bool = False, spool: IO[bytes] | None = None) -> tuple[int, int, str]:
    digest = hashlib.sha256()
    actual = crc = 0
    while True:
        budget.check()
        chunk = stream.read(ARCHIVE_STAGE_CHUNK_BYTES)
        if not chunk:
            break
        actual += len(chunk)
        budget.bytes_read += len(chunk)
        max_read = budget.max_read_bytes if budget.max_read_bytes is not None else (
            budget.limits.max_input_bytes + 2 * budget.limits.max_total_uncompressed_bytes)
        if budget.bytes_read > max_read:
            raise ArchiveRebuildError("budget", "Archive total verification read budget exhausted")
        if actual > max_bytes:
            raise ArchiveRebuildError("budget", "Archive read exceeds its byte bound")
        if expanded:
            budget.expanded_bytes += len(chunk)
            if budget.expanded_bytes > budget.limits.max_total_uncompressed_bytes:
                raise ArchiveRebuildError("budget", "Archive expansion budget exhausted")
        if spool is not None:
            budget.temp_bytes += len(chunk)
            if budget.temp_bytes > budget.limits.max_total_temp_bytes:
                raise ArchiveRebuildError("budget", "Archive private spool budget exhausted")
            spool.write(chunk)
        digest.update(chunk)
        crc = zlib.crc32(chunk, crc)
    budget.check()
    return actual, crc & 0xFFFFFFFF, digest.hexdigest()


def _retiring(path: Path, identity: tuple[int, ...], targets: Iterable[str | Path]) -> bool:
    for raw in targets:
        target = _absolute(raw)
        if path == target or target in path.parents:
            return True
        try:
            observed = target.lstat()
        except FileNotFoundError:
            continue
        if (observed.st_dev, observed.st_ino) == identity[:2]:
            return True
    return False


def _select_source(manifest: Mapping[str, Any], inputs: Iterable[ArchiveSurvivingInput],
                   budget: ArchiveRebuildBudget, retirement_set: tuple[str | Path, ...]
                   ) -> tuple[Path, tuple[int, ...]]:
    expected = manifest.get("source_sha256")
    seen = 0
    changed = False
    for item in inputs:
        budget.check()
        seen += 1
        if seen > budget.limits.max_members:
            raise ArchiveRebuildError("budget", "authorized source lookup exceeds its bound")
        path = _absolute(item.path)
        try:
            with _open_regular(path) as stream:
                identity = _identity(os.fstat(stream.fileno()))
                if item.identity is not None and identity != tuple(item.identity):
                    changed = True
                    continue
                if item.sha256 is not None and item.sha256 != expected:
                    continue
                if identity[3] != manifest.get("source_size"):
                    changed = True
                    continue
                cached = budget.source_cache.get(str(path))
                if cached is not None and cached[0] == identity:
                    digest = cached[1]
                else:
                    _, _, digest = _read_digest(stream, budget, max_bytes=budget.limits.max_input_bytes)
                if identity != _identity(os.fstat(stream.fileno())):
                    raise ArchiveRebuildError("source_changed", "source changed during verification")
                if digest != expected:
                    changed = True
                    continue
                if _retiring(path, identity, retirement_set):
                    raise ArchiveRebuildError("source_selected_for_retirement", "source must survive the operation")
                budget.source_cache[str(path)] = (identity, digest)
                return path, identity
        except FileNotFoundError:
            continue
    code = "source_changed" if changed else "source_unavailable"
    raise ArchiveRebuildError(code, "no authorized surviving source matches the manifest")


def _member_chain(manifest: Mapping[str, Any], identity: str,
                  budget: ArchiveRebuildBudget) -> tuple[dict[str, Any], ...]:
    entries = manifest.get("entries")
    if not isinstance(entries, list) or len(entries) > budget.limits.max_members:
        raise ArchiveRebuildError("budget", "manifest member coverage exceeds its bound")
    by_id = {entry["identity"]: entry for entry in entries}
    if len(by_id) != len(entries):
        raise ArchiveRebuildError("ambiguous_chain", "duplicate structural member identity")
    chain: list[dict[str, Any]] = []
    seen: set[str] = set()
    while identity:
        budget.check()
        if identity in seen or identity not in by_id or len(chain) >= budget.limits.max_depth:
            raise ArchiveRebuildError("ambiguous_chain", "member ancestry is absent, cyclic or too deep")
        seen.add(identity)
        entry = by_id[identity]
        expected = _entry_identity(manifest.get("source_sha256"), entry["chain"],
                                   entry["ordinal"], entry["header_offset"],
                                   entry.get("parent_identity"))
        if expected != identity or entry.get("status") != "validated":
            raise ArchiveRebuildError("ambiguous_chain", "member structural identity is not validated")
        chain.append(entry)
        identity = entry.get("parent_identity")
    chain.reverse()
    if any(entry["depth"] != index + 1 for index, entry in enumerate(chain)):
        raise ArchiveRebuildError("ambiguous_chain", "member ancestry depth differs")
    return tuple(chain)


def _verify_member(stream: BinaryIO, size: int, chain: tuple[dict[str, Any], ...],
                   budget: ArchiveRebuildBudget, spool_root: Path | None,
                   level: int = 0, source_key: str = "") -> tuple[int, int, str]:
    budget.check()
    entry = chain[level]
    archive_key = (source_key, *(item["identity"] for item in chain[:level]))
    structure = budget.structure_cache.get(archive_key)
    if structure is None:
        structure = inspect_zip_stream(stream, size,
            max_members=max(1, budget.limits.max_members - budget.members),
            max_central_directory_bytes=budget.limits.max_central_directory_bytes)
        budget.members += structure.members
        if budget.members > budget.limits.max_members:
            raise ArchiveRebuildError("budget", "Archive structural member budget exhausted")
        budget.structure_cache[archive_key] = structure
    ordinal = entry["ordinal"]
    if type(ordinal) is not int or not 0 <= ordinal < structure.members:
        raise ArchiveRebuildError("ambiguous_chain", "member ordinal is unavailable")
    structure_entry = structure.entries[ordinal]
    if (structure_entry.header_offset != entry["header_offset"]
            or structure_entry.filename != entry["original_name"]
            or structure_entry.uncompressed_size != entry["declared_size"]
            or structure_entry.crc32 != entry["expected_crc32"]):
        raise ArchiveRebuildError("ambiguous_chain", "member structure does not match manifest")
    if (structure_entry.flags & 1 or structure_entry.uncompressed_size > budget.limits.max_member_bytes
            or structure_entry.uncompressed_size / max(1, structure_entry.compressed_size)
            > budget.limits.max_compression_ratio):
        raise ArchiveRebuildError("budget", "member encryption or expansion exceeds policy")
    with zipfile.ZipFile(stream) as archive:
        info = archive.infolist()[ordinal]
        if level + 1 < len(chain):
            if spool_root is None:
                spool_key = (source_key, entry["identity"])
                memory = budget.memory_spools.get(spool_key)
                if memory is None:
                    if entry["actual_size"] > budget.max_memory_spool_bytes - budget.memory_spool_bytes:
                        raise ArchiveRebuildError("scratch_required", "nested Archive exceeds the memory spool allowance")
                    memory = io.BytesIO()
                    try:
                        with archive.open(info) as member:
                            observed = _read_digest(member, budget,
                                max_bytes=min(budget.limits.max_member_bytes,
                                              budget.max_memory_spool_bytes - budget.memory_spool_bytes),
                                expanded=True, spool=memory)
                        _require_member_bytes(entry, observed)
                    except BaseException:
                        memory.close()
                        raise
                    budget.memory_spools[spool_key] = memory
                    budget.memory_spool_bytes += observed[0]
                memory.seek(0)
                return _verify_member(memory, entry["actual_size"], chain, budget, None,
                                      level + 1, source_key)
            spool_path = spool_root / f"member-{level}"
            with archive.open(info) as member, spool_path.open("x+b") as spool:
                observed = _read_digest(member, budget, max_bytes=budget.limits.max_member_bytes,
                                        expanded=True, spool=spool)
                _require_member_bytes(entry, observed)
                spool.flush()
                spool.seek(0)
                result = _verify_member(spool, observed[0], chain, budget, spool_root, level + 1, source_key)
            spool_path.unlink()
            return result
        with archive.open(info) as member:
            result = _read_digest(member, budget, max_bytes=budget.limits.max_member_bytes,
                                  expanded=True)
        _require_member_bytes(entry, result)
        return result


def _require_member_bytes(entry: Mapping[str, Any], observed: tuple[int, int, str]) -> None:
    if observed != (entry.get("actual_size"), entry.get("actual_crc32"), entry.get("sha256")):
        raise ArchiveRebuildError("member_changed", "member bytes differ from the durable manifest")


def _coerce_ref(ref: ArchiveManifestReference | Mapping[str, Any]) -> ArchiveManifestReference:
    return ref if isinstance(ref, ArchiveManifestReference) else ArchiveManifestReference.from_dict(ref)


def assess_archive_output_rebuildability(
    output_claim: str | Path | object,
    manifest_ref: ArchiveManifestReference | Mapping[str, Any],
    surviving_inputs: Iterable[ArchiveSurvivingInput | str | Path],
    budget: ArchiveRebuildBudget | None = None,
    *, retirement_set: Iterable[str | Path] = (),
) -> ArchiveRebuildProof:
    """Prove exact bytes from a surviving authorized source without materializing it."""
    effective = budget or ArchiveRebuildBudget()
    reference: ArchiveManifestReference | None = None
    raw_path = getattr(output_claim, "path", output_claim)
    proof = ArchiveRebuildProof(str(raw_path), None)
    try:
        effective.limits.validate()
        effective.check()
        if not isinstance(raw_path, (str, os.PathLike)):
            raise ArchiveRebuildError("unsafe_path", "output claim must provide a filesystem path")
        output_path = _absolute(raw_path)
        if hasattr(output_claim, "verified") and not output_claim.verified:
            raise ArchiveRebuildError("output_claim_unverified", "artifact claim is not verified")
        reference = _coerce_ref(manifest_ref)
        proof = replace(proof, manifest_ref=reference)
        manifest = _read_manifest(reference, effective)
        destination = _absolute(manifest["destination"])
        if destination not in output_path.parents:
            raise ArchiveRebuildError("output_outside_destination", "output is outside its manifest destination")
        matches = [item for item in manifest["outputs"] if item["absolute_path"] == str(output_path)
                   and item["status"] in {"applied", "reused"}]
        if len(matches) != 1:
            raise ArchiveRebuildError("output_unmapped", "output needs one exact published manifest entry")
        output = matches[0]
        if str(output_path.relative_to(destination)) != output["relative_path"]:
            raise ArchiveRebuildError("output_unmapped", "relative output path differs from its manifest")
        inputs = (item if isinstance(item, ArchiveSurvivingInput) else ArchiveSurvivingInput(item)
                  for item in surviving_inputs)
        targets = tuple(retirement_set)
        source_path, source_identity = _select_source(manifest, inputs, effective, targets)
        proof = replace(proof, source_path=str(source_path), source_identity=source_identity,
                        source_sha256=manifest["source_sha256"], source_is_outside_retirement_set=True)
        with _open_regular(output_path) as current:
            identity = _identity(os.fstat(current.fileno()))
            size, crc, digest = _read_digest(current, effective, max_bytes=effective.limits.max_input_bytes)
            if identity != _identity(os.fstat(current.fileno())):
                raise ArchiveRebuildError("output_changed", "output changed during verification")
        proof = replace(proof, output_identity=identity, output_sha256=digest)
        if digest != output["sha256"]:
            raise ArchiveRebuildError("output_changed", "current output differs from published bytes")
        is_unit = output["entry_identity"].startswith("archive-unit:")
        if is_unit:
            if manifest["classification"].get("unit_kind") not in {"office", "odf", "epub", "project"}:
                raise ArchiveRebuildError("ambiguous_chain", "whole-container output has no unit classification")
            if (size, digest) != (manifest["source_size"], manifest["source_sha256"]):
                raise ArchiveRebuildError("output_changed", "unit output differs from its surviving source")
            chain: tuple[dict[str, Any], ...] = ()
        else:
            chain = _member_chain(manifest, output["entry_identity"], effective)
            cache_key = (ARCHIVE_REBUILD_POLICY, reference.digest, reference.identity,
                         source_identity, tuple(item["identity"] for item in chain), identity, digest)
            cached_member = effective.member_cache.get(cache_key)
            with _open_regular(source_path) as original:
                if source_identity != _identity(os.fstat(original.fileno())):
                    raise ArchiveRebuildError("source_changed", "source changed before member verification")
                if cached_member is not None:
                    member = cached_member
                elif len(chain) > 1 and effective.scratch_directory is not None:
                    with _registered_scratch_workspace(effective.scratch_directory,
                            metadata={"operation": "archive_rebuild_proof"}) as workspace:
                        member = _verify_member(original, source_identity[3], chain, effective, workspace,
                                                source_key=manifest["source_sha256"])
                else:
                    member = _verify_member(original, source_identity[3], chain, effective, None,
                                            source_key=manifest["source_sha256"])
                if source_identity != _identity(os.fstat(original.fileno())):
                    raise ArchiveRebuildError("source_changed", "source changed while reading its member")
            effective.member_cache[cache_key] = member
            if member != (size, crc, digest):
                raise ArchiveRebuildError("output_changed", "member and current output bytes differ")
        effective.check()
        if _identity(source_path.lstat()) != source_identity or _identity(output_path.lstat()) != identity:
            raise ArchiveRebuildError("identity_changed", "source or output changed after proof")
        if _retiring(source_path, source_identity, targets):
            raise ArchiveRebuildError("source_selected_for_retirement", "source must survive the operation")
        _read_manifest(reference, effective)
        return replace(proof, member_identities=tuple(item["identity"] for item in chain),
                       verified_size=size, verified_crc32=crc, coverage="complete")
    except ArchiveRebuildError as exc:
        return replace(proof, blocker=exc.code, detail=str(exc))
    except (OSError, ValueError, TypeError, KeyError, zipfile.BadZipFile, ZipStructureError,
            RuntimeError, NotImplementedError, EOFError, zlib.error) as exc:
        return replace(proof, blocker="unverified", detail=f"{type(exc).__name__}: {exc}")


def revalidate_archive_rebuild_proof(
    proof: ArchiveRebuildProof,
    surviving_inputs: Iterable[ArchiveSurvivingInput | str | Path],
    budget: ArchiveRebuildBudget | None = None,
    *, retirement_set: Iterable[str | Path] = (),
) -> ArchiveRebuildProof:
    if not proof.rebuildable or proof.manifest_ref is None or proof.policy != ARCHIVE_REBUILD_POLICY:
        return replace(proof, blocker="proof_unverified", coverage="unproved")
    current = assess_archive_output_rebuildability(proof.output_path, proof.manifest_ref,
        surviving_inputs, budget, retirement_set=retirement_set)
    if current.rebuildable and current != proof:
        return replace(current, blocker="proof_fence_changed", coverage="unproved")
    return current


def assess_archive_materialization_rebuildability(
    destination: str | Path,
    manifest_refs: Iterable[ArchiveManifestReference | Mapping[str, Any]],
    surviving_inputs: Iterable[ArchiveSurvivingInput | str | Path],
    budget: ArchiveRebuildBudget | None = None,
    *, retirement_set: Iterable[str | Path] = (),
) -> ArchiveMaterializationRebuildProof:
    """Require exact directory coverage; unknown additions block the whole tree."""
    effective = budget or ArchiveRebuildBudget()
    outputs: list[ArchiveRebuildProof] = []
    unknown: list[str] = []
    directories: list[tuple[str, tuple[int, ...]]] = []
    try:
        effective.check()
        root = _absolute(destination)
        refs = tuple(dict.fromkeys(_coerce_ref(ref) for ref in manifest_refs))
        inputs = tuple(surviving_inputs)
        targets = tuple(retirement_set)
        if not refs:
            raise ArchiveRebuildError("manifest_unavailable", "materialization has no durable provenance")
        by_path: dict[str, ArchiveManifestReference] = {}
        known_dirs: set[str] = {str(root)}
        for ref in refs:
            manifest = _read_manifest(ref, effective)
            if manifest["destination"] != str(root):
                raise ArchiveRebuildError("destination_mismatch", "manifest belongs to another destination")
            for output in manifest["outputs"]:
                effective.check()
                if output["status"] not in {"applied", "reused"}:
                    continue
                path = _absolute(output["absolute_path"])
                if root not in path.parents:
                    raise ArchiveRebuildError("unsafe_path", "manifest output leaves destination")
                is_dir = any(entry["identity"] == output["entry_identity"] and entry["content_kind"] == "directory"
                             for entry in manifest["entries"])
                if is_dir:
                    known_dirs.add(str(path))
                else:
                    if str(path) in by_path:
                        raise ArchiveRebuildError("ambiguous_manifest", "more than one manifest claims an output")
                    by_path[str(path)] = ref
                known_dirs.update(str(parent) for parent in path.parents if parent == root or root in parent.parents)
        pending = [root]
        observed: set[str] = set()
        count = 0
        while pending:
            directory = pending.pop()
            effective.check()
            with _open_directory(directory) as descriptor:
                before = _identity(os.fstat(descriptor))
                directories.append((str(directory), before))
                with os.scandir(descriptor) as entries:
                    for entry in entries:
                        effective.check()
                        count += 1
                        if count > effective.limits.max_members * (effective.limits.max_depth + 1):
                            raise ArchiveRebuildError("budget", "output coverage exceeds its entry bound")
                        path = directory / entry.name
                        metadata = entry.stat(follow_symlinks=False)
                        if stat.S_ISDIR(metadata.st_mode):
                            if str(path) not in known_dirs:
                                unknown.append(str(path))
                            else:
                                pending.append(path)
                        elif not stat.S_ISREG(metadata.st_mode) or str(path) not in by_path:
                            unknown.append(str(path))
                        else:
                            observed.add(str(path))
                if before != _identity(os.fstat(descriptor)):
                    raise ArchiveRebuildError("output_tree_changed", "output directory changed during coverage scan")
        if known_dirs.difference(directory for directory, _identity_value in directories):
            raise ArchiveRebuildError("output_missing", "manifest output directory is absent")
        for output_path_key, ref in by_path.items():
            if output_path_key not in observed:
                raise ArchiveRebuildError("output_missing", "manifest output is absent")
            outputs.append(assess_archive_output_rebuildability(output_path_key, ref, inputs, effective,
                                                                retirement_set=targets))
        for output_proof in outputs:
            effective.check()
            proof_source_path = output_proof.source_path
            if output_proof.rebuildable and (
                proof_source_path is None
                or _identity(Path(proof_source_path).lstat()) != output_proof.source_identity
                or _identity(Path(output_proof.output_path).lstat()) != output_proof.output_identity
            ):
                raise ArchiveRebuildError("proof_fence_changed", "source or output changed during tree proof")
        for directory_name, identity in directories:
            if _identity(Path(directory_name).lstat()) != identity:
                raise ArchiveRebuildError("output_tree_changed", "output directory changed during proof")
        blockers = tuple(sorted({p.blocker for p in outputs if p.blocker is not None}))
        return ArchiveMaterializationRebuildProof(str(root), tuple(outputs), tuple(unknown), blockers,
                                                  tuple(directories))
    except ArchiveRebuildError as exc:
        return ArchiveMaterializationRebuildProof(str(destination), tuple(outputs), tuple(unknown),
                                                  (exc.code,), tuple(directories))
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return ArchiveMaterializationRebuildProof(str(destination), tuple(outputs), tuple(unknown),
                                                  (f"unverified:{type(exc).__name__}",), tuple(directories))


def _output_artifact_id(path: Path, registry: Any) -> str:
    base = "archive-output-" + hashlib.sha256(os.fsencode(path)).hexdigest()
    existing = registry.verify(base)
    if existing.verified and existing.state != "retired":
        return base
    if registry.manifest_path(base).exists():
        # A physically reconstructed output is a new artifact generation.
        # Preserve the retired claim; never rebind its historical identity.
        generation = hashlib.sha256(repr(_identity(path.lstat())).encode("ascii")).hexdigest()[:24]
        return base + "-" + generation
    return base


class ArchiveManifestStore:
    """Persist full immutable manifests, write-ahead events and registry claims."""
    def __init__(self, root: str | Path, *, artifact_registry_root: str | Path | None = None) -> None:
        self.root = _absolute(root)
        self.artifact_registry_root = _absolute(artifact_registry_root or self.root / "artifacts")
        self.reference: ArchiveManifestReference | None = None
        self.current: ArchiveManifest | None = None
        self.operation_id = uuid.uuid4().hex
        self.journal_path = self.root / f"operation-{self.operation_id}.jsonl"
        self.registry: Any = None

    def _registry(self) -> Any:
        if self.registry is None:
            from neocortex.runtime.artifact_registry import ArtifactRegistry
            self.registry = ArtifactRegistry(self.artifact_registry_root,
                owner=ARCHIVE_PROVENANCE_OWNER, create_root=True)
        return self.registry

    def publish(self, manifest: ArchiveManifest) -> ArchiveManifestReference:
        if manifest.manifest_digest != _digest_manifest(manifest):
            raise ArchiveRebuildError("manifest_changed", "manifest digest is not current")
        path = self.root / f"manifest-{manifest.manifest_digest}.json"
        _publish_json(path, manifest.to_dict())
        artifact_id = f"archive-manifest-{manifest.manifest_digest}"
        registry = self._registry()
        existing = registry.verify(artifact_id)
        if existing.verified:
            if (existing.path != path or existing.digest != manifest.manifest_digest
                    or existing.owner != ARCHIVE_PROVENANCE_OWNER
                    or existing.purpose != "archive-output-provenance"):
                raise ArchiveRebuildError("manifest_claim_changed", "manifest registration differs")
        else:
            registry.register(artifact_id, producer="archive", path=path, root=self.root,
                kind="canonical", state="active", disposable=False,
                digest=manifest.manifest_digest, purpose="archive-output-provenance",
                metadata={"manifest_digest": manifest.manifest_digest,
                          "destination": manifest.destination, "policy": ARCHIVE_REBUILD_POLICY})
        self.reference = ArchiveManifestReference(str(path), manifest.manifest_digest,
                                                  _identity(path.lstat()), artifact_id)
        self.current = manifest
        return self.reference

    def begin(self, manifest: ArchiveManifest) -> None:
        reference = self.publish(manifest)
        with _open_directory(self.root) as parent:
            descriptor = os.open(self.journal_path.name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            os.close(descriptor)
            os.fsync(parent)
        self._registry().register(f"archive-journal-{self.operation_id}", producer="archive",
            path=self.journal_path, root=self.root, kind="canonical", state="active",
            disposable=False, purpose="archive-materialization-recovery",
            dependencies=(reference.artifact_id,), metadata={"policy": ARCHIVE_REBUILD_POLICY})
        self.journal({"event": "manifest", "manifest_ref": reference.to_dict()})

    def _register_output(self, event: Mapping[str, Any]) -> None:
        if self.reference is None or self.current is None or event.get("status") not in {"applied", "reused"}:
            return
        destination = self.current.destination
        if destination is None:
            raise ArchiveRebuildError("manifest_invalid", "published output needs a manifest destination")
        path = _absolute(event["absolute_path"])
        if stat.S_ISDIR(path.lstat().st_mode):
            # Directories are covered by the exact tree proof; registering
            # them separately would permit retiring an unproved subtree.
            return
        registry = self._registry()
        key = _output_artifact_id(path, registry)
        metadata = {"manifest_ref": self.reference.to_dict(), "destination": self.current.destination,
                    "relative_path": event["relative_path"], "entry_identity": event["entry_identity"],
                    "source_path": self.current.source_path, "source_sha256": self.current.source_sha256,
                    "policy": ARCHIVE_REBUILD_POLICY}
        existing = registry.verify(key)
        if existing is not None and existing.verified:
            if existing.metadata.get("entry_identity") != event["entry_identity"]:
                raise ArchiveRebuildError("output_claim_changed", "existing output belongs to another member")
            registry.update(key, dependencies=(self.reference.artifact_id,), metadata=metadata,
                            digest=event.get("sha256"))
        else:
            registry.register(key, producer="archive", path=path, root=Path(destination),
                kind="rebuildable", state="active", disposable=False,
                digest=event.get("sha256"), purpose="archive-materialized-output",
                dependencies=(self.reference.artifact_id,), metadata=metadata)

    def journal(self, event: dict[str, Any]) -> None:
        encoded = _canonical(event) + b"\n"
        if len(encoded) > MAX_ARCHIVE_MANIFEST_BYTES:
            raise ArchiveRebuildError("manifest_budget", "journal event exceeds its bound")
        with _open_directory(self.root) as parent:
            descriptor = os.open(self.journal_path.name, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW,
                                 dir_fd=parent)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
        if event.get("event") == "materialization":
            self._register_output(event)

    def finish(self, manifest: ArchiveManifest) -> ArchiveManifest:
        ref = self.publish(manifest)
        for output in manifest.outputs:
            self._register_output(output.to_dict())
        self.journal({"event": "complete", "manifest_ref": ref.to_dict()})
        return replace(manifest, manifest_path=ref.path)


def archive_manifest_surviving_inputs(
    manifest_refs: Iterable[ArchiveManifestReference | Mapping[str, Any]],
    *, authorized_roots: Iterable[str | Path] = (),
    authorized_locations: Iterable[str | Path] = (),
    budget: ArchiveRebuildBudget | None = None,
) -> tuple[ArchiveSurvivingInput, ...]:
    """Resolve only persisted source locations and explicitly indexed alternatives.

    An arbitrary manifest path does not grant access to a source. The caller
    supplies its already authorized corpus roots or exact catalog locations;
    this helper never searches directories, disks or matching basenames.
    """
    effective = budget or ArchiveRebuildBudget()
    roots = tuple(_absolute(root) for root in authorized_roots)
    locations = {_absolute(path) for path in authorized_locations}
    result: dict[str, ArchiveSurvivingInput] = {}
    for ref in manifest_refs:
        manifest = _read_manifest(_coerce_ref(ref), effective)
        source = _absolute(manifest["source_path"])
        if source in locations or any(root == source or root in source.parents for root in roots):
            result[str(source)] = ArchiveSurvivingInput(source, manifest["source_sha256"])
    for path in locations:
        result.setdefault(str(path), ArchiveSurvivingInput(path))
    return tuple(result.values())


@contextmanager
def archive_output_retirement_guard(
    proof: ArchiveRebuildProof,
    registry: Any,
    surviving_inputs: Iterable[ArchiveSurvivingInput | str | Path],
    budget: ArchiveRebuildBudget | None = None,
    *, retirement_set: Iterable[str | Path] = (),
) -> Iterator[Any]:
    """Apply a caller-authorized output retirement under fresh owner evidence.

    Eligibility exists only in this guard's memory. Durable records retain
    active/non-disposable protection even if the process dies before intent
    publication. The common registry still verifies dependencies and records
    its write-ahead retirement intent before the physical effect.
    """
    from neocortex.runtime.artifact_registry import ArtifactRecord, ArtifactRegistry

    if registry.owner != ARCHIVE_PROVENANCE_OWNER or proof.manifest_ref is None:
        raise ArchiveRebuildError("output_claim_changed", "an exact Archive registry owner is required")
    inputs = tuple(surviving_inputs)
    targets = tuple(retirement_set)
    key = _output_artifact_id(Path(proof.output_path), registry)
    reference = json.loads(json.dumps(proof.manifest_ref.to_dict()))

    class _ArchiveProofRegistry(ArtifactRegistry):
        def _classify_for_owner(self, record: Any, *, now_ns: int) -> tuple[str, str]:
            category, reason = super()._classify_for_owner(record, now_ns=now_ns)
            if (category == "protected" and reason == "state_active"
                    and record.artifact_id == key and record.kind == "rebuildable"
                    and record.owner == ARCHIVE_PROVENANCE_OWNER
                    and record.purpose == "archive-materialized-output"
                    and str(record.path) == proof.output_path
                    and record.digest == proof.output_sha256
                    and record.metadata.get("manifest_ref") == reference):
                return "eligible", "archive_rebuild_proved"
            return category, reason

    guarded_registry = _ArchiveProofRegistry(registry.root, owner=registry.owner,
        max_records=registry.max_records, max_bytes=registry.max_bytes,
        max_manifest_bytes=registry.max_manifest_bytes, max_metadata_bytes=registry.max_metadata_bytes)
    # Share the coordinator's exact nested lock descriptor, as for_owner does.
    guarded_registry._lock_local = registry._lock_local
    with guarded_registry.retirement_batch_guard() as batch:
        current_proof = revalidate_archive_rebuild_proof(proof, inputs, budget,
                                                        retirement_set=targets)
        if not current_proof.rebuildable:
            raise ArchiveRebuildError(current_proof.blocker or "proof_unverified",
                                      current_proof.detail or "Archive rebuild proof is no longer valid")
        record = guarded_registry.verify(key)
        if (not isinstance(record, ArtifactRecord) or not record.verified
                or record.owner != ARCHIVE_PROVENANCE_OWNER
                or record.purpose != "archive-materialized-output"
                or record.metadata.get("manifest_ref") != reference):
            raise ArchiveRebuildError("output_claim_changed", "registered output does not match this proof")
        with batch.guard(key) as guarded:
            effective = budget or ArchiveRebuildBudget()
            effective.check()
            proof_source_path = proof.source_path
            if (_identity(Path(proof.output_path).lstat()) != proof.output_identity
                    or proof_source_path is None
                    or _identity(Path(proof_source_path).lstat()) != proof.source_identity):
                raise ArchiveRebuildError("proof_fence_changed", "Archive source or output changed at effect boundary")
            yield guarded
        if os.path.lexists(proof.output_path):
            latest = guarded_registry.verify(key)
            if isinstance(latest, ArtifactRecord) and latest.verified and latest.state == "active":
                guarded_registry._clear_retirement_intent_locked(latest)
