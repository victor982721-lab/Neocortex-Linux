"""Synthetic physical producer fixed point before the real SHA-256 planner."""

from __future__ import annotations

import io
import json
from dataclasses import replace
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace
import zipfile

from neocortex.runtime.models import FrameworkConfig
from neocortex.runtime.orchestration.orchestrator import FrameworkOrchestrator
from neocortex.runtime.orchestration.orchestrator_pipeline import InitialPipelineMixin
from neocortex.runtime.orchestration.route_registry import RouteAdapter
from neocortex.workflow.mutations import BackendOutcome
from tests.test_framework_actions import _fixture_trash_receipt


def _zip(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def _email(entries):
    message = EmailMessage()
    message["From"] = "fixture@example.invalid"
    message["To"] = "fixture@example.invalid"
    message["Subject"] = "Contained admission fixture"
    message.set_content("Synthetic body with useful attachments.")
    for name, content in entries.items():
        message.add_attachment(content, maintype="application", subtype="octet-stream", filename=name)
    return message.as_bytes()


def _backend(monkeypatch, tmp_path):
    effects = []
    class FixtureTrash:
        supports_empty_directories = True

        def apply_snapshot(self, snapshot, *, root, source_digest, object_kind="regular_file", **_kwargs):
            assert Path(snapshot.path).is_relative_to(root)
            target = tmp_path / "trash" / str(len(effects))
            effects.append(snapshot.path)
            receipt = json.loads(_fixture_trash_receipt(snapshot, source_digest, target))
            receipt["object_kind"] = object_kind
            receipt["trash"]["object_kind"] = object_kind
            return BackendOutcome("applied", "fixture_verified", receipt_json=json.dumps(receipt))

        def apply_many_snapshots(self, items, *, root):
            return tuple(self.apply_snapshot(item[0], root=root, source_digest=item[1],
                                             object_kind=item[2] if len(item) > 2 else "regular_file") for item in items)

    monkeypatch.setattr("neocortex.workflow.mutations.KioTrashBackend", FixtureTrash)
    monkeypatch.setattr("neocortex.safety.kio_trash.preflight_kio_trash",
                        lambda: SimpleNamespace(client="contained-fixture"))
    # Unit scope: the approved installed launcher is validated separately.
    # This private tooling interpreter exercises SQLite owners, not release
    # path accreditation or a real desktop Trash service.
    monkeypatch.setattr("neocortex.platform.sqlite_runtime_attestation.observe_platform_native_runtime",
                        lambda **_kwargs: {"status": "approved", "observed": {}})
    return effects


def _config(root, state):
    return FrameworkConfig(root=root, state_directory=state, route="all", apply_actions=True,
                           document_catalog_enabled=False, global_cpu_slots=2,
                           global_min_free_memory_bytes=0, global_min_free_commit_bytes=0)


def test_nested_zip_email_children_all_cross_gate_before_real_planner(tmp_path, monkeypatch):
    root = tmp_path / "corpus"
    root.mkdir()
    pdf = b"%PDF-1.7\nuseful original\n%%EOF\n"
    (root / "useful.pdf").write_bytes(pdf)
    (root / "duplicate.pdf").write_bytes(pdf)
    (root / "wrong-extension.dll").write_bytes(b"%PDF-1.7\nwrong suffix useful\n%%EOF\n")
    (root / "junk.exe").write_bytes(b"\x00runtime original")
    nested = _zip({"useful3.pdf": b"%PDF-1.7\nuseful three\n%%EOF\n", "runtime.exe": b"\x00runtime child"})
    (root / "archive.zip").write_bytes(_zip({
        "useful2.txt": b"useful second text\n", "junk.dll": b"\x00junk child",
        "message.eml": _email({"attachment.zip": nested}),
    }))
    (root / "message2.eml").write_bytes(_email({
        "useful4.pdf": b"%PDF-1.7\nuseful four\n%%EOF\n", "runtime.dll": b"\x00runtime from email",
    }))
    effects = _backend(monkeypatch, tmp_path)
    gate_observations = []
    planner = InitialPipelineMixin._plan_initial_dedup

    def before_planner(self, state, run_id, index, scan_id):
        names = {Path(snapshot.path).name for snapshot in index.snapshots(scan_id)}
        assert {"useful.pdf", "duplicate.pdf", "wrong-extension.pdf", "useful2.txt", "useful3.pdf"} <= names
        assert any(name.endswith("useful4.pdf") for name in names)
        assert not any(name.endswith((".exe", ".dll", ".zip")) for name in names)
        assert self._curation_admission_result["pending_physical_admission"] == 0
        assert self._curation_admission_result["rounds"] == 2
        gate_observations.append(names)
        return planner(self, state, run_id, index, scan_id)

    monkeypatch.setattr(InitialPipelineMixin, "_plan_initial_dedup", before_planner)
    result = FrameworkOrchestrator(_config(root, tmp_path / "state"), route_registry={
        "text": RouteAdapter("text", lambda _context: {"fixture": "route-boundary-only"}),
    }).run()
    assert len(gate_observations) == 1
    assert result.dedup_plan.group_count == 1
    assert result.actions.duplicates_trashed == 1
    assert result.route_results["curation_admission"]["expensive_work_avoided"] == 4
    assert result.route_results["corpus_verification"]["passed"], result.route_results["corpus_verification"]
    assert not result.route_failures, result.route_failures
    assert {path.name for path in root.iterdir()} == {"Corpus_ordenado", "Sin_clasificar"}
    assert len([path for path in effects if Path(path).suffix == ".zip"]) == 2


def test_more_than_256_email_children_are_curated_not_presentation_sample(tmp_path, monkeypatch):
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "message.eml").write_bytes(_email({
        f"runtime-{number}.dll": b"\x00runtime " + str(number).encode() for number in range(263)
    }))
    effects = _backend(monkeypatch, tmp_path)
    planner = InitialPipelineMixin._plan_initial_dedup
    seen = []

    def before_planner(self, state, run_id, index, scan_id):
        survivors = tuple(index.snapshots(scan_id))
        assert [Path(item.path).name for item in survivors] == ["message.eml"]
        seen.append(self._curation_admission_result.copy())
        return planner(self, state, run_id, index, scan_id)

    monkeypatch.setattr(InitialPipelineMixin, "_plan_initial_dedup", before_planner)
    FrameworkOrchestrator(_config(root, tmp_path / "state"), route_registry={
        "text": RouteAdapter("text", lambda _context: {"fixture": "route-boundary-only"}),
    }).run()
    assert seen[0]["redlist_trashed"] == 263
    assert len([path for path in effects if path.endswith(".dll")]) == 263


def test_preview_does_not_rename_trash_extract_or_create_final_roots(tmp_path, monkeypatch):
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "empty/subdirectory").mkdir(parents=True)
    (root / "report.dll.~2~").write_bytes(b"%PDF-1.7\nuseful misleading suffix\n%%EOF\n")
    (root / "runtime.exe").write_bytes(b"\x00software runtime")
    (root / "archive.zip").write_bytes(_zip({"inside.txt": b"synthetic content\n"}))
    (root / "message.eml").write_bytes(_email({"attachment.pdf": b"%PDF-1.7\nchild\n%%EOF\n"}))
    before = {str(path.relative_to(root)): (path.stat().st_ino, path.read_bytes() if path.is_file() else None)
              for path in root.rglob("*")}
    effects = _backend(monkeypatch, tmp_path)
    config = replace(_config(root, tmp_path / "state"), apply_actions=False)
    result = FrameworkOrchestrator(config, route_registry={
        "text": RouteAdapter("text", lambda _context: {"fixture": "preview-route-boundary"}),
    }).run()
    after = {str(path.relative_to(root)): (path.stat().st_ino, path.read_bytes() if path.is_file() else None)
             for path in root.rglob("*")}
    assert after == before
    assert not effects
    assert not (root / "Corpus_ordenado").exists()
    assert not (root / "Sin_clasificar").exists()
    assert result.actions.files_renamed == 0
    assert result.actions.duplicates_trashed == 0
    assert result.actions.empty_directories_trashed == 0
    assert result.route_results["curation_admission"]["redlist_matched"] == 1


def test_empty_survivor_has_identity_bound_unknown_mime_and_no_guess(tmp_path, monkeypatch):
    root = tmp_path / "corpus"
    root.mkdir()
    # The existing empty-file policy protects legal metadata names. Such a
    # legitimate zero-byte survivor still needs a current UNKNOWN decision.
    source = root / "LICENSE"
    source.touch()
    original_identity = source.stat().st_ino
    _backend(monkeypatch, tmp_path)
    result = FrameworkOrchestrator(_config(root, tmp_path / "state"), route_registry={
        "text": RouteAdapter("text", lambda _context: {"fixture": "empty-source"}),
    }).run()
    target = root / "Sin_clasificar/_MIME/application/octet-stream/LICENSE"
    assert target.is_file()
    assert target.stat().st_ino == original_identity
    assert target.read_bytes() == b""
    assert not result.route_failures, result.route_failures


def test_blocked_normalization_settles_as_excluded_without_readmission_loop(tmp_path, monkeypatch):
    root = tmp_path / "corpus"
    root.mkdir()
    source = root / "report.dll"
    payload = b"%PDF-1.7\nuseful document with blocked rename\n%%EOF\n"
    source.write_bytes(payload)
    effects = _backend(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "neocortex.workflow.mutations.PosixRenameBackend.apply",
        lambda *_args, **_kwargs: BackendOutcome("blocked", "fixture_rename_blocked"),
    )
    observations = []
    planner = InitialPipelineMixin._plan_initial_dedup

    def before_planner(self, state, run_id, index, scan_id):
        snapshots = tuple(index.snapshots(scan_id))
        assert len(snapshots) == 1
        assert self._curation_admission_check(snapshots[0]) is False
        observations.append(self._curation_admission_result.copy())
        return planner(self, state, run_id, index, scan_id)

    monkeypatch.setattr(InitialPipelineMixin, "_plan_initial_dedup", before_planner)
    result = FrameworkOrchestrator(_config(root, tmp_path / "state"), route_registry={
        "text": RouteAdapter("text", lambda _context: {"fixture": "blocked-normalize"}),
    }).run()
    assert len(observations) == 1
    assert observations[0]["rounds"] == 1
    assert observations[0]["exclusion_reasons"] == {"normalization_incomplete": 1}
    assert source.read_bytes() == payload
    assert not effects
    assert result.route_failures["curation-admission"] == "partial"
    assert result.route_results["residual_mime"]["moved"] == 0
