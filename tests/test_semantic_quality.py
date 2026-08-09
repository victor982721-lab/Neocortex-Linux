from __future__ import annotations

import pytest

from _04_Nucleo_Operativo.semantic_chunking import TextChunkingConfig
from _04_Nucleo_Operativo.semantic_models import TextSection
from _04_Nucleo_Operativo.semantic_quality import (
    SEMANTIC_TEXT_QUALITY_POLICY,
    assess_semantic_text,
    iter_semantic_text_chunks,
)


def _chunking() -> TextChunkingConfig:
    return TextChunkingConfig(
        max_chars=2_048,
        max_terms=384,
        overlap_chars=0,
        overlap_terms=0,
        min_natural_break_chars=64,
    )


def test_quality_gate_rejects_encoded_binary_and_formula_dumps() -> None:
    encoded = (
        "q9oYOxNvmp6FqrfF2TK2oMv+7TEycm8KXCy+oM/q1wlpYno1Jtu77ymRNuNdEYR+"
        "MXP6K57Ny1qTtfa6chinZTvderjddSpomRNXZFrw8yNQqPZrH5BV0BCNGMPqdA=="
    )
    formula = (
        "$BB$1048558 D12/SQRT(3)/J12 AW1048563 $BB$1048558 "
        "D13/SQRT(3)/J13 AW1048564 $BB$1048558 D14/SQRT(3)/J14 "
        'IF(AY1048561="+",$AV$1048558+AZ1048561*$AZ$1048558)'
    )

    assert assess_semantic_text(encoded).reason == "encoded_binary_text"
    assert assess_semantic_text(formula).reason == "spreadsheet_formula_dump"
    assert (
        tuple(
            iter_semantic_text_chunks(
                "item:fixture:noise",
                (
                    TextSection("pdf_page", "1", encoded),
                    TextSection("xlsx_document", "body", formula),
                ),
                _chunking(),
            )
        )
        == ()
    )


def test_quality_gate_keeps_human_text_and_collapses_item_local_repeats() -> None:
    text = (
        "Bitácora diaria de trabajos de mantenimiento en la central hidroeléctrica. "
        "Se revisó el transformador de reserva y el interruptor principal."
    )
    chunks = tuple(
        iter_semantic_text_chunks(
            "item:fixture:report",
            (
                TextSection("pdf_page", "1", text),
                TextSection("pdf_page", "2", text),
            ),
            _chunking(),
        )
    )

    assert len(chunks) == 1
    assert chunks[0].text == text
    assert SEMANTIC_TEXT_QUALITY_POLICY == "semantic-text-quality-v1"


@pytest.mark.parametrize(
    ("text", "eligible"),
    (
        ("Informe de mantenimiento preventivo del transformador principal.", True),
        ("Se verificó la operación del relevador diferencial 87T y no hubo alarmas.", True),
        ("Breaker timing test completed successfully on all three phases.", True),
        ("Equipo, tensión nominal, corriente nominal y fecha de inspección.", True),
        ("Parámetro\tValor\nRetardo de disparo\t0.30 segundos", True),
        ("La cuadrilla montó el gabinete de control durante el turno nocturno.", True),
        ("Subject: Resultado de pruebas\nEl alimentador quedó disponible.", True),
        ("ASTM D1816 exige registrar la tensión de ruptura del aceite aislante.", True),
        ('{"equipo": "transformador", "estado": "vigente"}', True),
        ("<equipo><nombre>Interruptor de potencia</nombre></equipo>", True),
        ("2026-08-09 08:30 Inicio de maniobra; 09:15 cierre satisfactorio.", True),
        ("Lista de materiales: conductor de cobre, aislador y terminal bimetálica.", True),
        (
            "QWxhZGRpbjpvcGVuIHNlc2FtZSBxOW9ZT3hOdm1wNkZxcmZGMlRLMm9Ndit7VEV5Y204S1hDeStvTS9xMXdscFlubzFKdHU3N3ltUk51TmRFWVIr",
            False,
        ),
        ("$A$1 $B$2 $C$3 $D$4 $E$5 $F$6 $G$7 $H$8 IF($A$1=1,SUM($B$2:$H$8),0)", False),
        ("REF! " * 80, False),
        ("token" * 600, False),
        ("0000000000000000000000000000000000000000000000000000000000000000", False),
        ("Ã© Ã± Ã³ Ãº " * 30, False),
        ("x " * 100, False),
        ("$AA$1048576 " * 30 + "SQRT($AA$1048576) IF($AA$1048576=1,2,3)", False),
        ("+++ === --- /// 000 111 222 333 444 555 666 777 888 999", False),
        ("a", False),
        ("\x00\x01\x02\x03" * 30, False),
        ("Zm9vYmFyYmF6cXV4" * 40, False),
    ),
)
def test_quality_gate_on_24_representative_human_and_machine_fixtures(
    text: str,
    eligible: bool,
) -> None:
    assert assess_semantic_text(text).eligible is eligible
