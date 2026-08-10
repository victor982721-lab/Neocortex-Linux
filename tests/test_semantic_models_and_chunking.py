from __future__ import annotations

import math
from dataclasses import replace
from itertools import pairwise
from typing import Sequence, cast

import pytest

from _04_Nucleo_Operativo.semantic_chunking import (
    ChunkLimitExceeded,
    TextChunkingConfig,
    chunk_text_sections,
)
from _04_Nucleo_Operativo.semantic_models import (
    CalibrationStatus,
    ContentFingerprint,
    EmbeddingModality,
    EmbeddingModelSpec,
    EmbeddingRequest,
    EmbeddingRole,
    EvidenceDisposition,
    ExactSearchQuery,
    FusionEvidence,
    ResolvedSearchHit,
    SearchHit,
    SemanticEvidence,
    TextChunk,
    TextSection,
    VectorDType,
    cosine_similarity,
    decode_vector,
    encode_vector,
    fingerprint_bytes,
    fingerprint_chunks,
    fingerprint_text,
    validate_vector,
)


# region [01] Content identity and vector codec


def test_xxh3_stream_fingerprint_matches_contiguous_payload() -> None:
    payload = "subestación, transformador y protección".encode()
    assert fingerprint_chunks((payload[:7], payload[7:21], payload[21:])) == (
        fingerprint_bytes(payload)
    )
    with pytest.raises(ValueError, match="lowercase hexadecimal"):
        ContentFingerprint("ABC", 3, "bad")


@pytest.mark.parametrize("dtype,width", [(VectorDType.FLOAT16, 2), (VectorDType.FLOAT32, 4)])
def test_vector_codec_normalizes_and_has_explicit_compact_width(
    dtype: VectorDType,
    width: int,
) -> None:
    payload, original_norm = encode_vector((3.0, 4.0, 0.0), 3, dtype)
    decoded = decode_vector(payload, 3, dtype)
    assert len(payload) == 3 * width
    assert original_norm == pytest.approx(5.0)
    assert math.sqrt(sum(value * value for value in decoded)) == pytest.approx(
        1.0,
        abs=1e-3,
    )
    assert cosine_similarity(decoded, (0.6, 0.8, 0.0), 3) == pytest.approx(
        1.0,
        abs=1e-5,
    )


def test_vector_codec_rejects_wrong_dimensions_nonfinite_zero_and_bad_blob() -> None:
    with pytest.raises(ValueError, match="expected 3"):
        encode_vector((1.0, 2.0), 3, VectorDType.FLOAT16)
    with pytest.raises(ValueError, match="finite"):
        encode_vector((1.0, math.nan), 2, VectorDType.FLOAT16)
    with pytest.raises(ValueError, match="non-zero"):
        encode_vector((0.0, 0.0), 2, VectorDType.FLOAT16)
    with pytest.raises(ValueError, match="payload length"):
        decode_vector(b"short", 4, VectorDType.FLOAT16)


def test_dimensions_reject_bool_across_model_query_and_vector_boundaries() -> None:
    with pytest.raises(ValueError, match="integer between 1 and 65536"):
        EmbeddingModelSpec(
            "model",
            "space",
            EmbeddingModality.TEXT,
            "model-id",
            "1",
            True,
            "fixture",
            (EmbeddingRole.QUERY,),
        )
    with pytest.raises(ValueError, match="integer between 1 and 65536"):
        ExactSearchQuery(
            "query-model",
            "space",
            True,
            (1.0,),
            EmbeddingModality.TEXT,
        )
    with pytest.raises(ValueError, match="integer between 1 and 65536"):
        validate_vector((1.0,), True)
    with pytest.raises(ValueError, match="integer between 1 and 65536"):
        decode_vector(b"\x00\x00", True, VectorDType.FLOAT16)


# endregion [01]


# region [02] Natural bounded text chunks


def test_chunker_preserves_sections_is_deterministic_and_respects_both_bounds() -> None:
    config = TextChunkingConfig(
        max_chars=96,
        max_terms=12,
        overlap_chars=20,
        overlap_terms=3,
        min_natural_break_chars=32,
    )
    sections = (
        TextSection(
            "pdf_page",
            "1",
            "Protección diferencial del transformador. " * 12,
            {"page": 1},
        ),
        TextSection(
            "pdf_page",
            "2",
            "Interruptor de potencia y seccionador de barra. " * 8,
            {"page": 2},
        ),
    )
    first = chunk_text_sections("pdf:1", sections, config)
    second = chunk_text_sections("pdf:1", sections, config)
    assert first == second
    assert len(first) > 2
    assert [chunk.ordinal for chunk in first] == list(range(len(first)))
    assert {chunk.section_id for chunk in first} == {"1", "2"}
    assert all(len(chunk.text) <= config.max_chars for chunk in first)
    assert all(len(chunk.text.split()) <= config.max_terms for chunk in first)
    assert all(chunk.fingerprint == fingerprint_text(chunk.text) for chunk in first)
    assert all(chunk.chunking_signature == config.signature for chunk in first)
    assert all(chunk.end_char > chunk.start_char for chunk in first)


def test_chunker_normalizes_whitespace_without_losing_diacritics() -> None:
    chunks = chunk_text_sections(
        "doc:á",
        (TextSection("part", "body", "  tensión\n\n  eléctrica\tindustrial  "),),
        TextChunkingConfig(
            max_chars=128,
            max_terms=16,
            overlap_chars=0,
            overlap_terms=0,
            min_natural_break_chars=16,
        ),
    )
    assert len(chunks) == 1
    assert chunks[0].text == "tensión eléctrica industrial"


def test_chunk_identity_binds_immutable_provenance_and_ordinal() -> None:
    config = TextChunkingConfig(
        max_chars=128,
        max_terms=16,
        overlap_chars=0,
        overlap_terms=0,
        min_natural_break_chars=16,
    )
    original = chunk_text_sections(
        "pdf:identity",
        (TextSection("pdf_page", "1", "protección diferencial", {"adapter": "v2"}),),
        config,
    )[0]
    changed_provenance = chunk_text_sections(
        "pdf:identity",
        (TextSection("pdf_page", "1", "protección diferencial", {"adapter": "v3"}),),
        config,
    )[0]
    shifted = chunk_text_sections(
        "pdf:identity",
        (
            TextSection("metadata", "title", "transformador"),
            TextSection("pdf_page", "1", "protección diferencial", {"adapter": "v2"}),
        ),
        config,
    )[1]

    assert original.text == changed_provenance.text == shifted.text
    assert original.start_char == changed_provenance.start_char == shifted.start_char
    assert original.end_char == changed_provenance.end_char == shifted.end_char
    assert original.chunk_id != changed_provenance.chunk_id
    assert original.chunk_id != shifted.chunk_id


def _normalized_span_chunk() -> TextChunk:
    source_text = "  tensión\n\n  eléctrica\tindustrial  "
    normalized_text = "tensión eléctrica industrial"
    start_char = 11
    return TextChunk(
        chunk_id="chunk:normalized:0",
        item_id="item:normalized",
        ordinal=0,
        section_kind="body",
        section_id="1",
        start_char=start_char,
        end_char=start_char + len(source_text),
        text=normalized_text,
        fingerprint=fingerprint_text(normalized_text),
        chunking_signature="normalized-span-v1",
    )


def test_text_chunk_accepts_original_span_larger_than_normalized_text() -> None:
    chunk = _normalized_span_chunk()

    assert chunk.end_char - chunk.start_char > len(chunk.text)


@pytest.mark.parametrize(
    "invalid_offset",
    (cast(int, True), cast(int, 0.5), cast(int, "40")),
)
def test_text_chunk_rejects_bool_and_non_integer_offsets(
    invalid_offset: int,
) -> None:
    chunk = _normalized_span_chunk()

    with pytest.raises(ValueError, match="chunk offsets must be integers"):
        replace(chunk, start_char=invalid_offset)
    with pytest.raises(ValueError, match="chunk offsets must be integers"):
        replace(chunk, end_char=invalid_offset)


@pytest.mark.parametrize(
    "invalid_ordinal",
    (cast(int, True), cast(int, 0.5), cast(int, "0")),
)
def test_text_chunk_rejects_bool_and_non_integer_ordinals(
    invalid_ordinal: int,
) -> None:
    chunk = _normalized_span_chunk()

    with pytest.raises(ValueError, match="ordinal must be an integer"):
        replace(chunk, ordinal=invalid_ordinal)


def test_text_chunk_rejects_locator_span_shorter_than_normalized_text() -> None:
    chunk = _normalized_span_chunk()

    with pytest.raises(ValueError, match="shorter than normalized text"):
        replace(
            chunk,
            end_char=chunk.start_char + len(chunk.text) - 1,
        )


def test_text_chunk_preserves_existing_non_empty_locator_invariants() -> None:
    chunk = _normalized_span_chunk()

    with pytest.raises(ValueError, match="non-empty range"):
        replace(chunk, start_char=-1)
    with pytest.raises(ValueError, match="non-empty range"):
        replace(chunk, end_char=chunk.start_char)
    with pytest.raises(ValueError, match="text must be a string"):
        replace(chunk, text=cast(str, 7))


def test_chunker_raises_instead_of_silently_truncating_item() -> None:
    config = TextChunkingConfig(
        max_chars=64,
        max_terms=4,
        overlap_chars=0,
        overlap_terms=0,
        min_natural_break_chars=16,
        max_chunks_per_item=2,
    )
    with pytest.raises(ChunkLimitExceeded, match="exceeds 2 chunks"):
        chunk_text_sections(
            "large",
            (TextSection("body", "1", "uno dos tres cuatro cinco " * 10),),
            config,
        )


def test_chunker_fits_subword_heavy_windows_with_exact_token_counter() -> None:
    config = TextChunkingConfig(
        max_chars=1_600,
        max_terms=280,
        overlap_chars=192,
        overlap_terms=40,
        min_natural_break_chars=128,
        algorithm_version="token-fit-regression-v1",
        model_token_limit=512,
        tokenizer_signature="fixture-tokenizer-v1",
    )
    source = "ANSI/49T protección-diferencial IEC-61850/GOOSE " * 400

    def subword_heavy_counts(texts: Sequence[str]) -> tuple[tuple[int, ...], int]:
        return tuple(2 + 2 * len(text.split()) for text in texts), 512

    chunks = chunk_text_sections(
        "pdf:subword-heavy",
        (TextSection("pdf_page", "16", source),),
        config,
        token_counter=subword_heavy_counts,
    )
    repeated = chunk_text_sections(
        "pdf:subword-heavy",
        (TextSection("pdf_page", "16", source),),
        config,
        token_counter=subword_heavy_counts,
    )
    legacy = chunk_text_sections(
        "pdf:subword-heavy",
        (TextSection("pdf_page", "16", source),),
        replace(config, algorithm_version="token-fit-regression-v0"),
        token_counter=subword_heavy_counts,
    )

    assert len(chunks) > 2
    assert chunks == repeated
    assert [chunk.chunk_id for chunk in chunks] != [chunk.chunk_id for chunk in legacy]
    assert all(subword_heavy_counts((chunk.text,))[0][0] <= 512 for chunk in chunks)
    assert chunks[0].start_char == 0
    assert chunks[-1].end_char == len(source)
    assert all(current.start_char <= previous.end_char for previous, current in pairwise(chunks))


def test_chunker_rejects_payload_that_cannot_fit_one_source_character() -> None:
    config = TextChunkingConfig(
        max_chars=64,
        max_terms=8,
        overlap_chars=0,
        overlap_terms=0,
        min_natural_break_chars=16,
        model_token_limit=512,
        tokenizer_signature="fixture-tokenizer-v1",
    )

    with pytest.raises(ChunkLimitExceeded, match="one source character"):
        chunk_text_sections(
            "impossible",
            (TextSection("body", "1", "X"),),
            config,
            token_counter=lambda texts: (tuple(513 for _ in texts), 512),
        )


# endregion [02]


# region [03] Compatibility and advisory contracts


def test_public_string_contracts_raise_controlled_errors_before_strip() -> None:
    invalid_string = cast(str, 7)
    text = "validated text"
    fingerprint = fingerprint_text(text)

    with pytest.raises(ValueError, match="model_signature must be a string"):
        EmbeddingModelSpec(
            invalid_string,
            "space",
            EmbeddingModality.TEXT,
            "model-id",
            "1",
            8,
            "fixture",
            (EmbeddingRole.QUERY,),
        )
    with pytest.raises(ValueError, match="section_kind must be a string"):
        TextSection(invalid_string, "1", text)
    with pytest.raises(ValueError, match="text must be a string"):
        TextSection("body", "1", invalid_string)
    with pytest.raises(ValueError, match="request_id must be a string"):
        EmbeddingRequest(
            invalid_string,
            EmbeddingRole.QUERY,
            fingerprint,
            text=text,
        )
    with pytest.raises(ValueError, match="text must be a string"):
        EmbeddingRequest(
            "request",
            EmbeddingRole.QUERY,
            fingerprint,
            text=invalid_string,
        )

    hit = SearchHit(
        ref_id=1,
        entity_id="entity",
        item_id="item",
        indexed_model_signature="indexed",
        vector_space="space",
        modality=EmbeddingModality.TEXT,
        score=0.5,
        generation_id=1,
        query_model_signature="query",
    )
    with pytest.raises(ValueError, match="query_model_signature must be a string"):
        replace(hit, query_model_signature=invalid_string)
    with pytest.raises(ValueError, match="source_status must be a string"):
        ResolvedSearchHit(
            hit=hit,
            path=None,
            source_kind="pdf",
            source_identity="identity",
            section_kind=None,
            section_id=None,
            start_char=None,
            end_char=None,
            snippet=None,
            source_status=invalid_string,
        )
    with pytest.raises(ValueError, match="query_model_signature must be a string"):
        FusionEvidence(
            "ranking",
            1,
            0.5,
            0.5,
            "entity",
            "indexed",
            invalid_string,
        )


def test_model_contract_separates_modality_from_vector_space() -> None:
    text = EmbeddingModelSpec(
        "clip-text-v1",
        "clip-vit-b32-v1",
        EmbeddingModality.TEXT,
        "clip-text",
        "1",
        512,
        "fastembed",
        (EmbeddingRole.QUERY, EmbeddingRole.PASSAGE),
    )
    image = EmbeddingModelSpec(
        "clip-image-v1",
        "clip-vit-b32-v1",
        EmbeddingModality.IMAGE,
        "clip-image",
        "1",
        512,
        "fastembed",
        (EmbeddingRole.IMAGE,),
    )
    assert text.vector_space == image.vector_space
    assert text.modality is not image.modality
    with pytest.raises(ValueError, match="incompatible with modality"):
        EmbeddingModelSpec(
            "bad",
            "bad-space",
            EmbeddingModality.IMAGE,
            "bad",
            "1",
            8,
            "test",
            (EmbeddingRole.QUERY,),
        )


def test_embedding_request_requires_exact_payload_and_matching_text_identity() -> None:
    text = "breaker maintenance"
    request = EmbeddingRequest(
        "q1",
        EmbeddingRole.QUERY,
        fingerprint_text(text),
        text=text,
        source_revision={"source": "fixture"},
    )
    assert request.text == text
    with pytest.raises(ValueError, match="exactly one"):
        EmbeddingRequest(
            "bad",
            EmbeddingRole.QUERY,
            fingerprint_text(text),
            text=text,
            image_bytes=text.encode(),
        )


def test_uncalibrated_evidence_cannot_be_confirmed_without_feedback() -> None:
    advisory = SemanticEvidence(
        item_id="item",
        source_entity_id="chunk",
        ontology_id="industrial-electrical",
        ontology_version="1",
        concept_id="transformer",
        prototype_id="prototype",
        query_model_signature="query-model",
        indexed_model_signature="passage-model",
        vector_space="text-space",
        score=0.75,
        rank=1,
    )
    assert advisory.calibration_status is CalibrationStatus.UNCALIBRATED
    assert advisory.disposition is EvidenceDisposition.ADVISORY
    with pytest.raises(ValueError, match="must remain advisory"):
        replace(advisory, disposition=EvidenceDisposition.CONFIRMED)
    with pytest.raises(ValueError, match="feedback_reference must be a string"):
        replace(
            advisory,
            calibration_status=CalibrationStatus.CALIBRATED,
            disposition=EvidenceDisposition.CONFIRMED,
            feedback_reference=cast(str, 7),
        )
    calibrated = replace(
        advisory,
        calibration_status=CalibrationStatus.CALIBRATED,
        disposition=EvidenceDisposition.CONFIRMED,
        feedback_reference="review-decision:42",
    )
    assert calibrated.feedback_reference == "review-decision:42"


# endregion [03]
