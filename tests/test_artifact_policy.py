from __future__ import annotations

import sqlite3
import struct
import zipfile
from pathlib import Path

from neocortex.platform.content_types import DetectedType
from neocortex.workflow.actions.artifact_policy import ArtifactPolicy
from neocortex.workflow.actions.action_effects import EffectsActionsMixin
from neocortex.deduplication import FileSnapshot


def _decision(path: Path):
    return ArtifactPolicy().evaluate(path)


def _valid_elf() -> bytes:
    header = bytearray(64)
    header[:4] = b"\x7fELF"
    header[4:7] = b"\x02\x01\x01"
    struct.pack_into("<HHI", header, 16, 2, 0x3E, 1)
    struct.pack_into("<QQ", header, 32, 0, 0)
    struct.pack_into("<HHHHH", header, 52, 64, 56, 0, 64, 0)
    return bytes(header)


def _valid_pe() -> bytes:
    image = bytearray(400)
    image[:2] = b"MZ"
    struct.pack_into("<I", image, 0x3C, 64)
    image[64:68] = b"PE\0\0"
    struct.pack_into("<HHIIIHH", image, 68, 0x8664, 1, 0, 0, 0, 0xF0, 0x0002)
    struct.pack_into("<H", image, 88, 0x10B)
    return bytes(image)


def test_artifact_policy_requires_codex_rollout_structure(tmp_path: Path) -> None:
    name = "rollout-2026-09-28T12-00-00-01a078a2-42fd-7971-9f91-0fa60cfc8fbb.jsonl"
    candidate = tmp_path / name
    candidate.write_text('{"type":"session_meta","payload":{"id":"x","model_provider":"codex"}}\n', encoding="utf-8")
    decision = _decision(candidate)
    assert decision.disposition == "trash"
    assert decision.rule_id == "artifact.codex-rollout.v1"
    assert decision.preview["action"] == "trash"
    assert decision.evidence["rule_id"] == decision.rule_id

    documentary = tmp_path / "rollout-2026-09-28T12-00-00-01a078a2-42fd-7971-9f91-0fa60cfc8faa.jsonl"
    documentary.write_text('{"type":"document","title":"rollout"}\n', encoding="utf-8")
    assert _decision(documentary).rule_id == "keep.codex-rollout.structure-unconfirmed"


def test_cache_database_requires_sqlite_and_cache_schema(tmp_path: Path) -> None:
    cache = tmp_path / "cache.0.db"
    connection = sqlite3.connect(cache)
    connection.execute("CREATE TABLE cache_entries (key TEXT, value BLOB)")
    connection.commit()
    connection.close()
    decision = _decision(cache)
    assert decision.disposition == "trash"
    assert decision.rule_id == "artifact.sqlite-cache.v1"

    unknown = tmp_path / "cache.1.db"
    connection = sqlite3.connect(unknown)
    connection.execute("CREATE TABLE invoices (id INTEGER PRIMARY KEY)")
    connection.commit()
    connection.close()
    assert _decision(unknown).disposition == "keep"

    active = tmp_path / "cache.2.db"
    connection = sqlite3.connect(active)
    connection.execute("CREATE TABLE cache_entries (key TEXT)")
    connection.commit()
    connection.close()
    (tmp_path / "cache.2.db-wal").write_bytes(b"active-wal")
    assert _decision(active).disposition == "keep"


def test_unknown_and_personal_sqlite_are_preserved(tmp_path: Path) -> None:
    for name in ("History", "Cookies", "Login Data", "Bookmarks", "messages.json", "records.db"):
        candidate = tmp_path / name
        candidate.write_bytes(b"SQLite format 3\x00unknown")
        decision = _decision(candidate)
        assert decision.disposition == "keep"
        if name != "records.db":
            assert "protect" in decision.rule_id
    backup_name = tmp_path / "History.~1~"
    backup_name.write_text("valuable", encoding="utf-8")
    assert _decision(backup_name).rule_id == "protect.personal-data-name"


def test_appx_and_strong_magic_are_structural(tmp_path: Path) -> None:
    manifest = tmp_path / "AppxManifest.xml"
    manifest.write_text(
        '<Package xmlns="http://schemas.microsoft.com/appx/manifest/foundation/windows10">'
        '<Identity Name="fixture"/><Applications/></Package>',
        encoding="utf-8",
    )
    assert _decision(manifest).rule_id == "artifact.appx-metadata.v1"
    misleading_manifest = tmp_path / "AppxManifest.xml.~1~"
    misleading_manifest.write_text(
        "<Document>Package http://schemas.microsoft.com/appx/manifest/foundation/windows10</Document>",
        encoding="utf-8",
    )
    assert _decision(misleading_manifest).disposition == "keep"

    executable = tmp_path / "without-extension"
    executable.write_bytes(_valid_pe())
    executable_decision = _decision(executable)
    assert executable_decision.rule_id == "artifact.strong-magic.pe"
    assert executable_decision.content_proof is not None

    weak = tmp_path / "weak-magic"
    weak.write_bytes(b"MZ" + b"\0" * 398)
    assert _decision(weak).disposition == "keep"


def test_wheels_fixtures_and_sensitive_names_are_never_trash_candidates(tmp_path: Path) -> None:
    wheel = tmp_path / "package.whl"
    wheel.write_bytes(_valid_pe())
    fixture = tmp_path / "binaryfixtures" / "fixture.bin"
    fixture.parent.mkdir()
    fixture.write_bytes(_valid_elf())
    license_file = tmp_path / "licenses" / "runtime.bin"
    license_file.parent.mkdir()
    license_file.write_bytes(_valid_elf())
    assert _decision(wheel).disposition == "keep"
    assert _decision(fixture).disposition == "keep"
    assert _decision(license_file).disposition == "keep"


def test_generated_directory_name_does_not_override_document_evidence(tmp_path: Path) -> None:
    pdf = tmp_path / ".pytest_cache" / "report.pdf"
    pdf.parent.mkdir()
    pdf.write_bytes(b"%PDF-1.7\nuseful report")
    assert _decision(pdf).disposition == "keep"

    metadata_dir = tmp_path / "mirror.dist-info"
    metadata_dir.mkdir()
    report = metadata_dir / "report.pdf"
    report.write_bytes(b"%PDF-1.7\nuseful report")
    assert _decision(report).disposition == "keep"


def test_effect_guard_uses_logical_wheel_suffix() -> None:
    snapshot = FileSnapshot("/corpus/package.whl.~1~", 1, 2, 3, 4, -1)
    assert EffectsActionsMixin()._effect_preservation_reason(snapshot) == "retained_package_archive"


def test_typed_detection_is_evidence_not_semantic_authority(tmp_path: Path) -> None:
    path = tmp_path / "History"
    path.write_text("not a database", encoding="utf-8")
    detected = DetectedType(
        "application/x-executable",
        ".exe",
        frozenset({".exe"}),
        "magic:pe",
    )
    decision = ArtifactPolicy().evaluate(path, detected)
    assert decision.disposition == "keep"
    assert decision.rule_id == "protect.personal-data-name"


def test_package_metadata_requires_direct_distribution_relation(tmp_path: Path) -> None:
    dist_info = tmp_path / "package-1.0.dist-info"
    dist_info.mkdir()
    metadata = dist_info / "METADATA"
    metadata.write_text("Metadata-Version: 2.1\nName: package\n", encoding="utf-8")
    assert _decision(metadata).rule_id == "artifact.package-metadata.relation-v1"

    nested = dist_info / "docs" / "METADATA"
    nested.parent.mkdir()
    nested.write_text("documentary metadata\n", encoding="utf-8")
    assert _decision(nested).disposition == "keep"

    # A PDF with a misleading metadata-like basename is not a package record.
    misleading_named = dist_info / "METADATA"
    misleading_named.write_bytes(b"%PDF-1.7\nuseful document")
    assert _decision(misleading_named).disposition == "keep"


def test_common_documents_do_not_capture_artifact_proofs(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[str] = []
    real_capture = __import__(
        "neocortex.workflow.actions.artifact_policy",
        fromlist=["capture_artifact_content_proof"],
    ).capture_artifact_content_proof

    def capture(source, **kwargs):
        calls.append(str(getattr(source, "path", source)))
        return real_capture(source, **kwargs)

    monkeypatch.setattr(
        "neocortex.workflow.actions.artifact_policy.capture_artifact_content_proof",
        capture,
    )
    documents = []
    for index in range(8):
        path = tmp_path / f"report-{index}.pdf"
        path.write_bytes(b"%PDF-1.7\nuseful\n")
        documents.append(path)
    for path in documents:
        assert _decision(path).disposition == "keep"
    assert calls == []

    rollout = tmp_path / "rollout-2026-09-28T12-00-00-01a078a2-42fd-7971-9f91-0fa60cfc8fbb.jsonl"
    rollout.write_text(
        '{"type":"session_meta","payload":{"id":"x","model_provider":"codex"}}\n',
        encoding="utf-8",
    )
    assert _decision(rollout).content_proof is not None
    assert calls == [str(rollout)]


def test_archive_runtime_classification_requires_bounded_structure(tmp_path: Path) -> None:
    policy = ArtifactPolicy()
    sdk = tmp_path / "SDK.zip"
    runtime = policy.evaluate_archive_members(
        sdk,
        ("sdk/bin/tool", "sdk/lib/libcore.so", "sdk/include/core.h"),
        member_signatures={"sdk/lib/libcore.so": "elf"},
    )
    assert runtime.disposition == "keep"
    assert runtime.rule_id == "artifact.archive.runtime-structure.v1"

    useful = policy.evaluate_archive_members(sdk, ("docs/readme.md", "photos/a.jpg"))
    assert useful.disposition == "keep"

    forged = policy.evaluate_archive_members(
        sdk,
        ("sdk/lib/libcore.so",),
        member_signatures={"missing/lib.so": "elf"},
    )
    assert forged.disposition == "keep"
    assert forged.rule_id == "keep.archive-signatures-unbound"

    assert policy.evaluate_archive_members(
        tmp_path / "SDK.whl",
        ("pkg.dist-info/METADATA", "pkg/__init__.py"),
    ).disposition == "keep"

    docs = policy.evaluate_archive_members(
        sdk,
        ("sdk/bin/tool", "sdk/lib/libcore.so", "reports/acceptance.pdf"),
        member_signatures={"sdk/lib/libcore.so": "elf"},
    )
    assert docs.disposition == "keep"


def test_held_zip_context_reaches_proof_authorized_artifact_trash(tmp_path: Path) -> None:
    source = tmp_path / "SDK.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("sdk/bin/tool.exe", _valid_pe())
        archive.writestr("sdk/lib/libcore.so", _valid_elf())
    detected = DetectedType(
        "application/zip",
        ".zip",
        frozenset({".zip"}),
        "zip:intake-artifact-runtime-hold",
    )
    decision = ArtifactPolicy().evaluate(
        source,
        detected,
    )
    assert decision.disposition == "trash"
    assert decision.content_proof is not None
