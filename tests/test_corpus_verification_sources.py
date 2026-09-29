from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from neocortex.documents.document_resource_binding import build_resource_binding
from neocortex.foundation.file_identity import FileIdentity
from neocortex.runtime.orchestration.corpus_verification_sources import verify_current_corpus
from neocortex.semantic.fast_curation_policy import CalibrationParameters, FastCurationPolicy
from neocortex.semantic.fast_curation_policy_bundle import FastCurationPolicyBundle


def _make_fixture(tmp_path: Path, *, successor: bool = False) -> tuple[SimpleNamespace, object, int, int]:
    root = tmp_path / "corpus"
    ordered = root / "Corpus_ordenado" / "Pruebas"
    residual = root / "Sin_clasificar" / "_MIME" / "application" / "octet-stream"
    ordered.mkdir(parents=True)
    residual.mkdir(parents=True)
    classified = ordered / "informe.pdf"
    unknown = residual / "sin-tipo"
    classified.write_bytes(b"classified")
    unknown.write_bytes(b"unknown")
    classified_stat = classified.stat()
    unknown_stat = unknown.stat()

    state = tmp_path / "state"
    state.mkdir()
    source = root / "source.pdf"
    receipt = {
        "organization_receipt_schema": "neocortex.organization-move-receipt/v1",
        "operation": "move",
        "source_absent": True,
        "source_path": str(source),
        "source_digest": "metadata:v1:source",
        "target_path": str(classified),
        "target_identity": {
            "path": str(classified),
            "size": classified.stat().st_size,
            "mtime_ns": classified.stat().st_mtime_ns,
            "birthtime_ns": getattr(classified.stat(), "st_birthtime_ns", -1),
            "volume_id": f"{classified.stat().st_dev:x}",
            "file_id": f"{classified.stat().st_ino:x}",
        },
    }
    sync = json.dumps({"physical_receipt": receipt})
    binding = build_resource_binding(
        source_kind="pdf",
        file_key="doc-1",
        path=str(classified),
        identity=FileIdentity(classified_stat.st_dev, classified_stat.st_ino),
        birthtime_ns=getattr(classified_stat, "st_birthtime_ns", -1),
        size=classified_stat.st_size,
        mtime_ns=classified_stat.st_mtime_ns,
    )
    binding_json = json.dumps(binding, sort_keys=True, separators=(",", ":"))
    catalog_path = state / "document_catalog.sqlite3"
    catalog = sqlite3.connect(catalog_path)
    catalog.executescript(
        """
        CREATE TABLE documents(
          source_kind TEXT,file_key TEXT,processing_signature TEXT,text_fingerprint TEXT,path TEXT,
          catalog_status TEXT,classification_json TEXT,confidence REAL,
          primary_kind TEXT,active INTEGER,resource_binding_json TEXT,
          volume_id INTEGER,file_id INTEGER,
          size INTEGER,mtime_ns INTEGER,birthtime_ns INTEGER
        );
        CREATE TABLE organization_plans(
          plan_id INTEGER,destination_path TEXT,organization_root TEXT,
          source_path TEXT,status TEXT,cache_sync_status TEXT,cache_sync_json TEXT,
          source_kind TEXT,file_key TEXT
        );
        CREATE TABLE curator_decisions(
          source_kind TEXT,file_key TEXT,source_binding_json TEXT,input_signature TEXT,
          semantic_representation_fingerprint TEXT,representation_version TEXT,
          model_signature TEXT,role TEXT,vector_space TEXT,dimensions INTEGER,
          ontology_version TEXT,prototype_version TEXT,policy_version TEXT,
          calibration_version TEXT,decision TEXT,top1_label TEXT,top1_score REAL,
          top2_label TEXT,top2_score REAL,margin REAL,top_k_json TEXT,
          evidence_json TEXT,context_provenance_json TEXT,created_ns INTEGER,
          updated_ns INTEGER
        );
        """
    )
    payload = json.dumps({"evidence": ["heading:SAT"], "taxonomy_status": "in_taxonomy"})
    catalog.execute(
        "INSERT INTO documents VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("pdf", "doc-1", "processing-1", "input-1", str(classified), "classified", payload, 0.9, "informe_tecnico", 1, binding_json, classified_stat.st_dev, classified_stat.st_ino, classified_stat.st_size, classified_stat.st_mtime_ns, getattr(classified_stat, "st_birthtime_ns", -1)),
    )
    catalog.execute(
        "INSERT INTO organization_plans VALUES(?,?,?,?,?,?,?,?,?)",
        (1, str(classified), str(root), str(source), "applied", "synced", sync, "pdf", "doc-1"),
    )
    catalog.execute(
        "INSERT INTO curator_decisions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "pdf", "doc-1", binding_json, "input-1",
            "representation-sha", "repr-v1", "model-v1", "document", "fast", 3,
            "ontology-v1", "proto-v1", "fast-curation-policy-v1", "calibration-v1", "CLASSIFIED",
            "informe_tecnico", 0.91, "manual", 0.50, 0.41,
            json.dumps([{"label": "informe_tecnico", "score": 0.91}]),
            json.dumps({"metadata": "catalog", "prototype_set_fingerprint": "0" * 64}),
            json.dumps({"semantic": "fast"}), 1, 1,
        ),
    )
    catalog.commit()
    catalog.close()

    pdf_path = state / "pdf.sqlite3"
    pdf = sqlite3.connect(pdf_path)
    pdf.execute("CREATE TABLE documents(path TEXT,volume_id INTEGER,file_id INTEGER,size INTEGER,mtime_ns INTEGER,birthtime_ns INTEGER)")
    pdf.execute("INSERT INTO documents VALUES(?,?,?,?,?,?)", (str(classified), classified_stat.st_dev, classified_stat.st_ino, classified_stat.st_size, classified_stat.st_mtime_ns, getattr(classified_stat, "st_birthtime_ns", -1)))
    pdf.commit()
    pdf.close()

    dedup_path = state / "dedup.sqlite3"
    dedup = sqlite3.connect(dedup_path)
    dedup.executescript(
        """
        CREATE TABLE files(scan_id INTEGER,path TEXT,volume_id INTEGER,file_id INTEGER,size INTEGER,mtime_ns INTEGER,birthtime_ns INTEGER);
        CREATE TABLE inventory_scan_successors(predecessor_scan_id INTEGER,successor_scan_id INTEGER);
        CREATE TABLE planned_duplicate_groups(group_id INTEGER,scan_id INTEGER);
        CREATE TABLE planned_duplicate_members(group_id INTEGER,path TEXT,role TEXT);
        """
    )
    dedup.executemany("INSERT INTO files VALUES(?,?,?,?,?,?,?)", ((7, str(classified), classified_stat.st_dev, classified_stat.st_ino, classified_stat.st_size, classified_stat.st_mtime_ns, getattr(classified_stat, "st_birthtime_ns", -1)), (7, str(unknown), unknown_stat.st_dev, unknown_stat.st_ino, unknown_stat.st_size, unknown_stat.st_mtime_ns, getattr(unknown_stat, "st_birthtime_ns", -1))))
    if successor:
        dedup.execute("INSERT INTO inventory_scan_successors VALUES(?,?)", (7, 8))
        dedup.executemany("INSERT INTO files VALUES(?,?,?,?,?,?,?)", ((8, str(classified), classified_stat.st_dev, classified_stat.st_ino, classified_stat.st_size, classified_stat.st_mtime_ns, getattr(classified_stat, "st_birthtime_ns", -1)), (8, str(unknown), unknown_stat.st_dev, unknown_stat.st_ino, unknown_stat.st_size, unknown_stat.st_mtime_ns, getattr(unknown_stat, "st_birthtime_ns", -1))))
    dedup.commit()
    dedup.close()

    semantic_path = state / "semantic.sqlite3"
    semantic = sqlite3.connect(semantic_path)
    semantic.executescript(
        """
        CREATE TABLE semantic_items(active INTEGER,path TEXT,provenance_json TEXT);
        CREATE TABLE published_embedding_heads(model_signature TEXT,generation_id INTEGER);
        CREATE TABLE embedding_generations(generation_id INTEGER,status TEXT);
        CREATE TABLE embedding_generation_members(generation_id INTEGER,item_revision_id INTEGER);
        CREATE TABLE semantic_item_revisions(item_revision_id INTEGER,path TEXT,provenance_json TEXT);
        """
    )
    semantic_identity = {"physical_identity": {"volume_id": unknown_stat.st_dev, "file_id": unknown_stat.st_ino, "size": unknown_stat.st_size, "mtime_ns": unknown_stat.st_mtime_ns, "birthtime_ns": getattr(unknown_stat, "st_birthtime_ns", -1)}}
    semantic.execute("INSERT INTO semantic_items VALUES(?,?,?)", (1, str(unknown), json.dumps(semantic_identity)))
    semantic.execute("INSERT INTO published_embedding_heads VALUES(?,?)", ("model-v1", 1))
    semantic.execute("INSERT INTO embedding_generations VALUES(?,?)", (1, "ready"))
    semantic.execute("INSERT INTO embedding_generation_members VALUES(?,?)", (1, 1))
    semantic.execute("INSERT INTO semantic_item_revisions VALUES(?,?,?)", (1, str(unknown), json.dumps(semantic_identity)))
    semantic.commit()
    semantic.close()

    framework_connection = sqlite3.connect(":memory:")
    framework_connection.executescript(
        """
        CREATE TABLE route_candidates(run_id INTEGER,path TEXT,volume_id INTEGER,file_id INTEGER,size INTEGER,mtime_ns INTEGER,birthtime_ns INTEGER);
        CREATE TABLE initial_runs(run_id INTEGER,status TEXT);
        CREATE TABLE file_actions(
          action_id INTEGER,run_id INTEGER,action_type TEXT,source_path TEXT,
          target_path TEXT,status TEXT,detail TEXT,effect_receipt_json TEXT,
          expected_identity_json TEXT
        );
        """
    )
    framework_connection.execute("INSERT INTO route_candidates VALUES(?,?,?,?,?,?,?)", (9, str(classified), classified_stat.st_dev, classified_stat.st_ino, classified_stat.st_size, classified_stat.st_mtime_ns, getattr(classified_stat, "st_birthtime_ns", -1)))
    framework_connection.execute("INSERT INTO initial_runs VALUES(?,?)", (9, "running"))
    framework_connection.commit()

    class FakeFramework:
        _connection = framework_connection
        _root = root

        def source_run_inventory(self, run_id):
            assert run_id == 9
            return self._root, 7

        def read_run_manifest(self, run_id):
            return {"run_id": run_id, "root": str(self._root), "input_snapshot": {"scan_id": 7}}

        @staticmethod
        def get_content_type_cache(snapshot, detector_version):
            del snapshot, detector_version
            return True, None

    config = SimpleNamespace(
        state_directory=state,
        document_catalog_database=catalog_path,
        dedup_database=dedup_path,
        pdf_database=pdf_path,
        docx_database=state / "missing-docx.sqlite3",
        office_database=state / "missing-office.sqlite3",
        text_database=state / "missing-text.sqlite3",
        audio_database=state / "missing-audio.sqlite3",
        image_database=state / "missing-image.sqlite3",
        video_database=state / "missing-video.sqlite3",
    )
    config.organization_min_confidence = 0.72
    calibration = CalibrationParameters(
        calibration_version="calibration-v1",
        model_signature="model-v1",
        measured=True,
        min_score_by_family={},
        min_margin_by_family={},
    )
    config.curation_policy_bundle = FastCurationPolicyBundle(
        policy=FastCurationPolicy(
            calibration=calibration,
            policy_version="fast-curation-policy-v1",
        ),
        model_signature="model-v1",
        representation_version="repr-v1",
        ontology_version="ontology-v1",
        prototype_version="proto-v1",
        policy_version="fast-curation-policy-v1",
        calibration_version="calibration-v1",
        prototype_set_fingerprint="0" * 64,
        prototype_scope="fixture",
    )
    return config, FakeFramework(), 9, 7


def test_adapter_reads_catalog_curation_current_state_and_closed_owners(tmp_path: Path) -> None:
    config, framework, run_id, scan_id = _make_fixture(tmp_path)
    result = verify_current_corpus(
        config,
        root=tmp_path / "corpus",
        framework_state=framework,
        run_id=run_id,
        scan_id=scan_id,
        admission_result={"status": "completed", "exclusion_reasons": {}, "pending_physical_admission": 0},
        phase="after_semantic",
    )
    assert result.passed, result.to_dict()
    assert result.metrics["classified"] == 1
    assert result.metrics["unclassified"] == 1


def test_adapter_rejects_missing_current_curation_decision(tmp_path: Path) -> None:
    config, framework, run_id, scan_id = _make_fixture(tmp_path)
    with sqlite3.connect(config.document_catalog_database) as connection:
        connection.execute("DELETE FROM curator_decisions")
        connection.commit()
    result = verify_current_corpus(
        config,
        root=tmp_path / "corpus",
        framework_state=framework,
        run_id=run_id,
        scan_id=scan_id,
        admission_result={"status": "completed", "exclusion_reasons": {}, "pending_physical_admission": 0},
        phase="after_semantic",
    )
    assert not result.passed
    assert any(issue.code == "classified_record_missing" for issue in result.failures)


def test_adapter_rejects_missing_dedup_or_terminal_semantic_owner(tmp_path: Path) -> None:
    config, framework, run_id, scan_id = _make_fixture(tmp_path)
    config.dedup_database.unlink()
    result = verify_current_corpus(
        config,
        root=tmp_path / "corpus",
        framework_state=framework,
        run_id=run_id,
        scan_id=scan_id,
        admission_result={"status": "completed", "exclusion_reasons": {}, "pending_physical_admission": 0},
        phase="before_semantic",
    )
    assert not result.passed
    assert any("dedup" in issue.detail.casefold() for issue in result.failures)

    config, framework, run_id, scan_id = _make_fixture(tmp_path / "terminal")
    (config.state_directory / "semantic.sqlite3").unlink()
    result = verify_current_corpus(
        config,
        root=tmp_path / "terminal" / "corpus",
        framework_state=framework,
        run_id=run_id,
        scan_id=scan_id,
        admission_result={"status": "completed", "exclusion_reasons": {}, "pending_physical_admission": 0},
        phase="after_semantic",
    )
    assert not result.passed
    assert any("semantic" in issue.detail.casefold() for issue in result.failures)


def test_adapter_rejects_partial_admission_before_semantic(tmp_path: Path) -> None:
    config, framework, run_id, scan_id = _make_fixture(tmp_path)
    result = verify_current_corpus(
        config,
        root=tmp_path / "corpus",
        framework_state=framework,
        run_id=run_id,
        scan_id=scan_id,
        admission_result={"status": "partial", "exclusion_reasons": {}, "pending_physical_admission": 0},
        phase="before_semantic",
    )
    assert not result.passed
    assert any("admission" in issue.detail.casefold() for issue in result.failures)


def test_adapter_resolves_inventory_successor_for_current_owner_rows(tmp_path: Path) -> None:
    config, framework, run_id, scan_id = _make_fixture(tmp_path, successor=True)
    result = verify_current_corpus(
        config,
        root=tmp_path / "corpus",
        framework_state=framework,
        run_id=run_id,
        scan_id=scan_id,
        admission_result={"status": "completed", "exclusion_reasons": {}, "pending_physical_admission": 0},
        phase="after_semantic",
    )
    assert result.passed, result.to_dict()


def test_terminal_semantic_skip_receipt_allows_no_semantic_owner(tmp_path: Path) -> None:
    config, framework, run_id, scan_id = _make_fixture(tmp_path)
    (config.state_directory / "semantic.sqlite3").unlink()
    framework.read_run_stages = lambda _run_id: (
        {
            "stage": "semantic",
            "status": "skipped",
            "details": {
                "selected_sources": [],
                "image_available": False,
                "source_unavailable": {},
            },
        },
    )
    result = verify_current_corpus(
        config,
        root=tmp_path / "corpus",
        framework_state=framework,
        run_id=run_id,
        scan_id=scan_id,
        admission_result={"status": "completed", "exclusion_reasons": {}, "pending_physical_admission": 0},
        phase="after_semantic",
    )
    assert result.passed, result.to_dict()
