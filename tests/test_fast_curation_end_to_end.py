"""Offline end-to-end coverage for the physical Fast Curation contract.

The fixtures in this module are deliberately small and private.  The test
uses the real inventory, admission, routes, Catalog, organization and
residual/layout owners; only the two model backends and the KIO boundary are
contained fixtures.  No gold label is placed in a production DTO: expected
classes are retained only by the test's digest ledger.
"""

from __future__ import annotations

import hashlib
import html
import json
import sqlite3
import zipfile
from collections import Counter
from dataclasses import replace
from pathlib import Path

import pymupdf as fitz

from neocortex.runtime.models import FrameworkConfig
from neocortex.runtime.control.global_resources import (
    GlobalResourceCoordinator,
    ResourceSample,
)
from neocortex.runtime.orchestration import orchestrator as orchestrator_module
from neocortex.runtime.orchestration.orchestrator import FrameworkOrchestrator
from neocortex.runtime.orchestration.route_registry import builtin_route_registry
from neocortex.semantic.fast_curation_embeddings import (
    CurationRepresentation,
    EmbeddingRequest,
)
from neocortex.semantic.fast_curation_policy import (
    CalibrationParameters,
    FastCurationPolicy,
)
from neocortex.semantic.fast_curation_policy_bundle import FastCurationPolicyBundle
from neocortex.semantic.fast_curation_prototypes import (
    FastCurationPrototype,
    PrototypeSet,
)
from neocortex.semantic.semantic_config import multilingual_text_model
from neocortex.semantic.semantic_models import (
    BackendEmbedding,
    EmbeddingModelSpec,
    EmbeddingRole,
)
from neocortex.semantic import semantic_service
from neocortex.semantic.fast_curation_service import (
    FastCurationConfig,
    MemoryEmbeddingCache,
    run_fast_curation,
)
from neocortex.workflow.mutations import BackendOutcome


_KNOWN_KINDS = {
    "informe_tecnico": "Ingenieria_y_documentacion/Informes_y_referencias",
    "ficha_tecnica": "Ingenieria_y_documentacion/Manuales_catalogos_y_fichas",
    "manual_equipo": "Ingenieria_y_documentacion/Manuales_catalogos_y_fichas",
}


def _write_pdf(path: Path, text: str) -> None:
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), text)
    document.save(path)
    document.close()


def _write_docx(path: Path, text: str) -> None:
    value = html.escape(text)
    document = f"""<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body><w:p><w:r><w:t>{value}</w:t></w:r></w:p>
  <w:sectPr/></w:body>
</w:document>"""
    content_types = """<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Override PartName="/word/document.xml"
 ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""
    relationships = """<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1"
 Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
 Target="word/document.xml"/>
</Relationships>"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", relationships)
        archive.writestr("word/document.xml", document)


def _write_xlsx(path: Path, text: str) -> None:
    value = html.escape(text)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr(
            "xl/workbook.xml",
            """<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
 <sheets><sheet name="Mediciones" sheetId="1" r:id="rId1"/></sheets></workbook>""",
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            """<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"
 Target="worksheets/sheet1.xml"/></Relationships>""",
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            f"""<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
 <sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>{value}</t></is></c></row></sheetData>
</worksheet>""",
        )


def _fixture_prototypes() -> PrototypeSet:
    ontology_version = "e2e-ontology-v1"
    prototype_version = "e2e-prototypes-v1"
    values = (
        FastCurationPrototype(
            "kind:informe",
            "informe_tecnico",
            "document_kind",
            "Informe técnico",
            "Informe técnico con resultados de pruebas y conclusiones.",
            ontology_version=ontology_version,
            prototype_version=prototype_version,
        ),
        FastCurationPrototype(
            "kind:ficha",
            "ficha_tecnica",
            "document_kind",
            "Ficha técnica",
            "Ficha técnica de equipo, especificaciones y datos.",
            ontology_version=ontology_version,
            prototype_version=prototype_version,
        ),
        FastCurationPrototype(
            "kind:manual",
            "manual_equipo",
            "document_kind",
            "Manual de equipo",
            "Manual de equipo con instrucciones de operación y mantenimiento.",
            ontology_version=ontology_version,
            prototype_version=prototype_version,
        ),
    )
    return PrototypeSet(
        values,
        ontology_id="neocortex.document-taxonomy",
        ontology_version=ontology_version,
        prototype_version=prototype_version,
    )


def _fixture_bundle(model: EmbeddingModelSpec, prototypes: PrototypeSet) -> FastCurationPolicyBundle:
    policy = FastCurationPolicy.from_calibration(
        CalibrationParameters(
            "e2e-calibration-v1",
            model.model_signature,
            True,
            {"document_kind": 0.50},
            {"document_kind": 0.20},
            min_text_chars=10,
            min_evidence_count=2,
        ),
        required_families=("document_kind",),
        policy_version="e2e-policy-v1",
    )
    return FastCurationPolicyBundle(
        policy,
        model.model_signature,
        "fast-curation-representation/v1",
        prototypes.ontology_version,
        prototypes.prototype_version,
        policy.policy_version,
        "e2e-calibration-v1",
        prototypes.fingerprint,
        prototypes.ontology_id,
    )


class _FastFixtureBackend:
    """Content-controlled deterministic encoder for the Fast Curation seam."""

    def __init__(self, model: EmbeddingModelSpec, *, calls: list[tuple[str, ...]]) -> None:
        self.model = model
        self.max_batch_size = 64
        self.calls = calls

    def text_token_counts(self, texts: tuple[str, ...]) -> tuple[tuple[int, ...], int]:
        return tuple(max(1, len(value.split()) + 2) for value in texts), 512

    def embed(self, requests: tuple[EmbeddingRequest, ...]) -> tuple[BackendEmbedding, ...]:
        self.calls.append(tuple(request.request_id for request in requests))
        result: list[BackendEmbedding] = []
        for request in requests:
            vector = [0.0] * self.model.dimensions
            if request.role is EmbeddingRole.QUERY:
                if request.request_id.endswith("informe"):
                    vector[0] = 1.0
                elif request.request_id.endswith("ficha"):
                    vector[1] = 1.0
                elif request.request_id.endswith("manual"):
                    vector[2] = 1.0
            else:
                text = request.text.casefold()
                text = (
                    text.replace("á", "a")
                    .replace("é", "e")
                    .replace("í", "i")
                    .replace("ó", "o")
                    .replace("ú", "u")
                )
                if "informe tecnico" in text and "ficha tecnica" in text:
                    vector[3] = 1.0
                elif "informe tecnico" in text:
                    vector[0] = 1.0
                elif "ficha tecnica" in text:
                    vector[1] = 1.0
                elif "manual de equipo" in text:
                    vector[2] = 1.0
                else:
                    vector[3] = 1.0
            result.append(
                BackendEmbedding(
                    request.request_id,
                    tuple(vector),
                    {"backend": "fast-curation-e2e-fixture"},
                )
            )
        return tuple(result)

    def close(self) -> None:
        return None


class _FullFixtureBackend:
    def __init__(self, model: EmbeddingModelSpec, *, calls: list[tuple[str, ...]]) -> None:
        self.model = model
        self.max_batch_size = 64
        self.calls = calls

    def text_token_counts(self, texts: tuple[str, ...]) -> tuple[tuple[int, ...], int]:
        return tuple(max(1, len(value.split()) + 2) for value in texts), 512

    def text_tokenizer_contract(self) -> tuple[str, int]:
        return "full-semantic-e2e-tokenizer-v1", 512

    def embed(self, requests: tuple[EmbeddingRequest, ...]) -> tuple[BackendEmbedding, ...]:
        self.calls.append(tuple(request.request_id for request in requests))
        return tuple(
            BackendEmbedding(
                request.request_id,
                (1.0,) + (0.0,) * (self.model.dimensions - 1),
                {"backend": "full-semantic-e2e-fixture"},
            )
            for request in requests
        )

    def close(self) -> None:
        return None


class _FixtureTrash:
    supports_empty_directories = True

    def __init__(self, trash_root: Path) -> None:
        self.trash_root = trash_root
        self.effects: list[str] = []

    def apply_snapshot(
        self,
        snapshot,
        *,
        root: Path,
        source_digest: str,
        object_kind: str = "regular_file",
        **_kwargs: object,
    ) -> BackendOutcome:
        del root
        from tests.test_framework_actions import _fixture_trash_receipt

        self.effects.append(snapshot.path)
        target = self.trash_root / str(len(self.effects))
        receipt = json.loads(_fixture_trash_receipt(snapshot, source_digest, target))
        receipt["object_kind"] = object_kind
        receipt["trash"]["object_kind"] = object_kind
        return BackendOutcome(
            "applied",
            "fixture_verified",
            receipt_json=json.dumps(receipt),
        )

    def apply_many_snapshots(self, items, *, root: Path):
        return tuple(
            self.apply_snapshot(
                item[0],
                root=root,
                source_digest=item[1],
                object_kind=item[2] if len(item) > 2 else "regular_file",
            )
            for item in items
        )


def _full_model() -> EmbeddingModelSpec:
    return EmbeddingModelSpec(
        "e2e-full-semantic-model-v1",
        "e2e-full-semantic-space-v1",
        multilingual_text_model().modality,
        "fixture/full-semantic",
        "1",
        4,
        "fastembed-onnx-cpu",
        (EmbeddingRole.QUERY, EmbeddingRole.PASSAGE),
    )


def _new_representation(document_id: str, text: str, path: str) -> CurationRepresentation:
    return CurationRepresentation(
        document_id=document_id,
        source_kind="text",
        file_key=document_id,
        input_signature=f"fixture-{hashlib.sha256(text.encode()).hexdigest()}",
        content_text=text,
        path_context=path,
    )


def test_fast_curation_full_pipeline_e2e(tmp_path, monkeypatch) -> None:
    """Exercise 100+ sources through physical layout and one Full Semantic callback."""

    root = tmp_path / "corpus"
    state_directory = tmp_path / "state"
    root.mkdir()
    state_directory.mkdir()

    expected: dict[str, str | None] = {}
    source_bytes: dict[str, bytes] = {}
    formats = ("txt", "pdf", "docx", "xlsx")
    phrases = {
        "informe_tecnico": "Informe técnico de pruebas del transformador con resultados y conclusión.",
        "ficha_tecnica": "Ficha técnica del equipo con especificaciones, tensión y datos nominales.",
        "manual_equipo": "Manual de equipo para operación y mantenimiento del interruptor.",
        "ambiguous": "Informe técnico y ficha técnica mezclados sin una clase inequívoca.",
        "ood": "Receta familiar de cocina y novela de ficción sin relación técnica.",
    }

    def add_source(name: str, kind: str, index: int, *, fmt: str | None = None) -> None:
        selected = fmt or (formats[index % len(formats)] if index < 4 else "txt")
        text = f"{phrases[kind]} Registro fixture {index:03d}."
        path = root / f"{name}-{index:03d}.{selected}"
        if selected == "txt":
            path.write_text(text, encoding="utf-8")
        elif selected == "pdf":
            _write_pdf(path, text)
        elif selected == "docx":
            _write_docx(path, text)
        else:
            _write_xlsx(path, text)
        payload = path.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        source_bytes[digest] = payload
        expected[digest] = _KNOWN_KINDS.get(kind)

    # Thirty-six unambiguous records plus seventy-two explicit ambiguous/OOD
    # records keep the run non-vacuous while exercising a substantial input.
    for kind in ("informe_tecnico", "ficha_tecnica", "manual_equipo"):
        for index in range(12):
            add_source(kind, kind, index)
    for index in range(36):
        add_source("ambiguous", "ambiguous", index)
        add_source("ood", "ood", index)

    duplicate = root / "duplicate-informe.txt"
    original_digest = next(
        key for key, value in expected.items()
        if value == _KNOWN_KINDS["informe_tecnico"]
    )
    duplicate.write_bytes(source_bytes[original_digest])
    archive_payload = phrases["informe_tecnico"] + " Registro desde ZIP."
    with zipfile.ZipFile(root / "fixture-bundle.zip", "w") as archive:
        archive.writestr("from-archive.txt", archive_payload)
    (root / "runtime.exe").write_bytes(b"MZ" + b"fixture-redlist")

    prototypes = _fixture_prototypes()
    fast_model = multilingual_text_model()
    bundle = _fixture_bundle(fast_model, prototypes)
    fast_calls: list[tuple[str, ...]] = []
    full_calls: list[tuple[str, ...]] = []

    import neocortex.platform.sqlite_runtime_attestation as sqlite_attestation
    import neocortex.documents.semantic_curation_gate as curation_gate_module
    import neocortex.safety.kio_trash as kio_trash
    import neocortex.semantic.fast_curation_policy_bundle as policy_bundle_module
    import neocortex.semantic.fast_curation_prototypes as prototypes_module
    import neocortex.semantic.fast_curation_service as fast_service
    import neocortex.workflow.mutations as mutations

    # The run uses the real coordinator, but its existing observation seam is
    # fed by a bounded fixture sample.  Ambient host PSI (not this pipeline)
    # must not decide whether a deterministic unit test can start.
    capacity = 8 * 1024 * 1024 * 1024
    stable_sample = ResourceSample(
        available_physical=2 * capacity,
        available_commit=2 * capacity,
        total_physical=4 * capacity,
        total_commit=4 * capacity,
        cpu_load_percent=0.0,
        external_cpu_cores=0.0,
        own_cpu_cores=0.0,
        effective_cpu_capacity=4,
        memory_pressure_some_percent=0.0,
        memory_pressure_full_percent=0.0,
        io_pressure_some_percent=0.0,
        io_pressure_full_percent=0.0,
    )
    coordinators: list[GlobalResourceCoordinator] = []

    def make_coordinator(route_order, limits, *, cancellation, checkpoint, route_memory_budgets):
        coordinator = GlobalResourceCoordinator(
            route_order,
            replace(
                limits,
                memory_budget_bytes=capacity,
                temp_budget_bytes=capacity,
                min_free_memory_bytes=0,
                min_free_commit_bytes=0,
                cpu_slots=4,
                native_thread_slots=4,
                max_cpu_load_percent=100.0,
                wait_timeout_seconds=2.0,
                poll_interval_seconds=0.01,
            ),
            cpu_load_probe=lambda: 0.0,
            effective_cpu_probe=lambda: 4,
            resource_probe=lambda: stable_sample,
            cancellation=cancellation,
            checkpoint=checkpoint,
            route_memory_budgets=route_memory_budgets,
        )
        coordinators.append(coordinator)
        return coordinator

    monkeypatch.setattr(orchestrator_module, "GlobalResourceCoordinator", make_coordinator)

    monkeypatch.setattr(mutations, "KioTrashBackend", lambda: _FixtureTrash(tmp_path / "trash"))
    monkeypatch.setattr(
        kio_trash,
        "preflight_kio_trash",
        lambda: type("FixturePreflight", (), {"client": "contained-fixture"})(),
    )
    monkeypatch.setattr(
        sqlite_attestation,
        "observe_platform_native_runtime",
        lambda **_kwargs: {"status": "approved", "observed": {}},
    )
    monkeypatch.setattr(
        policy_bundle_module,
        "default_calibrated_policy",
        lambda *args, **kwargs: bundle,
    )
    # Some verifier revisions retain a module-local import alias; patch that
    # pure loader too, without widening the model/KIO fixture boundary.
    monkeypatch.setattr(
        curation_gate_module,
        "default_calibrated_policy",
        lambda *args, **kwargs: bundle,
        raising=False,
    )
    monkeypatch.setattr(prototypes_module, "default_prototypes", lambda: prototypes)
    monkeypatch.setattr(
        fast_service,
        "_backend_factory_default",
        lambda model, **_kwargs: _FastFixtureBackend(model, calls=fast_calls),
    )
    monkeypatch.setattr(
        semantic_service,
        "_backend",
        lambda model, **_kwargs: _FullFixtureBackend(_full_model(), calls=full_calls),
    )

    builtin = builtin_route_registry()
    route_registry = {name: builtin[name] for name in ("pdf", "docx", "office", "text")}
    full_model = _full_model()
    callback_observations: list[dict[str, object]] = []

    def full_semantic_callback(run_id: int) -> object:
        del run_id
        final_files = tuple(
            path for path in root.rglob("*") if path.is_file()
        )
        assert (root / "Corpus_ordenado").is_dir()
        assert (root / "Sin_clasificar").is_dir()
        callback_observations.append({"paths": final_files})
        return semantic_service.index_text_embeddings(
            state_directory,
            source_kinds=("pdf", "docx", "xlsx", "text"),
            model=full_model,
            local_files_only=True,
            threads=1,
        )

    config = FrameworkConfig(
        root=root,
        state_directory=state_directory,
        route="all",
        apply_actions=True,
        document_catalog_enabled=True,
        organization_min_confidence=0.0,
        curation_batch_size=32,
        global_cpu_slots=8,
        global_max_cpu_load_percent=100.0,
        global_min_free_memory_bytes=0,
        global_min_free_commit_bytes=0,
    )
    for policy_field in ("fast_curation_policy_bundle", "curation_policy_bundle"):
        try:
            setattr(config, policy_field, bundle)
        except AttributeError:
            pass
    try:
        result = FrameworkOrchestrator(
            config,
            route_registry=route_registry,
            lifecycle_stage_runner=full_semantic_callback,
            lifecycle_stage_details={"fixture": "full-semantic-e2e"},
        ).run()
    finally:
        for coordinator in coordinators:
            coordinator.close()

    assert callback_observations
    assert full_calls
    assert result.route_failures == {}, result.route_failures
    assert result.route_results["corpus_verification"]["passed"] is True
    assert result.route_results["fast_curation"]["documents_seen"] >= 100
    assert result.route_results["fast_curation"]["classified"] > 0
    assert result.route_results["fast_curation"]["abstained"] > 0
    admission = result.route_results["curation_admission"]
    assert admission["rounds"] >= 1
    assert result.route_results["zip_intake"]["status"] in {"completed", "partial"}
    assert result.actions.duplicates_trashed >= 1
    assert admission["redlist_trashed"] >= 1

    assert {path.name for path in root.iterdir()} == {"Corpus_ordenado", "Sin_clasificar"}
    files = tuple(path for path in root.rglob("*") if path.is_file())
    organized_counts: Counter[str] = Counter()
    for path in files:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest not in expected:
            # The ZIP child is an intentional, independently admitted source.
            if path.read_text(encoding="utf-8", errors="ignore").startswith(phrases["informe_tecnico"]):
                expected[digest] = _KNOWN_KINDS["informe_tecnico"]
            else:
                continue
        kind_root = expected[digest]
        if kind_root is None:
            assert path.is_relative_to(root / "Sin_clasificar" / "_MIME")
        else:
            assert path.is_relative_to(root / "Corpus_ordenado" / kind_root)
            organized_counts[kind_root] += 1

    assert organized_counts == Counter(
        {
            _KNOWN_KINDS["informe_tecnico"]: 13,  # 12 originals + one ZIP child
            _KNOWN_KINDS["ficha_tecnica"]: 12,
            _KNOWN_KINDS["manual_equipo"]: 12,
        }
    )

    empty_dirs = [path for path in (root / "Corpus_ordenado").rglob("*") if path.is_dir() and not any(path.iterdir())]
    empty_dirs.extend(
        path for path in (root / "Sin_clasificar").rglob("*") if path.is_dir() and not any(path.iterdir())
    )
    assert not empty_dirs

    with sqlite3.connect(state_directory / "document_catalog.sqlite3") as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT source_kind,file_key,path,resource_binding_json FROM documents WHERE active=1"
        ).fetchall()
        decisions = connection.execute(
            "SELECT decision,COUNT(*) AS count FROM curator_decisions GROUP BY decision"
        ).fetchall()
        assert rows
        assert {row["decision"]: row["count"] for row in decisions}["CLASSIFIED"] > 0
        assert {row["decision"]: row["count"] for row in decisions}["ABSTAIN"] > 0
        for row in rows:
            current = Path(row["path"])
            assert current.is_file()
            binding = json.loads(row["resource_binding_json"])
            assert binding["physical_anchor_path"] == str(current)

    semantic_database = state_directory / "semantic.sqlite3"
    with sqlite3.connect(semantic_database) as connection:
        head_count = connection.execute("SELECT COUNT(*) FROM published_embedding_heads").fetchone()[0]
        active_paths = [row[0] for row in connection.execute("SELECT path FROM semantic_items WHERE active=1")]
    assert head_count == 1
    assert len(active_paths) >= 100
    assert all(Path(path).is_file() for path in active_paths)

    # A second service pass uses the same durable cache key after a rename;
    # content changes and a model-version change invalidate independently.
    cache = MemoryEmbeddingCache()
    direct_calls: list[tuple[str, ...]] = []
    def service_backend(model, **_kwargs):
        return _FastFixtureBackend(model, calls=direct_calls)
    direct_root = tmp_path / "direct-root"
    direct_root.mkdir()
    direct_config = FastCurationConfig(
        model=fast_model,
        local_files_only=True,
        batch_size=8,
        max_documents=10,
    )
    direct_input = [_new_representation("doc-a", "Informe técnico de pruebas estable.", "/old/a.txt")]
    first = run_fast_curation(
        direct_config,
        direct_root,
        state_directory,
        inputs=direct_input,
        policy_bundle=bundle,
        prototypes=prototypes,
        embedding_cache=cache,
        backend_factory=service_backend,
    )
    assert first.metrics.embeddings_produced == 1
    direct_calls.clear()
    renamed = run_fast_curation(
        direct_config,
        direct_root,
        state_directory,
        inputs=[_new_representation("doc-renamed", "Informe técnico de pruebas estable.", "/new/renamed.txt")],
        policy_bundle=bundle,
        prototypes=prototypes,
        embedding_cache=cache,
        backend_factory=service_backend,
    )
    assert renamed.metrics.cache_hits == 1
    assert direct_calls == []

    changed = run_fast_curation(
        direct_config,
        direct_root,
        state_directory,
        inputs=[_new_representation("doc-renamed", "Informe técnico de pruebas corregido.", "/new/renamed.txt")],
        policy_bundle=bundle,
        prototypes=prototypes,
        embedding_cache=cache,
        backend_factory=service_backend,
    )
    assert changed.metrics.cache_misses == 1
    assert direct_calls
    direct_calls.clear()
    changed_model = replace(
        fast_model,
        model_signature="e2e-fast-model-v2",
        vector_space="e2e-fast-space-v2",
    )
    changed_bundle = _fixture_bundle(changed_model, prototypes)
    changed_model_result = run_fast_curation(
        FastCurationConfig(model=changed_model, batch_size=8),
        direct_root,
        state_directory,
        inputs=direct_input,
        policy_bundle=changed_bundle,
        prototypes=prototypes,
        embedding_cache=cache,
        backend_factory=service_backend,
    )
    assert changed_model_result.metrics.cache_misses == 1
    assert direct_calls

    version_prototypes = PrototypeSet(
        tuple(
            replace(value, prototype_version="e2e-prototypes-v2")
            for value in prototypes.prototypes
        ),
        ontology_id=prototypes.ontology_id,
        ontology_version=prototypes.ontology_version,
        prototype_version="e2e-prototypes-v2",
    )
    version_bundle = _fixture_bundle(fast_model, version_prototypes)
    direct_calls.clear()
    version_result = run_fast_curation(
        direct_config,
        direct_root,
        state_directory,
        inputs=direct_input,
        policy_bundle=version_bundle,
        prototypes=version_prototypes,
        embedding_cache=cache,
        backend_factory=service_backend,
    )
    assert version_result.metrics.cache_hits == 1
    assert direct_calls
    assert all(
        request_id.startswith("prototype:")
        for batch in direct_calls
        for request_id in batch
    )
