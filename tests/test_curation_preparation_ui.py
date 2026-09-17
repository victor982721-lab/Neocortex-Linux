"""Read-only GUI projection for curation grants and recovery evidence."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from neocortex.curation.read import (
    CurationAttemptView,
    CurationGrantView,
    CurationRecoveryView,
    CurationReadSnapshot,
)
from neocortex.interface.presentation.theme import STYLESHEET
from neocortex.interface.presentation.windows.main import MainWindow
from neocortex.interface.read.curation import CurationReadRepository


TEST_CAPABILITIES = ("ui",)
pytestmark = pytest.mark.capability("ui")


@pytest.fixture(scope="module")
def application() -> QApplication:
    instance = QApplication.instance()
    if instance is not None and not isinstance(instance, QApplication):
        raise RuntimeError("A non-GUI Qt application already exists")
    app = instance or QApplication([])
    app.setStyleSheet(STYLESHEET)
    return app


def test_curation_panel_only_presents_grants_attempts_receipts_and_recovery(
    application: QApplication,
    tmp_path: Path,
) -> None:
    grant = CurationGrantView(
        grant_id="curation-authorization-grant-v1:fixture",
        plan_digest="sha256:" + "a" * 64,
        root=str(tmp_path / "fixture"),
        actor="victor",
        action="move",
        backend="linux",
        item_count=1,
        effect_count=1,
        max_actions=1,
        max_bytes=20,
        issued_ns=10,
        expires_ns=20,
        receipt_digest="sha256:" + "b" * 64,
        receipt_state="canonical",
        receipt_effects_digest="sha256:" + "c" * 64,
        review_heads_digest="sha256:" + "d" * 64,
    )
    attempt = CurationAttemptView(
        action_id=7,
        run_id=3,
        action_type="move_curation",
        status="recovery_required",
        source_path=str(tmp_path / "fixture" / "source.txt"),
        target_path=str(tmp_path / "fixture" / "target.txt"),
        grant_id=grant.grant_id,
        effect_id="item-1:effect:1",
        started_ns=11,
        completed_ns=None,
        detail="cache synchronization pending",
        expected_identity_present=True,
        receipt_digest="sha256:" + "e" * 64,
        receipt_state="object",
        receipt_type="successful_return_and_observation",
        receipt_operation="move",
    )
    recovery = CurationRecoveryView(
        action_id=7,
        status="recovery_required",
        reconciliation_event_id=9,
        classification="ambiguous",
        recommendation="preserve_evidence_and_review_manually",
        detail="fixture recovery evidence requires review",
        observed_ns=12,
        recorded_ns=13,
    )
    snapshot = CurationReadSnapshot(
        status="complete",
        grants=(grant,),
        attempts=(attempt,),
        recovery=(recovery,),
    )

    window = MainWindow(
        initial_root=tmp_path / "fixture",
        state_directory=tmp_path / "state",
        settings_path=tmp_path / "config" / "ui.ini",
    )
    window._curation_read_repository = CurationReadRepository(
        tmp_path / "state",
        reader=lambda *_args, **_kwargs: snapshot,
    )
    window.show()
    application.processEvents()
    try:
        window._select_page(4)
        application.processEvents()
        assert window.pages.count() == 5
        assert window.curation_result.isReadOnly()
        body = window.curation_result.toPlainText()
        assert "actor declarado: victor" in body
        assert "principal: no autenticado" in body
        assert "successful_return_and_observation" not in body
        assert "Receipt: sha256:" in body
        assert "Recovery pendiente: 1" in body
        assert "preserve_evidence_and_review_manually" in body
        assert window.curation_copy_button.isEnabled()
        assert not any(
            word in button.text().casefold()
            for button in (window.curation_refresh_button, window.curation_copy_button)
            for word in ("aplicar", "apply", "restaurar", "restore", "mover", "move")
        )
    finally:
        window.close()
        application.processEvents()
