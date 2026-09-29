"""Physical ZIP publication precedes Identify and route candidate consumers."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from neocortex.progress import RecordingProgress
from neocortex.runtime.models import FrameworkConfig
from neocortex.runtime.orchestration.orchestrator import FrameworkOrchestrator
from neocortex.runtime.orchestration.route_registry import RouteAdapter, RouteExecutionContext
from neocortex.safety.route_filters import CandidateSelection
from neocortex.workflow.actions.actions import FrameworkActions
from neocortex.workflow import mutations, zip_intake_orchestrator
from neocortex.workflow.mutations import BackendOutcome
from tests.test_framework_actions import _fixture_trash_receipt


def test_all_expands_nested_zip_before_identify_and_routes_and_replays_safely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    nested = io.BytesIO()
    with zipfile.ZipFile(nested, "w") as archive:
        archive.writestr("document.txt", b"nested text evidence\n")
    source = root / "bundle.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("folder/inner.zip", nested.getvalue())
        archive.writestr("root.txt", b"root text evidence\n")
    original = source.read_bytes()
    # A packaged document must remain atomic, rather than being unpacked.
    atomic = root / "atomic.docx"
    with zipfile.ZipFile(atomic, "w") as archive:
        archive.writestr("[Content_Types].xml", b"<Types/>")
        archive.writestr("word/document.xml", b"<document/>")
    atomic_original = atomic.read_bytes()
    trash = tmp_path / "fixture-trash"
    trash.mkdir()
    effects: list[Path] = []
    order: list[str] = []
    expected = {
        root / "bundle/root.txt": b"root text evidence\n",
        root / "bundle/folder/inner/document.txt": b"nested text evidence\n",
    }

    class FixtureTrash:
        supports_empty_directories = True

        def apply_snapshot(self, snapshot, *, root, source_digest, object_kind="regular_file", **kwargs):
            path = Path(snapshot.path)
            assert source_digest and path.is_relative_to(root)
            assert path == source or object_kind == "empty_directory"
            if path == source:
                for extracted, content in expected.items():
                    assert extracted.read_bytes() == content
                order.append("zip-published")
            receipt = json.loads(_fixture_trash_receipt(snapshot, source_digest, trash / str(len(effects))))
            receipt["object_kind"] = object_kind
            receipt["trash"]["object_kind"] = object_kind
            effects.append(path)
            return BackendOutcome("applied", "fixture", receipt_json=json.dumps(receipt))

        def apply_many_snapshots(self, items, *, root):
            return tuple(self.apply_snapshot(item[0], root=root, source_digest=item[1],
                                            object_kind=item[2] if len(item) > 2 else "regular_file")
                         for item in items)

    monkeypatch.setattr(mutations, "KioTrashBackend", FixtureTrash)
    # This test exercises contained ZIP effects, not host accreditation or
    # the desktop's real Trash backend. Those owners have independent tests.
    monkeypatch.setattr(
        "neocortex.platform.sqlite_runtime_attestation.observe_platform_native_runtime",
        lambda **kwargs: {"status": "approved", "observed": {}},
    )
    monkeypatch.setattr(
        "neocortex.safety.kio_trash.preflight_kio_trash",
        lambda: SimpleNamespace(client="fixture-trash"),
    )
    identify = FrameworkActions.identify_and_normalize

    final_expected = {
        root / "Sin_clasificar/_MIME/text/plain" / path.name: content
        for path, content in expected.items()
    }
    final_atomic = root / "Sin_clasificar/_MIME/application/vnd.openxmlformats-officedocument.wordprocessingml.document/atomic.docx"

    def observe_identify(self, **kwargs):
        order.append("identify")
        assert not source.exists()
        assert (atomic if not seen else final_atomic).read_bytes() == atomic_original
        for path, content in (expected if not seen else final_expected).items():
            assert path.read_bytes() == content
        return identify(self, **kwargs)

    monkeypatch.setattr(FrameworkActions, "identify_and_normalize", observe_identify)
    seen: list[set[Path]] = []

    def consume(context: RouteExecutionContext) -> dict[str, int]:
        order.append("route")
        candidates = context.framework_state.iter_selected_route_candidates(
            context.run_id, "text/plain", "text", CandidateSelection(),
        )
        paths = {Path(candidate.path) for candidate in candidates}
        assert paths == set(expected if not seen else final_expected)
        seen.append(paths)
        return {"processed": len(paths)}

    config = FrameworkConfig(
        root=root, state_directory=tmp_path / "state", route="all", apply_actions=True,
        document_catalog_enabled=False, global_cpu_slots=2,
        global_min_free_memory_bytes=0, global_min_free_commit_bytes=0,
    )
    recording = RecordingProgress()
    for _ in range(2):
        result = FrameworkOrchestrator(
            config, progress=recording, route_registry={"text": RouteAdapter("text", consume)},
        ).run()
        summary = result.route_results["text"]
        assert isinstance(summary, dict) and summary["processed"] == 2
        assert not result.route_failures, result.route_failures
    assert order == ["zip-published", "identify", "route", "identify", "route"]
    assert effects.count(source) == 1 and len(seen) == 2
    assert (trash / "0/files" / source.name).read_bytes() == original
    assert final_atomic.read_bytes() == atomic_original
    terminal = [event for event in recording.events
                if event.key == ("zip-intake", "process") and event.finished]
    assert [(event.completed, event.total) for event in terminal] == [(2, 2), (1, 1)]
    assert all(next(metric.value for metric in event.metrics if metric.name == "status")
               != "running" for event in terminal)


def test_intake_failure_cannot_start_identify_or_content_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "source.txt").write_text("fixture\n")

    def fail_intake(**kwargs):
        raise RuntimeError("fixture intake failure")

    def forbidden(*args, **kwargs):
        pytest.fail("downstream content stage started after failed intake")

    monkeypatch.setattr(zip_intake_orchestrator, "run_zip_intake_stage", fail_intake)
    monkeypatch.setattr(FrameworkActions, "identify_and_normalize", forbidden)
    config = FrameworkConfig(
        root=root, state_directory=tmp_path / "state", route="all",
        document_catalog_enabled=False,
    )
    with pytest.raises(RuntimeError, match="fixture intake failure"):
        FrameworkOrchestrator(
            config, route_registry={"text": RouteAdapter("text", forbidden)},
        ).run()
    assert (root / "source.txt").read_text() == "fixture\n"
