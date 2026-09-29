"""Deterministic Fast Curation service tests with no model downloads."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from neocortex.semantic.fast_curation_policy import (
    CalibrationParameters,
    FastCurationPolicy,
)
from neocortex.semantic.fast_curation_policy_bundle import FastCurationPolicyBundle
from neocortex.semantic.fast_curation_prototypes import FastCurationPrototype, PrototypeSet
from neocortex.semantic.fast_curation_service import (
    CurationRepresentation,
    FastCurationConfig,
    MemoryEmbeddingCache,
    fit_text_to_model,
    run_fast_curation,
)
from neocortex.semantic.semantic_config import (
    SemanticModelUnavailableError,
    compact_multilingual_text_model,
    multilingual_text_model,
)


@dataclass(frozen=True)
class _Output:
    request_id: str
    vector: tuple[float, ...]


class _FakeBackend:
    def __init__(self, model, *, calls: list[tuple[str, ...]]) -> None:
        self.model = model
        self.max_batch_size = 2
        self.calls = calls

    def text_token_counts(self, texts):
        return tuple(max(1, (len(text) + 3) // 4) for text in texts), 256

    def embed(self, requests):
        self.calls.append(tuple(request.request_id for request in requests))
        outputs = []
        for request in requests:
            text = request.text.casefold()
            vector = [0.0] * self.model.dimensions
            if "report" in text and "testing" in text:
                vector[0] = 0.8
                vector[2] = 0.6
            elif "report" in text:
                vector[0] = 1.0
            elif "manual" in text:
                vector[1] = 1.0
            elif "testing" in text:
                vector[2] = 1.0
            elif "maintenance" in text:
                vector[3] = 1.0
            else:
                vector[0] = 0.7
                vector[1] = 0.7
            outputs.append(_Output(request.request_id, tuple(vector)))
        return tuple(outputs)

    def close(self) -> None:
        return None


def _prototypes(*, four_axes: bool = False) -> PrototypeSet:
    values = [
        FastCurationPrototype("kind:report", "report", "document_kind", "report", "A report document with documented results."),
        FastCurationPrototype("kind:manual", "manual", "document_kind", "manual", "A maintenance or operating manual document."),
    ]
    if four_axes:
        values.extend(
            [
                FastCurationPrototype("activity:testing", "testing", "activity", "testing", "Testing and measurement activity."),
                FastCurationPrototype("activity:maintenance", "maintenance", "activity", "maintenance", "Maintenance activity."),
            ]
        )
    return PrototypeSet(tuple(values))


def _policy(model_signature: str, *, four_axes: bool = False) -> FastCurationPolicy:
    families = {"document_kind": 0.40}
    margins = {"document_kind": 0.10}
    if four_axes:
        families["activity"] = 0.40
        margins["activity"] = 0.10
    return FastCurationPolicy.from_calibration(
        CalibrationParameters(
            "cal-v1",
            model_signature,
            True,
            families,
            margins,
            min_text_chars=3,
            min_evidence_count=2,
        )
    )


def _representation(document_id: str, text: str, *, path: str = "/old/name.pdf") -> CurationRepresentation:
    return CurationRepresentation(
        document_id=document_id,
        content_text=text,
        source_kind="pdf",
        file_key=document_id,
        input_signature=f"input-{document_id}",
        path_context=path,
    )


def _run(
    tmp_path: Path,
    *,
    inputs,
    cache=None,
    model=None,
    policy=None,
    prototypes=None,
    backend_factory=None,
    **kwargs,
):
    model = model or compact_multilingual_text_model()
    calls: list[tuple[str, ...]] = []
    factory = backend_factory or (lambda selected, **_kwargs: _FakeBackend(selected, calls=calls))
    result = run_fast_curation(
        FastCurationConfig(model=model, batch_size=2),
        tmp_path / "root",
        tmp_path / "state",
        inputs=inputs,
        embedding_cache=cache,
        policy=policy or _policy(model.model_signature),
        prototypes=prototypes or _prototypes(),
        backend_factory=factory,
        **kwargs,
    )
    return result, calls


@pytest.fixture
def roots(tmp_path: Path):
    (tmp_path / "root").mkdir()
    (tmp_path / "state").mkdir()
    return tmp_path


def test_batch_matrix_decision_and_content_cache_survive_rename(roots: Path) -> None:
    cache = MemoryEmbeddingCache()
    first, first_calls = _run(
        roots,
        inputs=[_representation("a", "report"), _representation("b", "manual")],
        cache=cache,
    )
    assert first.metrics.classified == 2
    assert first.metrics.vectors_per_document == 1.0
    assert len(first_calls) == 2
    assert first.decisions[0].top1_score is not None
    assert first.decisions[0].top2_score is not None
    assert first.decisions[0].margin is not None

    second, second_calls = _run(
        roots,
        inputs=[_representation("renamed", "report", path="/new/name.pdf")],
        cache=cache,
    )
    assert second.metrics.cache_hits == 1
    assert second.metrics.cache_misses == 0
    assert second_calls == []
    assert second.decisions[0].content_embedding_key == first.decisions[0].content_embedding_key

    changed, changed_calls = _run(
        roots,
        inputs=[_representation("renamed", "report changed")],
        cache=cache,
    )
    assert changed.metrics.cache_misses == 1
    assert changed_calls


def test_model_and_prototype_versions_invalidate_their_distinct_layers(roots: Path) -> None:
    cache = MemoryEmbeddingCache()
    compact = compact_multilingual_text_model()
    first, _calls = _run(
        roots,
        inputs=[_representation("a", "report")],
        cache=cache,
        model=compact,
    )
    jina = multilingual_text_model()
    second, second_calls = _run(
        roots,
        inputs=[_representation("a", "report")],
        cache=cache,
        model=jina,
        policy=_policy(jina.model_signature),
    )
    assert second.metrics.cache_misses == 1
    assert second_calls

    values = tuple(
        FastCurationPrototype(
            value.prototype_id,
            value.concept_id,
            value.family,
            value.label,
            value.description,
            aliases=value.aliases,
            prototype_version="prototype-v2",
        )
        for value in _prototypes().prototypes
    )
    changed_set = PrototypeSet(values, prototype_version="prototype-v2")
    third, _third_calls = _run(
        roots,
        inputs=[_representation("a", "report")],
        cache=cache,
        prototypes=changed_set,
        policy=_policy(compact.model_signature),
    )
    assert third.metrics.cache_hits == 1
    assert third.decisions[0].decision_cache_key != first.decisions[0].decision_cache_key


def test_mapping_inputs_preserve_explicit_source_identity(roots: Path) -> None:
    result, _calls = _run(
        roots,
        inputs={
            "representations": [
                {
                    "document_id": "file-key",
                    "source_kind": "docx",
                    "file_key": "file-key",
                    "input_signature": "route-text-v1",
                    "content_text": "report",
                    "path_context": "/root/changed-name.docx",
                }
            ]
        },
    )
    evidence = result.decisions[0]
    assert evidence.source_kind == "docx"
    assert evidence.file_key == "file-key"
    assert evidence.input_signature == "route-text-v1"
    assert evidence.source_path.endswith("changed-name.docx")


def test_invalid_representation_fallback_remains_bounded_and_fail_safe(roots: Path) -> None:
    result, _calls = _run(
        roots,
        inputs=[
            {
                "document_id": "bad",
                "source_kind": "pdf",
                "path_context": "/root/bad.pdf",
                "content_text": "",
            }
        ],
    )
    assert result.metrics.documents_seen == 1
    assert result.decisions[0].document_id == "bad"
    assert result.decisions[0].decision_reason


def test_no_policy_does_not_load_backend_and_abstains_safely(roots: Path) -> None:
    called = False

    def forbidden(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("uncalibrated policy must not load a model")

    result = run_fast_curation(
        FastCurationConfig(),
        roots / "root",
        roots / "state",
        inputs=[_representation("a", "report")],
        backend_factory=forbidden,
    )
    assert called is False
    assert result.status == "completed"
    assert result.metrics.calibration_loaded is False
    assert result.decisions[0].decision_reason == "calibration_unavailable"


def test_default_model_tracks_valid_fresh_holdout_selection_without_calibration() -> None:
    assert FastCurationConfig().model.model_id == "jinaai/jina-embeddings-v2-base-es"


def test_missing_model_is_local_only_capability_abstention(roots: Path) -> None:
    observed: dict[str, object] = {}

    def missing(_model, **kwargs):
        observed.update(kwargs)
        raise SemanticModelUnavailableError("fixture model is not cached")

    result, _calls = _run(
        roots,
        inputs=[_representation("a", "report")],
        backend_factory=missing,
    )
    assert observed["local_files_only"] is True
    assert result.status == "capability_partial"
    assert result.decisions[0].decision_reason == "capability_unavailable"


def test_wrong_model_and_prototype_bundle_are_rejected(roots: Path) -> None:
    model = compact_multilingual_text_model()
    wrong_model_policy = _policy(multilingual_text_model().model_signature)
    wrong, _calls = _run(
        roots,
        inputs=[_representation("a", "report")],
        model=model,
        policy=wrong_model_policy,
    )
    assert wrong.decisions[0].decision_reason == "calibration_model_mismatch"

    policy = _policy(model.model_signature)
    bundle = FastCurationPolicyBundle(
        policy,
        model.model_signature,
        "fast-curation-representation/v1",
        "neocortex.document-taxonomy",
        "fast-curation-prototype-set-v1",
        policy.policy_version,
        "cal-v1",
        "f" * 64,
        _prototypes().ontology_id,
    )
    mismatch, _calls = _run(
        roots,
        inputs=[_representation("a", "report")],
        model=model,
        policy=None,
        policy_bundle=bundle,
    )
    assert mismatch.decisions[0].decision_reason == "prototype_set_mismatch"


def test_cancellation_is_checked_inside_processing_batch(roots: Path) -> None:
    class Cancellation:
        def __init__(self) -> None:
            self.calls = 0

        def checkpoint(self) -> None:
            self.calls += 1
            if self.calls >= 2:
                raise RuntimeError("cancelled")

    cancellation = Cancellation()
    with pytest.raises(RuntimeError, match="cancelled"):
        _run(
            roots,
            inputs=[_representation("a", "report"), _representation("b", "manual")],
            cancellation=cancellation,
            resource_coordinator=object(),
        )
    assert cancellation.calls >= 2


def test_document_kind_is_primary_axis_in_evidence(roots: Path) -> None:
    policy = _policy(compact_multilingual_text_model().model_signature, four_axes=True)
    result, _calls = _run(
        roots,
        inputs=[_representation("a", "report testing")],
        policy=policy,
        prototypes=_prototypes(four_axes=True),
    )
    evidence = result.decisions[0]
    assert evidence.classified
    assert evidence.top_candidates[0].family == "document_kind"
    assert tuple(evidence.selected_by_family) == ("document_kind", "activity")


def test_model_tokenizer_fit_keeps_bounded_unicode_tail_without_full_chunking() -> None:
    class TinyTokenizer:
        def text_token_counts(self, texts):
            return tuple(max(1, (len(text) + 3) // 4) for text in texts), 24

    text = "Título español áéíóú " + ("tabla Ω resistencia " * 100) + " conclusión final"
    fitted = fit_text_to_model(TinyTokenizer(), text, max_chars=4_000)
    count, limit = TinyTokenizer().text_token_counts((fitted,))
    assert count[0] <= limit
    assert len(fitted) < len(text)
    assert "conclusión" in fitted
