"""Focused functional-default and lifecycle-parity contracts for the Qt UI."""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Iterator, Mapping
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QPoint, QSettings
    from PySide6.QtWidgets import QApplication
    from neocortex.interface.presentation.theme import STYLESHEET
    from neocortex.interface.presentation.windows.main import MainWindow
except ImportError:  # base profile: keep non-UI cases importable without Qt
    QPoint = QSettings = QApplication = MainWindow = None  # type: ignore[assignment]
    STYLESHEET = ""

from neocortex.interface.application.request import (
    FULL_DEADLINE_SECONDS,
    FULL_MAX_ITEMS,
    ROUTE_ORDER,
    RunRequest,
)
from neocortex.interface.read.curation import CurationReadRepository, present_curation_snapshot
from neocortex.interface.read.issues import route_issue_count, route_summary_mapping


TEST_CAPABILITIES = ("base", "ui")
pytestmark = pytest.mark.capability("base", "ui")


@pytest.fixture(scope="module")
def isolated_home() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="neocortex-gui-home-") as directory:
        home = Path(directory)
        names = (
            "HOME",
            "XDG_CONFIG_HOME",
            "XDG_CACHE_HOME",
            "XDG_DATA_HOME",
            "XDG_STATE_HOME",
            "XDG_RUNTIME_DIR",
        )
        original = {name: os.environ.get(name) for name in names}
        try:
            for name in names[1:]:
                path = home / name.casefold()
                path.mkdir()
                os.environ[name] = str(path)
            os.environ["HOME"] = str(home)
            yield home
        finally:
            for name, value in original.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


@pytest.fixture(scope="module")
def application(isolated_home: Path) -> QApplication:
    if QApplication is None:
        pytest.skip("PySide6 is unavailable in the base profile")
    del isolated_home
    instance = QApplication.instance()
    if instance is not None and not isinstance(instance, QApplication):
        raise RuntimeError("A non-GUI Qt application already exists")
    app = instance or QApplication([])
    app.setStyleSheet(STYLESHEET)
    return app


def _close_window(application: QApplication, window: MainWindow) -> None:
    window.close()
    application.processEvents()


def test_full_default_request_uses_canonical_all_lifecycle() -> None:
    with tempfile.TemporaryDirectory() as directory:
        request = RunRequest(Path(directory), ROUTE_ORDER, profile="full").validated()
        arguments = request.cli_arguments()

    assert request.uses_all_lifecycle
    assert arguments == [
        "--root",
        str(Path(directory).resolve()),
        "--all",
        "--run-max-items",
        str(FULL_MAX_ITEMS),
        "--run-time-budget-seconds",
        str(float(FULL_DEADLINE_SECONDS)),
    ]
    assert "--route" not in arguments


def test_new_default_shows_video_without_promoting_saved_custom_selection(
    application: QApplication,
    isolated_home: Path,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / "Corpus"
        root.mkdir()
        window = MainWindow(
            initial_root=root,
            state_directory=Path(directory) / "state",
            settings_path=Path(directory) / "config" / "ui.ini",
        )
        window.show()
        application.processEvents()
        try:
            assert tuple(window.route_toggles) == ROUTE_ORDER
            assert window.route_toggles["video"].text() == "Video"
            assert all(toggle.isChecked() for toggle in window.route_toggles.values())
            assert not window._current_request().uses_all_lifecycle

            settings_path = Path(directory) / "config" / "saved.ini"
            settings_path.parent.mkdir(exist_ok=True)
            settings = QSettings(str(settings_path), QSettings.Format.IniFormat)
            settings.setValue("execution/root", str(root))
            settings.setValue("execution/routes", "pdf,code")
            settings.setValue("execution/profile", "full")
            settings.setValue("execution/max_items", 17)
            settings.setValue("execution/deadline_seconds", 42)
            settings.sync()

            saved = MainWindow(
                initial_root=root,
                state_directory=Path(directory) / "state-saved",
                settings_path=settings_path,
            )
            saved.show()
            application.processEvents()
            try:
                assert tuple(
                    route
                    for route, toggle in saved.route_toggles.items()
                    if toggle.isChecked()
                ) == ("pdf", "code")
                assert not saved.route_toggles["video"].isChecked()
                request = saved._current_request()
                assert request.profile == "full"
                assert request.max_items == 17
                assert request.deadline_seconds == 42.0
                assert not request.uses_all_lifecycle
                arguments = request.cli_arguments()
                assert arguments[arguments.index("--route") + 1] == "pdf,code"
                assert "--all" not in arguments

                saved.profile_combo.setCurrentIndex(0)
                saved.profile_combo.setCurrentIndex(1)
                application.processEvents()
                assert saved.max_items_spin.value() == 17
                assert saved.deadline_spin.value() == 42
                assert not saved.route_toggles["video"].isChecked()
            finally:
                _close_window(application, saved)
        finally:
            _close_window(application, window)


def test_semantic_partial_progress_stays_visible_as_recoverable_coverage(
    application: QApplication,
    isolated_home: Path,
) -> None:
    del isolated_home
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / "Corpus"
        root.mkdir()
        window = MainWindow(
            initial_root=root,
            state_directory=Path(directory) / "state",
            settings_path=Path(directory) / "config" / "ui.ini",
        )
        window.show()
        application.processEvents()
        try:
            window._clear_progress()
            window._on_worker_message(
                {
                    "type": "progress",
                    "operation": "semantic",
                    "phase": "integrated",
                    "description": "Semantic pausado con progreso",
                    "completed": 3,
                    "total": 8,
                    "unit": "elementos",
                    "finished": True,
                    "metrics": {"status": "partial", "remaining": 5},
                }
            )
            assert window.live_status.property("state") == "warning"
            assert window.header_status.property("state") == "warning"
            assert "cobertura parcial" in window.activity_detail.text()

            window._on_worker_message(
                {
                    "type": "completed",
                    "run_id": 12,
                    "files_checked": 8,
                    "action_errors": 0,
                    "route_errors": {},
                    "issues": 0,
                    "completion_status": "completed",
                    "exit_code": 0,
                    "semantic_status": "partial",
                    "semantic_exit_code": 0,
                    "semantic_recovery_required": False,
                    "semantic_selected_sources": ["pdf"],
                }
            )
            assert window.live_status.property("state") == "warning"
            assert "puede reanudarse" in window.activity_detail.text()
        finally:
            _close_window(application, window)


def test_worker_all_attaches_the_same_integrated_semantic_stage_as_cli(
    monkeypatch: pytest.MonkeyPatch,
    isolated_home: Path,
) -> None:
    del isolated_home
    from neocortex.api.cli import cli_semantic
    from neocortex.runtime.orchestration import orchestrator as orchestrator_module
    from neocortex.interface.protocol import worker

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / "Corpus"
        root.mkdir()
        observed: dict[str, object] = {}

        def fake_semantic(args, **kwargs):
            observed["all"] = args.all
            observed["semantic_max_items"] = args.semantic_max_items
            from neocortex.progress import ProgressEvent, ProgressMetric

            kwargs["progress"](
                ProgressEvent(
                    "semantic",
                    "integrated",
                    "Semantic no disponible",
                    1,
                    1,
                    "fase",
                    True,
                    (
                        ProgressMetric("scope", "audio"),
                        ProgressMetric("cause", "\x1b[31m" + "x" * 900),
                        ProgressMetric("next_action", "revisar dependencia local"),
                    ),
                )
            )
            observed.update(kwargs)
            return 0

        class FakeOrchestrator:
            def __init__(self, config, **kwargs):
                self.config = config
                self.kwargs = kwargs

            def request_cancellation(self) -> None:
                return None

        monkeypatch.setattr(cli_semantic, "run_integrated_all_semantic_index", fake_semantic)
        monkeypatch.setattr(orchestrator_module, "FrameworkOrchestrator", FakeOrchestrator)
        monkeypatch.setattr(worker, "_emit", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(worker, "_listen_for_commands", lambda _orchestrator: None)
        monkeypatch.setattr(worker._WorkerHeartbeat, "start", lambda _self: None)
        monkeypatch.setattr(worker._WorkerHeartbeat, "stop", lambda _self: None)
        monkeypatch.setattr(worker._ExecutionBudget, "start", lambda _self: None)

        orchestrator, _organization_check, heartbeat, budget = worker._prepare_framework(
            ["--root", str(root), "--all"]
        )
        try:
            callback = orchestrator.kwargs["lifecycle_stage_runner"]
            assert callable(callback)
            assert callback(21) == 0
            assert observed["all"] is True
            assert observed["run_id"] == 21
            assert observed["framework_lock_held"] is True
            assert observed["print_output"] is False
            assert orchestrator.config.route == "all"
            assert orchestrator.kwargs["lifecycle_stage_details"]["semantic_budget"][
                "max_items"
            ] == observed["semantic_max_items"]
            assert worker._ACTIVE_SEMANTIC_STATE is not None
            assert worker._ACTIVE_SEMANTIC_STATE.status == "skipped"
            assert len(worker._ACTIVE_SEMANTIC_STATE.unavailable["audio"]) == 800
            assert "\x1b" not in worker._ACTIVE_SEMANTIC_STATE.unavailable["audio"]
        finally:
            heartbeat.stop()
            budget.stop()
            worker._ACTIVE_BUDGET = None


def test_gui_renders_configured_persisted_plan_with_public_parity_and_no_writes(
    application: QApplication,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del isolated_home
    from neocortex.api import curation_api, public as public_api
    from neocortex.curation.preview import CurationItem, CurationPlanPage
    from neocortex.curation.read import CurationReadSnapshot
    import neocortex.sdk as sdk

    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory)
        state = base / "configured-state"
        public_state = base / "public-state"
        corpus = base / "configured-corpus"
        state.mkdir()
        public_state.mkdir()
        corpus.mkdir()
        (state / "sentinel.bin").write_bytes(b"state remains unchanged")
        (corpus / "evidence.txt").write_bytes(b"corpus remains unchanged")
        monkeypatch.setattr(curation_api, "default_state_directory", lambda: public_state)

        plan = CurationPlanPage(
            schema_version=1,
            coverage="complete",
            missing_owners=(),
            root=str(corpus),
            scan_id=7,
            inventory_files=1,
            duplicate_groups=0,
            duplicate_members=0,
            reclaimable_bytes=0,
            organization_plans=1,
            empty_files=0,
            limit=10,
            cursor=None,
            next_cursor=None,
            snapshot_id="sha256:" + "b" * 64,
            plan_digest="sha256:" + "a" * 64,
            items_total=1,
            items=(
                CurationItem(
                    item_id="organization:17",
                    kind="organization_plan",
                    status="review",
                    action="review_organization_proposal",
                    source_path=str(corpus / "evidence.txt"),
                    destination_path=str(corpus / "technical" / "evidence.txt"),
                    reason="newer_run",
                    evidence={
                        "catalog_run_id": 3,
                        "locator": {"source_kind": "text", "plan_id": 17},
                        "next_step": "confirmar clasificación con revisión humana",
                        "blockers": ["authorization_required"],
                    },
                ),
            ),
        )
        observed_state_directories: list[Path] = []

        def fake_builder(state_directory: Path, **_kwargs: object) -> CurationPlanPage:
            observed_state_directories.append(Path(state_directory))
            return plan

        monkeypatch.setattr(
            curation_api,
            "_plan_contract",
            lambda: (RuntimeError, fake_builder),
        )
        # The public and SDK facades intentionally keep their fixed canonical
        # root.  The desktop adapter is the only reader allowed to pass the
        # window's configured state directory explicitly.
        gui_plan_reader = curation_api._curation_plan_payload_for_state
        cli_payload = gui_plan_reader(state_directory=state, limit=10, request_id="cli")
        public_payload = public_api.curation_plan_payload(
            limit=10,
            request_id="public",
        )
        sdk_payload = sdk.curation_plan_payload(
            limit=10,
            request_id="sdk",
        )
        repository = CurationReadRepository(
            state,
            reader=lambda *_args, **_kwargs: CurationReadSnapshot(status="complete"),
            plan_reader=gui_plan_reader,
        )
        gui_payload = repository.read_plan(limit=10)

        def identity(payload: Mapping[str, object]) -> tuple[object, tuple[object, ...]]:
            page = payload["page"]
            assert isinstance(page, dict)
            items = page["items"]
            assert isinstance(items, list)
            return page["plan_digest"], tuple(item["item_id"] for item in items)

        expected_identity = identity(cli_payload)
        assert identity(public_payload) == expected_identity
        assert identity(sdk_payload) == expected_identity
        assert identity(gui_payload) == expected_identity
        assert observed_state_directories == [state, public_state, public_state, state]

        before_state = {
            path.relative_to(state): hashlib.sha256(path.read_bytes()).digest()
            for path in state.rglob("*")
            if path.is_file()
        }
        before_corpus = {
            path.relative_to(corpus): hashlib.sha256(path.read_bytes()).digest()
            for path in corpus.rglob("*")
            if path.is_file()
        }
        window = MainWindow(
            initial_root=corpus,
            state_directory=state,
            settings_path=base / "config" / "ui.ini",
        )
        window._curation_read_repository = repository
        window.show()
        application.processEvents()
        try:
            window._select_page(4)
            application.processEvents()
            body = window.curation_result.toPlainText()
            assert "Propuestas persistidas del CurationPlanPage" in body
            assert "Cobertura: complete" in body
            assert plan.plan_digest in body
            assert "organization:17" in body
            assert "Razón: newer_run" in body
            assert '"plan_id":17' in body
            assert "Siguiente paso: confirmar clasificación con revisión humana" in body
            assert window.curation_result.isReadOnly()
            assert window.curation_status.property("state") == "completed"
        finally:
            _close_window(application, window)

        after_state = {
            path.relative_to(state): hashlib.sha256(path.read_bytes()).digest()
            for path in state.rglob("*")
            if path.is_file()
        }
        after_corpus = {
            path.relative_to(corpus): hashlib.sha256(path.read_bytes()).digest()
            for path in corpus.rglob("*")
            if path.is_file()
        }
        assert after_state == before_state
        assert after_corpus == before_corpus


def test_gui_plan_projection_preserves_partial_coverage_and_sanitizes_evidence() -> None:
    from neocortex.curation.read import CurationReadSnapshot

    presentation = present_curation_snapshot(
        CurationReadSnapshot(status="complete"),
        plan={
            "coverage": "partial",
            "page": {
                "plan_digest": "sha256:" + "d" * 64,
                "items_total": 1,
                "next_cursor": "next-page",
                "items": [
                    {
                        "item_id": "organization:9",
                        "kind": "organization_plan",
                        "status": "review",
                        "action": "review_blocked_organization_proposal",
                        "source_path": "/tmp/\x1b[31munsafe.txt",
                        "destination_path": None,
                        "reason": "owner_health_unknown",
                        "evidence": {"blockers": ["authorization_required"]},
                    }
                ],
            },
            "error": {"code": "partial", "message": "owner coverage is partial"},
        },
    )

    assert presentation.state == "warning"
    assert "Cobertura: partial" in presentation.body
    assert "Página acotada" in presentation.body
    assert "Siguiente paso: revisión humana de bloqueadores" in presentation.body
    assert "\x1b" not in presentation.body


@pytest.mark.parametrize(
    "field",
    (
        "partial_documents",
        "partial",
        "containers_partial",
        "errors",
        "cached_errors",
        "profile_errors",
        "page_errors",
        "document_timeouts",
        "catalog_errors",
        "catalog_source_stale",
        "safety_issues",
        "protected",
        "retryable_errors",
        "manual_review_errors",
        "source_missing",
    ),
)
def test_gui_route_issue_projection_counts_each_typed_issue_family(field: str) -> None:
    summary = SimpleNamespace(**{field: 1})

    assert route_issue_count(summary) == 1
    assert route_summary_mapping(summary)[field] == 1


def test_gui_route_issue_projection_does_not_double_count_partial_coverage() -> None:
    summary = SimpleNamespace(
        partial_documents=2,
        partial=5,
        containers_partial=3,
    )

    assert route_issue_count(summary) == 5


def test_gui_route_issue_projection_keeps_audio_observations_non_errors() -> None:
    summary = SimpleNamespace(no_speech=17, no_audio=4, processed=21)

    assert route_issue_count(summary) == 0


def test_gui_route_issue_projection_counts_a_typed_failed_route_once() -> None:
    summary = SimpleNamespace(errors=4, partial=3)

    assert route_issue_count(summary, failed=True) == 1
    assert route_issue_count(None, failed=True) == 1


def test_worker_summary_projects_bounded_route_failure_causes() -> None:
    from neocortex.interface.protocol.worker import _summary_payload

    failures = {
        f"route-{index:02d}": "\x1b[31m" + "x" * 900
        for index in range(10)
    }
    payload = _summary_payload(
        SimpleNamespace(
            run_id=17,
            scan=SimpleNamespace(excluded_directories=7, skipped_links=4),
            actions=SimpleNamespace(files_checked=1, errors=0),
            route_results={"video": SimpleNamespace(partial=2)},
            route_failures=failures,
        )
    )

    causes = payload["route_unavailable"]
    assert isinstance(causes, dict)
    assert len(causes) == 9
    assert all(len(cause) <= 800 and "\x1b" not in cause for cause in causes.values())
    assert payload["route_errors"]["video"] == 2
    assert all(payload["route_errors"][name] == 1 for name in list(causes)[:9])
    assert payload["excluded_directories"] == 7
    assert payload["skipped_links"] == 4
    assert "route_unavailable" not in _summary_payload(
        SimpleNamespace(
            run_id=18,
            actions=SimpleNamespace(files_checked=0, errors=0),
            route_results={},
        )
    )


def test_worker_message_validator_accepts_and_bounds_unavailable_causes() -> None:
    from neocortex.interface.protocol.messages import (
        WorkerProtocolError,
        decode_message,
        encode_message,
    )

    record = decode_message(
        encode_message(
            "completed",
            run_id=1,
            files_checked=1,
            action_errors=0,
            route_errors={"video": 1},
            organization_errors=False,
            issues=1,
            completion_status="completed_with_issues",
            exit_code=2,
            skipped_links=4,
            excluded_directories=7,
            route_unavailable={"video": "local dependency is unavailable"},
            semantic_unavailable={"audio": "model_unavailable"},
        )
    )
    assert record is not None
    assert record["skipped_links"] == 4
    assert record["excluded_directories"] == 7
    assert record["route_unavailable"] == {"video": "local dependency is unavailable"}
    assert record["semantic_unavailable"] == {"audio": "model_unavailable"}

    with pytest.raises(WorkerProtocolError, match="bounded object"):
        encode_message(
            "completed",
            run_id=1,
            files_checked=1,
            action_errors=0,
            route_errors={},
            organization_errors=False,
            issues=0,
            completion_status="completed",
            exit_code=0,
            route_unavailable={f"route-{index}": "cause" for index in range(10)},
        )
    with pytest.raises(WorkerProtocolError, match="bounded strings"):
        encode_message(
            "completed",
            run_id=1,
            files_checked=1,
            action_errors=0,
            route_errors={},
            organization_errors=False,
            issues=0,
            completion_status="completed",
            exit_code=0,
            route_unavailable={"video": "x" * 801},
        )
    with pytest.raises(WorkerProtocolError, match="outside its safe bound"):
        encode_message(
            "completed",
            run_id=1,
            files_checked=1,
            action_errors=0,
            route_errors={},
            organization_errors=False,
            issues=0,
            completion_status="completed",
            exit_code=0,
            skipped_links=-1,
        )


def test_gui_final_activity_detail_renders_route_cause_and_safe_next_action(
    application: QApplication,
    isolated_home: Path,
) -> None:
    del isolated_home
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / "Corpus"
        root.mkdir()
        window = MainWindow(
            initial_root=root,
            state_directory=Path(directory) / "state",
            settings_path=Path(directory) / "config" / "ui.ini",
        )
        window.show()
        application.processEvents()
        try:
            window._clear_progress()
            window._on_worker_message(
                {
                    "type": "completed",
                    "run_id": 31,
                    "files_checked": 1,
                    "action_errors": 0,
                    "route_errors": {"video": 1},
                    "issues": 1,
                    "completion_status": "completed_with_issues",
                    "exit_code": 2,
                    "excluded_directories": 7,
                    "skipped_links": 4,
                    "route_unavailable": {
                        "video": "\x1b[31mFFmpeg missing\nlocal only"
                    },
                }
            )
            detail = window.activity_detail.text()
            assert window.live_status.property("state") == "warning"
            assert window.activity_title.text() == "Cobertura incompleta"
            assert "Rutas no disponibles" in detail
            assert "FFmpeg missing local only" in detail
            assert "Siguiente acción: revisar dependencia local" in detail
            assert "descargará" in detail
            assert "directorios excluidos: 7" in detail
            assert "enlaces omitidos: 4" in detail
            assert "\x1b" not in detail
        finally:
            _close_window(application, window)


def test_gui_activity_detail_wrap_geometry_at_normal_and_minimum_window(
    application: QApplication,
    isolated_home: Path,
) -> None:
    """C5 terminal text must keep its height-for-width allocation after scroll."""

    del isolated_home
    application.setStyleSheet(STYLESHEET)
    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory)
        window = MainWindow(
            initial_root=base / "Corpus",
            state_directory=base / "state",
            settings_path=base / "config" / "ui.ini",
        )
        window.show()
        window._select_page(1)
        application.processEvents()
        message = {
            "type": "completed",
            "run_id": 42,
            "files_checked": 7,
            "action_errors": 0,
            "route_errors": {"video": 1, "audio": 1},
            "issues": 2,
            "completion_status": "completed_with_issues",
            "exit_code": 2,
            "route_unavailable": {
                "video": "FFmpeg ausente: no se encontró revise PATH",
                "audio": "modelo local ausente",
            },
            "excluded_directories": 3,
            "skipped_links": 5,
        }
        try:
            sizes = ((1440, 900), (window.minimumWidth(), window.minimumHeight()))
            for requested_size in sizes:
                window.resize(*requested_size)
                application.processEvents()
                window._clear_progress()
                window._on_worker_message(message)
                application.processEvents()

                detail = window.activity_detail
                activity = detail.parentWidget()
                assert activity is not None
                required_height = detail.heightForWidth(detail.width())
                assert detail.hasHeightForWidth()
                assert required_height > detail.fontMetrics().lineSpacing()
                assert detail.height() >= required_height
                assert activity.height() >= activity.minimumSizeHint().height()
                assert activity.rect().contains(detail.geometry())
                assert activity.rect().contains(window.activity_progress.geometry())
                assert detail.geometry().bottom() < window.activity_progress.geometry().top()
                assert "Siguiente acción: revisar dependencia local" in detail.text()
                assert "directorios excluidos: 3" in detail.text()
                assert "enlaces omitidos: 5" in detail.text()

                page = window.pages.currentWidget()
                page.verticalScrollBar().setValue(page.verticalScrollBar().maximum())
                application.processEvents()
                visible_detail = detail.rect().translated(detail.mapTo(window, QPoint(0, 0)))
                assert visible_detail.intersects(window.rect())
        finally:
            _close_window(application, window)
