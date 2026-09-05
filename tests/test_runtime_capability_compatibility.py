"""Runtime presence never substitutes for declared compatibility or execution."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from importlib import metadata
from pathlib import Path

import pytest

from neocortex.capabilities import requirement_metadata
from neocortex.capabilities.runtime import (
    CapabilityState,
    RuntimeComponentState,
    inspect_python_component,
    inspect_runtime_capability,
)


@pytest.mark.parametrize(
    ("distribution", "module", "version", "compatible"),
    (
        ("PyMuPDF", "fitz", "1.26.7", False),
        ("PyMuPDF", "fitz", "1.27.0", True),
        ("pdfminer.six", "pdfminer", "20251230", False),
        ("pdfminer.six", "pdfminer", "20260107", True),
        ("Pillow", "PIL", "12.3.0", True),
        ("packaging", "packaging", "25.0", False),
    ),
)
def test_package_presence_is_compared_to_its_canonical_requirement(
    distribution: str, module: str, version: str, compatible: bool
) -> None:
    component = inspect_python_component(
        distribution,
        module,
        extra="documents",
        module_finder=lambda _name: object(),
        distribution_version=lambda _name: version,
    )
    assert component.available is compatible
    assert component.observation_state is (
        RuntimeComponentState.PRESENT_COMPATIBLE
        if compatible
        else RuntimeComponentState.PRESENT_INCOMPATIBLE
    )
    payload = component.to_dict()
    assert payload["distribution"] == distribution
    assert payload["observed_version"] == version
    assert payload["requirement"]
    assert payload["requirement_source"] == "source_pyproject"
    assert payload["functional_status"] == "not_checked"
    if not compatible:
        assert "distribution_version_incompatible" in str(payload["reason"])


def test_absence_remains_distinct_from_incompatibility() -> None:
    def absent(distribution: str) -> str:
        raise metadata.PackageNotFoundError(distribution)

    component = inspect_python_component(
        "PyMuPDF",
        "fitz",
        extra="documents",
        module_finder=lambda _name: None,
        distribution_version=absent,
    )
    assert component.observation_state is RuntimeComponentState.ABSENT
    assert component.available is False
    assert component.to_dict()["observed_version"] is None
    assert component.to_dict()["requirement"] is not None


@pytest.mark.parametrize("version", ("not-a-version", None))
def test_unusable_distribution_metadata_is_unverified(version: str | None) -> None:
    def read_version(distribution: str) -> str:
        if version is None:
            raise metadata.PackageNotFoundError(distribution)
        return version

    component = inspect_python_component(
        "PyMuPDF",
        "fitz",
        extra="documents",
        module_finder=lambda _name: object(),
        distribution_version=read_version,
    )
    assert not component.available
    assert component.observation_state is RuntimeComponentState.PRESENT_UNVERIFIED


def test_metadata_without_module_is_not_an_available_backend() -> None:
    component = inspect_python_component(
        "PyMuPDF",
        "fitz",
        extra="documents",
        module_finder=lambda _name: None,
        distribution_version=lambda _name: "1.27.0",
    )
    assert not component.available
    assert component.observation_state is RuntimeComponentState.PRESENT_UNVERIFIED
    assert "module_spec_unavailable" in str(component.reason)


def _pdf_versions(*, pdf: str = "1.27.0", fallback: str = "20260107"):
    versions = {
        "packaging": "26.0",
        "rich": "15.0.0",
        "xxhash": "3.8.0",
        "PyMuPDF": pdf,
        "pdfminer.six": fallback,
        "Pillow": "12.3.0",
        "pytesseract": "0.3.13",
    }
    return versions.__getitem__


def test_required_incompatibility_blocks_only_its_capability() -> None:
    pdf = inspect_runtime_capability(
        "pdf",
        module_finder=lambda _name: object(),
        distribution_version=_pdf_versions(pdf="1.26.7"),
        executable_finder=lambda name: f"/fixture/{name}",
    )
    assert pdf.state is CapabilityState.UNAVAILABLE
    assert pdf.operational_state == "blocked_by_requirements"
    assert pdf.to_dict()["processing_status"] == "not_checked"
    assert any(
        "pdf_extractor_unavailable:distribution_version_incompatible" == reason
        for reason in pdf.degradation_reasons
    )
    text = inspect_runtime_capability(
        "text",
        module_finder=lambda _name: object(),
        distribution_version=_pdf_versions(pdf="1.26.7"),
        executable_finder=lambda name: f"/fixture/{name}",
    )
    assert text.state is CapabilityState.AVAILABLE


def test_optional_incompatibility_is_degradation_not_absence() -> None:
    pdf = inspect_runtime_capability(
        "pdf",
        module_finder=lambda _name: object(),
        distribution_version=_pdf_versions(fallback="20251230"),
        executable_finder=lambda name: f"/fixture/{name}",
    )
    assert pdf.state is CapabilityState.DEGRADED
    assert pdf.degradation_reasons == (
        "pdf_fallback_unavailable:distribution_version_incompatible",
    )


def test_executable_path_is_not_a_functional_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbid_execution(*_args, **_kwargs):
        raise AssertionError("metadata-only capability probe executed a binary")

    monkeypatch.setattr(subprocess, "run", forbid_execution)
    status = inspect_runtime_capability(
        "video",
        module_finder=lambda _name: object(),
        distribution_version=_pdf_versions(),
        executable_finder=lambda name: f"/not/executed/{name}",
    )
    ffmpeg = next(item for item in status.components if item.requirement.component == "ffmpeg")
    assert ffmpeg.available
    assert ffmpeg.observation_state is RuntimeComponentState.EXECUTABLE_LOCATED
    assert ffmpeg.to_dict()["functional_status"] == "not_checked"


def test_configuration_and_processing_failure_are_not_requirement_absence() -> None:
    status = inspect_runtime_capability(
        "docx",
        module_finder=lambda _name: object(),
        distribution_version=_pdf_versions(),
        executable_finder=lambda _name: None,
    )
    assert status.state is CapabilityState.AVAILABLE
    assert status.operational_state == "not_checked"
    assert replace(status, enabled=False).operational_state == "disabled"
    failed = replace(status, processing_error="document_parse_failed")
    assert failed.state is CapabilityState.AVAILABLE
    assert failed.operational_state == "failed"
    assert failed.to_dict()["processing_error"] == "document_parse_failed"


def test_backend_presence_does_not_assert_local_models_available() -> None:
    status = inspect_runtime_capability(
        "semantic",
        module_finder=lambda _name: object(),
        distribution_version={
            "packaging": "26.0",
            "rich": "15.0.0",
            "xxhash": "3.8.0",
            "fastembed": "0.8.0",
            "numpy": "2.3.5",
            "Pillow": "12.3.0",
        }.__getitem__,
        executable_finder=lambda _name: None,
    )
    assert status.state is CapabilityState.AVAILABLE
    assert status.to_dict()["model_status"] == "not_checked"
    assert status.to_dict()["processing_status"] == "not_checked"


def test_transitive_requirement_uses_owning_distribution_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        metadata,
        "requires",
        lambda name: (
            ['ONNX_Runtime>=1.20,<2; python_version >= "3.13"', 'ONNX_Runtime>=2; extra == "cuda"']
            if name == "fastembed"
            else []
        ),
    )
    component = inspect_python_component(
        "onnxruntime",
        "onnxruntime",
        owner_distribution="fastembed",
        module_finder=lambda _name: object(),
        distribution_version=lambda _name: "1.21.0",
    )
    # ONNX_Runtime normalizes to onnx-runtime, not onnxruntime: do not conflate
    # distinct distribution names merely because their imports look similar.
    assert not component.available
    assert "requirement_not_declared" in str(component.reason)
    actual = inspect_python_component(
        "onnx-runtime",
        "onnxruntime",
        owner_distribution="fastembed",
        module_finder=lambda _name: object(),
        distribution_version=lambda _name: "1.21.0",
    )
    assert actual.available
    assert actual.requirement_source == "installed_metadata"
    assert ">=2" not in str(actual.applicable_requirement)


def test_dotted_module_probes_do_not_import_the_parent() -> None:
    seen = []

    def find_module(name: str):
        seen.append(name)
        return object()

    component = inspect_python_component(
        "PyMuPDF",
        "fitz.native_extension",
        extra="documents",
        module_finder=find_module,
        distribution_version=lambda _name: "1.27.0",
    )
    assert component.available
    assert seen == ["fitz"]


def test_missing_requirement_metadata_is_not_compatibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_metadata(**_kwargs):
        raise metadata.PackageNotFoundError("neocortex-framework")

    monkeypatch.setattr(requirement_metadata, "_declarations", no_metadata)
    component = inspect_python_component(
        "PyMuPDF",
        "fitz",
        extra="documents",
        module_finder=lambda _name: object(),
        distribution_version=lambda _name: "1.27.0",
    )
    assert not component.available
    assert "requirement_metadata_unavailable" in str(component.reason)


def test_requirement_source_is_independent_of_working_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="neocortex-framework"\ndependencies=["packaging>=999"]\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    component = inspect_python_component(
        "packaging",
        "packaging",
        module_finder=lambda _name: object(),
        distribution_version=lambda _name: "26.0",
    )
    assert component.available
    assert "999" not in str(component.applicable_requirement)


def test_human_diagnostic_shows_the_actual_requirement_and_failure(capsys) -> None:
    from neocortex.api.cli.cli_capabilities import _print_human

    status = inspect_runtime_capability(
        "pdf",
        module_finder=lambda _name: object(),
        distribution_version=_pdf_versions(pdf="1.26.7"),
        executable_finder=lambda _name: None,
    )
    _print_human((status,))
    output = capsys.readouterr().out
    line = next(line for line in output.splitlines() if "component=pymupdf " in line)
    assert 'version="1.26.7"' in line
    assert 'distribution="PyMuPDF"' in line
    assert 'requirement="PyMuPDF' in line
    assert "distribution_version_incompatible" in line
    assert "status=present_incompatible" in line
    assert "functional_status=not_checked" in line


def test_missing_packaging_can_be_diagnosed_without_import_failure() -> None:
    source_root = str(Path(__file__).resolve().parents[1])
    script = f"""
import importlib.abc, json, sys
sys.path.insert(0, {source_root!r})
class MissingPackaging(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.partition(".")[0] == "packaging":
            raise ModuleNotFoundError("packaging is intentionally absent")
        return None
sys.meta_path.insert(0, MissingPackaging())
from neocortex.capabilities.runtime import inspect_python_component
result = inspect_python_component(
    "PyMuPDF", "fitz", extra="documents", module_finder=lambda _name: object(),
    distribution_version=lambda _name: "1.27.0",
)
assert not result.available
print(json.dumps(result.to_dict()))
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", script],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "requirement_parser_unavailable:packaging" in json.loads(result.stdout)["reason"]


def test_diagnostic_imports_are_measured_against_the_interpreter_baseline() -> None:
    source_root = str(Path(__file__).resolve().parents[1])
    script = f"""
import json, sys
sys.path.insert(0, {source_root!r})
before = set(sys.modules)
from neocortex.capabilities.runtime import inspect_runtime_capabilities
statuses = inspect_runtime_capabilities()
introduced = set(sys.modules) - before
engines = {{"fitz", "pymupdf", "pdfminer", "PIL", "numpy", "xxhash", "fastembed",
           "onnxruntime", "faster_whisper", "ctranslate2", "PySide6"}}
assert not {{name.partition(".")[0] for name in introduced}} & engines
print(json.dumps({{"capabilities": len(statuses), "native_imports": 0}}))
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", script],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["native_imports"] == 0
