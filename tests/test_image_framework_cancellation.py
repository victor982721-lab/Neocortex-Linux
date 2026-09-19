"""Image's local abort must preserve Framework's route-failure contract."""

from __future__ import annotations

import threading
import time
from dataclasses import replace

import pytest
from PIL import Image

from neocortex.deduplication import snapshot_path
from neocortex.persistence.framework_state_writer import FrameworkState
from neocortex.runtime.control.global_resources import ResourceWaitTimeout
from neocortex.runtime.models import FrameworkConfig
from neocortex.runtime.orchestration.orchestrator import (
    FrameworkOrchestrator,
    RouteExecutionError,
)
from neocortex.runtime.orchestration.route_registry import RouteAdapter, builtin_route_registry


TEST_CAPABILITIES = ("image",)
MIB = 1024 * 1024


def _image_run(tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    paths = [corpus / "a.png", corpus / "b.png"]
    for path in paths:
        with Image.new("RGB", (32, 32), "navy") as image:
            image.save(path)
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    config = FrameworkConfig(
        root=corpus,
        state_directory=state_directory,
        route="all",
        image_workers=1,
        image_document_ocr_mode="never",
        global_memory_budget_bytes=512 * MIB,
        global_min_free_memory_bytes=1 << 60,
        global_min_free_commit_bytes=128 * MIB,
        global_resource_wait_timeout_seconds=1.0,
    )
    return config, paths


def _populate(state, config, paths):
    run_id = state.begin_initial_run(config.root, None)
    state.store_route_candidates(run_id, [("image/png", snapshot_path(path)) for path in paths])
    state.publish_initial_routing_snapshot(run_id, 1, 0, 1, "full", len(paths))
    return run_id


def test_fatal_image_headroom_retains_error_and_finishes_independent_sibling(tmp_path):
    config, paths = _image_run(tmp_path)
    failed = threading.Event()
    image_failures = []
    sibling_finished = []
    builtin = builtin_route_registry()["image"]

    def execute_image(context):
        try:
            return builtin.execute(context)
        except ResourceWaitTimeout as error:
            image_failures.append(error)
            failed.set()
            raise

    def execute_sibling(context):
        assert failed.wait(8), "Image did not report its bounded admission error"
        context.cancellation.checkpoint()
        sibling_finished.append(True)
        return {"processed": 0}

    orchestrator = FrameworkOrchestrator(
        config,
        route_registry={
            "image": replace(builtin, execute=execute_image),
            "text": RouteAdapter("text", execute_sibling),
        },
    )
    with FrameworkState(config.framework_database) as state:
        run_id = _populate(state, config, paths)
        started = time.monotonic()
        with pytest.raises(RouteExecutionError) as raised:
            orchestrator._run_content_routes(
                root=config.root, state=state, run_id=run_id, scan_id=1
            )
        elapsed = time.monotonic() - started
        assert len(image_failures) == 1
        assert raised.value.failures == {"image": image_failures[0]}
        assert sibling_finished == [True]
        assert not orchestrator._cancellation.is_cancelled
        assert orchestrator._active_coordinator is None
        assert elapsed < 3
        statuses = state._connection.execute(
            "SELECT route_name, status FROM route_runs WHERE run_id=? ORDER BY route_name",
            (run_id,),
        ).fetchall()
        assert statuses == [("image", "failed"), ("text", "completed")]


def test_framework_cancellation_reaches_images_local_token(tmp_path):
    config, paths = _image_run(tmp_path)
    config = replace(config, route="image", global_resource_wait_timeout_seconds=5.0)
    entered = threading.Event()
    failures = []
    framework_tokens = []
    builtin = builtin_route_registry()["image"]

    def execute_image(context):
        original_progress = context.progress

        def progress(event):
            if original_progress is not None:
                original_progress(event)
            if event.operation == "image" and event.phase == "classify":
                metrics = {metric.name: metric.value for metric in event.metrics}
                if metrics.get("pending_admissions", 0):
                    assert metrics.get("in_flight", 0) == 0
                    entered.set()

        # The registry owns the child token; this observes its parent without
        # replacing route execution or the real admission wait.
        framework_tokens.append(context.cancellation)
        return builtin.execute(replace(context, progress=progress))

    orchestrator = FrameworkOrchestrator(
        config, route_registry={"image": replace(builtin, execute=execute_image)}
    )

    def run():
        try:
            with FrameworkState(config.framework_database) as state:
                run_id = _populate(state, config, paths)
                orchestrator._run_content_routes(
                    root=config.root, state=state, run_id=run_id, scan_id=1
                )
        except BaseException as error:
            failures.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert entered.wait(5), "Image did not queue work for resource admission"
        orchestrator.request_cancellation()
        thread.join(3)
        assert not thread.is_alive()
    finally:
        orchestrator.request_cancellation()
        thread.join(8)

    assert len(failures) == 1 and isinstance(failures[0], KeyboardInterrupt)
    assert framework_tokens and framework_tokens[0].is_cancelled
    assert orchestrator._active_coordinator is None
