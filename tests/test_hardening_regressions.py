# region [00] Contexto del módulo
# Módulo: tests/test_hardening_regressions.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]
# region [01] Dependencias del módulo
from __future__ import annotations


import os
from pathlib import Path
from unittest.mock import patch

import pytest

from neocortex.workflow.actions import action_policy
from neocortex.workflow.actions.action_policy import validate_mutation_path
from neocortex.safety.corpus_access import (
    CorpusAccessPolicy,
    CorpusMutationGuard,
)
from neocortex.documents.document_organization import _create_destination_parent
from neocortex.documents.document_taxonomy import (
    MAX_TAXONOMY_BYTES,
    load_taxonomy,
)
from tests.internal_paths_test_support import disjoint_internal_paths_policy


TEST_CAPABILITIES = ("base", "documents", "ui")
# endregion [01]

# region [02] Implementación


@pytest.mark.capability("documents")
def test_qpdf_diagnostic_tail_read_is_bounded(tmp_path: Path) -> None:
    from neocortex.capabilities.formats.pdf.pdf_isolation import _read_file_tail

    diagnostics = tmp_path / "qpdf.stderr"
    diagnostics.write_bytes(b"prefix" * 20_000 + b"expected-tail")

    sample = _read_file_tail(diagnostics, 64)

    assert len(sample) == 64
    assert sample.endswith(b"expected-tail")


def test_taxonomy_file_size_is_bounded(tmp_path: Path) -> None:
    taxonomy_path = tmp_path / "taxonomy.toml"
    taxonomy_path.write_bytes(b" " * (MAX_TAXONOMY_BYTES + 1))

    with pytest.raises(ValueError, match="exceeds"):
        load_taxonomy(taxonomy_path)


def test_taxonomy_rejects_nested_repetition(tmp_path: Path) -> None:
    taxonomy_path = tmp_path / "taxonomy.toml"
    taxonomy_path.write_text(
        """
[[authorities]]
code = "UNSAFE"
aliases = ["UNSAFE"]
identifier_patterns = ["(a+)+$"]
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unsafe identifier pattern"):
        load_taxonomy(taxonomy_path)


@pytest.mark.capability("ui")
def test_controller_discards_oversized_unterminated_line_and_resynchronizes() -> None:
    from neocortex.interface.application.controller import MAX_PROCESS_LINE_BYTES, WorkerController

    controller = WorkerController()
    emitted: list[str] = []
    controller.output_received.connect(emitted.append)

    controller._ingest_output(
        controller._stdout_buffer,
        b"x" * (MAX_PROCESS_LINE_BYTES + 1),
        protocol=True,
    )

    assert not controller._stdout_buffer
    assert controller._stdout_discarding_oversized_line
    controller._ingest_output(
        controller._stdout_buffer,
        b"discarded suffix\nvisible output\n",
        protocol=True,
    )
    assert not controller._stdout_discarding_oversized_line
    assert any("exceder el límite" in message for message in emitted)
    assert emitted[-1] == "visible output"


def test_mutation_policy_allows_only_a_missing_destination_suffix(
    tmp_path: Path,
) -> None:
    root = tmp_path / "organized"
    root.mkdir()
    destination = root / "one" / "two" / "document.pdf"

    result = validate_mutation_path(
        root,
        destination,
        role="organization destination",
        allow_missing_tail=True,
    )

    assert result is None


def test_destination_parent_creation_rejects_existing_reparse_component(
    tmp_path: Path,
) -> None:
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    source = tmp_path / "source.pdf"
    source.write_bytes(b"source")
    root = tmp_path / "organized"
    unsafe_parent = root / "unsafe"
    unsafe_parent.mkdir(parents=True)
    destination = unsafe_parent / "nested" / "document.pdf"
    unsafe_key = os.path.normcase(os.path.abspath(unsafe_parent))
    real_check = action_policy._is_reparse_entry

    def simulated_reparse(path: Path, entry_stat: os.stat_result) -> bool:
        return os.path.normcase(os.path.abspath(path)) == unsafe_key or real_check(path, entry_stat)

    with patch.object(
        action_policy,
        "_is_reparse_entry",
        side_effect=simulated_reparse,
    ):
        with pytest.raises(ValueError, match="reparse point"):
            _create_destination_parent(
                state_directory,
                source,
                root,
                destination,
                os.stat(root, follow_symlinks=False),
                CorpusMutationGuard(
                    CorpusAccessPolicy.capture("normal", root),
                    disjoint_internal_paths_policy(tmp_path),
                ),
            )

    assert not (unsafe_parent / "nested").exists()


@pytest.mark.parametrize(
    ("pattern", "expected"),
    (
        (r"^[A-Z]{2}-\d{4}$", None),
        (r"(?:AB)+-\d+$", None),
        (r"(AB|CD)-\d+$", None),
        (r"(AB){2,3}?$", None),
        (r"\(AB\+\)", None),
        (
            r"(a+)+$",
            "a repeated group cannot itself contain repetition or alternation",
        ),
        (
            r"(a|b)+$",
            "a repeated group cannot itself contain repetition or alternation",
        ),
        (
            r"(?:a*){2}$",
            "a repeated group cannot itself contain repetition or alternation",
        ),
        (
            r"((ab)+)?$",
            "a repeated group cannot itself contain repetition or alternation",
        ),
        (
            r"(a++)+$",
            "a repeated group cannot itself contain repetition or alternation",
        ),
        (r"(a)\1", "backreferences are not allowed"),
        (r"[\1]", "backreferences are not allowed"),
        (
            r"a(?=b)",
            "lookarounds, named groups, and inline extensions are not allowed",
        ),
        (
            r"(?P<name>a)",
            "lookarounds, named groups, and inline extensions are not allowed",
        ),
        (
            r"(?i:a)",
            "lookarounds, named groups, and inline extensions are not allowed",
        ),
    ),
)
def test_custom_regex_safety_characterization(
    pattern: str,
    expected: str | None,
) -> None:
    from neocortex.documents.document_taxonomy_overlay import (
        _unsafe_custom_regex_reason,
    )

    assert _unsafe_custom_regex_reason(pattern) == expected
    assert _unsafe_custom_regex_reason(pattern) == expected


def test_custom_regex_safety_reason_precedence_is_left_to_right() -> None:
    from neocortex.documents.document_taxonomy_overlay import (
        _unsafe_custom_regex_reason,
    )

    assert _unsafe_custom_regex_reason(r"(a)\1(?=b)") == ("backreferences are not allowed")
    assert _unsafe_custom_regex_reason(r"(?=b)(a)\1") == (
        "lookarounds, named groups, and inline extensions are not allowed"
    )


def test_custom_regex_safety_signature_is_frozen() -> None:
    from inspect import signature

    from neocortex.documents.document_taxonomy_overlay import (
        _unsafe_custom_regex_reason,
    )

    assert str(signature(_unsafe_custom_regex_reason)) == ("(pattern: 'str') -> 'str | None'")


# endregion [02]
