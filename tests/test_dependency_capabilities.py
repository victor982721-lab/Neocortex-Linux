"""Canonical-install metadata and safe optional-capability boundaries."""


# region [01] Imports, metadata expectations and isolated harness

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import tomllib
from importlib import metadata
from pathlib import Path

from neocortex.capabilities import (
    CAPABILITY_SPECS,
    ROUTE_CAPABILITY_NAMES,
    CapabilityState,
    inspect_runtime_capabilities,
    inspect_runtime_capability,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]

BASE_DEPENDENCIES = (
    "complexipy>=6.2,<7",
    "cosmic-ray>=8.4.6,<8.5",
    "coverage>=7.14,<8",
    "deptry>=0.25,<0.26",
    "grimp>=3.15,<4",
    "mcp==1.23.3",
    "mypy>=2.1,<3",
    "packaging>=26,<27",
    "pip-audit>=2.10,<2.11",
    "pytest>=9.1,<10",
    "radon>=6.0.1,<7",
    "rich>=15,<16",
    "ruff>=0.15,<0.16",
    "semgrep>=1.172,<1.173",
    "vulture>=2.16,<2.17",
    "xxhash>=3.8,<4",
)

DEV_DEPENDENCIES = ("build>=1.5,<2",)

OPTIONAL_DEPENDENCIES = {
    "documents": (
        "Pillow>=12.3,<13",
        "PyMuPDF>=1.27,<2",
        "pdfminer.six>=20260107",
        "pytesseract>=0.3.13,<0.4",
    ),
    "audio": ("ctranslate2>=4.8,<5", "faster-whisper>=1.2,<2"),
    "image": (
        "nudenet>=3.4.2,<4",
        "Pillow>=12.3,<13",
    ),
    "semantic": (
        "fastembed==0.8.0",
        "numpy>=2.1,<3",
        "Pillow>=12.3,<13",
    ),
    "ui": ("PySide6>=6.11,<7",),
    "full": (
        "ctranslate2>=4.8,<5",
        "fastembed==0.8.0",
        "faster-whisper>=1.2,<2",
        "nudenet>=3.4.2,<4",
        "numpy>=2.1,<3",
        "Pillow>=12.3,<13",
        "PyMuPDF>=1.27,<2",
        "pdfminer.six>=20260107",
        "PySide6>=6.11,<7",
        "pytesseract>=0.3.13,<0.4",
    ),
}


def _run_isolated(
    script: str,
    **environment_values: str,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment.update(environment_values)
    return subprocess.run(
        [sys.executable, "-B", "-c", textwrap.dedent(script)],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


# endregion [01]


# region [02] Package dependency contract


def test_project_metadata_separates_canonical_runtime_and_extras() -> None:
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as stream:
        metadata_document = tomllib.load(stream)
    project = metadata_document["project"]

    assert tuple(project["dependencies"]) == BASE_DEPENDENCIES
    extras = project["optional-dependencies"]
    assert tuple(extras) == (*OPTIONAL_DEPENDENCIES, "dev")
    for name, expected in OPTIONAL_DEPENDENCIES.items():
        assert tuple(extras[name]) == expected
    assert tuple(extras["dev"]) == DEV_DEPENDENCIES
    assert {"coverage>=7.14,<8", "pytest>=9.1,<10"} <= set(project["dependencies"])
    assert {"coverage>=7.14,<8", "pytest>=9.1,<10"}.isdisjoint(extras["dev"])

    full_union = {
        dependency
        for name in ("documents", "audio", "image", "semantic", "ui")
        for dependency in extras[name]
    }
    assert set(extras["full"]) == full_union
    assert "Pillow>=12.3,<13" in extras["documents"]
    assert "Pillow>=12.3,<13" in extras["image"]
    assert "Pillow>=12.3,<13" in extras["semantic"]
    package_data = metadata_document["tool"]["setuptools"]["package-data"]
    assert package_data["neocortex"] == ["py.typed"]
    assert package_data["_04_Nucleo_Operativo"] == [
        "py.typed",
        "semgrep_rules/*.yml",
    ]
    assert (PROJECT_ROOT / "neocortex" / "py.typed").is_file()
    assert (PROJECT_ROOT / "_04_Nucleo_Operativo" / "py.typed").is_file()


# endregion [02]


# region [03] Static capability and degradation declarations


def _version_reader(available_modules: set[str]):
    versions = {
        requirement.distribution: "fixture-version"
        for spec in CAPABILITY_SPECS.values()
        for requirement in spec.requirements
        if requirement.distribution is not None and requirement.module in available_modules
    }

    def read(distribution: str) -> str:
        try:
            return versions[distribution]
        except KeyError as exc:
            raise metadata.PackageNotFoundError(distribution) from exc

    return read


def _inspect_with(
    available_modules: set[str],
    available_executables: set[str] | None = None,
):
    executables = set() if available_executables is None else available_executables
    return inspect_runtime_capabilities(
        module_finder=lambda name: object() if name in available_modules else None,
        distribution_version=_version_reader(available_modules),
        executable_finder=(lambda name: f"C:/fixture/{name}.exe" if name in executables else None),
    )


def test_every_builtin_route_has_one_static_capability_declaration() -> None:
    from _04_Nucleo_Operativo.route_selection import BUILTIN_ROUTE_ORDER

    assert ROUTE_CAPABILITY_NAMES == BUILTIN_ROUTE_ORDER
    assert all(name in CAPABILITY_SPECS for name in ROUTE_CAPABILITY_NAMES)


def test_missing_optional_runtimes_are_explicitly_unavailable_or_degraded() -> None:
    statuses = {status.capability: status for status in _inspect_with({"rich", "xxhash"})}

    assert statuses["docx"].state is CapabilityState.AVAILABLE
    assert statuses["office"].state is CapabilityState.AVAILABLE
    assert statuses["code"].state is CapabilityState.DEGRADED
    assert statuses["code"].degradation_reasons == (
        "code_ruff_provider_unavailable",
        "code_mypy_provider_unavailable",
        "code_vulture_provider_unavailable",
        "code_grimp_provider_unavailable",
        "code_complexipy_provider_unavailable",
        "code_pyright_node_unavailable",
        "code_pyright_provider_unavailable",
    )
    assert statuses["pdf"].state is CapabilityState.UNAVAILABLE
    assert "pdf_extractor_unavailable" in statuses["pdf"].degradation_reasons
    assert statuses["audio"].state is CapabilityState.UNAVAILABLE
    assert "audio_backend_unavailable" in statuses["audio"].degradation_reasons
    assert statuses["image"].state is CapabilityState.UNAVAILABLE
    assert "image_decode_unavailable" in statuses["image"].degradation_reasons
    assert statuses["semantic"].state is CapabilityState.UNAVAILABLE
    assert statuses["ui"].state is CapabilityState.UNAVAILABLE


def test_optional_route_components_produce_stable_degradation_reasons() -> None:
    pdf = inspect_runtime_capability(
        "pdf",
        module_finder=(lambda name: object() if name in {"rich", "xxhash", "fitz"} else None),
        distribution_version=_version_reader({"rich", "xxhash", "fitz"}),
        executable_finder=lambda _name: None,
    )
    assert pdf.state is CapabilityState.DEGRADED
    assert pdf.degradation_reasons == (
        "pdf_fallback_unavailable",
        "pdf_ocr_image_runtime_unavailable",
        "pdf_ocr_adapter_unavailable",
        "pdf_ocr_executable_unavailable",
        "pdf_recovery_unavailable",
    )

    image = inspect_runtime_capability(
        "image",
        module_finder=(lambda name: object() if name in {"rich", "xxhash", "PIL"} else None),
        distribution_version=_version_reader({"rich", "xxhash", "PIL"}),
        executable_finder=lambda _name: None,
    )
    assert image.state is CapabilityState.DEGRADED
    assert image.degradation_reasons == (
        "image_adult_classifier_unavailable",
        "image_document_ocr_unavailable",
    )
    assert image.to_dict()["models_loaded"] is False
    assert image.to_dict()["models_downloaded"] is False


def test_all_declared_components_serialize_as_available_when_present() -> None:
    modules = {
        requirement.module
        for spec in CAPABILITY_SPECS.values()
        for requirement in spec.requirements
        if requirement.module is not None
    }
    executables = {
        requirement.executable
        for spec in CAPABILITY_SPECS.values()
        for requirement in spec.requirements
        if requirement.executable is not None
    }

    statuses = _inspect_with(modules, executables)
    assert all(status.state is CapabilityState.AVAILABLE for status in statuses)
    for status in statuses:
        payload = status.to_dict()
        assert payload["schema_version"] == 1
        assert payload["probe_policy"] == "metadata-spec-path-only-v1"
        assert payload["capability"] == status.capability
        assert payload["degradation_reasons"] == []
        assert all(component["available"] for component in payload["components"])


# endregion [03]


# region [04] Base-only import and Knowledge behavior


def test_semantic_facades_cold_import_no_owner_or_image_runtime() -> None:
    completed = _run_isolated(
        """
        import importlib.abc
        import sys

        blocked_modules = {
            "_04_Nucleo_Operativo.audio_state",
            "_04_Nucleo_Operativo.code_schema",
            "_04_Nucleo_Operativo.docx_schema",
            "_04_Nucleo_Operativo.image_state",
            "_04_Nucleo_Operativo.office_state",
            "_04_Nucleo_Operativo.pdf_schema",
        }

        class OwnerImportBlocker(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                del path, target
                if fullname in blocked_modules or fullname.partition(".")[0] == "PIL":
                    raise ModuleNotFoundError(
                        f"blocked cold-import dependency: {fullname}",
                        name=fullname,
                    )
                return None

        sys.meta_path.insert(0, OwnerImportBlocker())

        import _04_Nucleo_Operativo.semantic_planner
        import _04_Nucleo_Operativo.semantic_service

        loaded = sorted(blocked_modules.intersection(sys.modules))
        loaded_pillow = sorted(
            name for name in sys.modules if name == "PIL" or name.startswith("PIL.")
        )
        if loaded or loaded_pillow:
            raise SystemExit(
                "semantic cold import loaded owners: "
                + ",".join((*loaded, *loaded_pillow))
            )
        print("SEMANTIC_COLD_IMPORT_OK")
        """
    )

    assert completed.returncode == 0, completed.stderr
    assert "SEMANTIC_COLD_IMPORT_OK" in completed.stdout


def test_base_surfaces_and_absent_knowledge_state_ignore_optional_engines(
    tmp_path: Path,
) -> None:
    state_directory = tmp_path / "missing-state"
    completed = _run_isolated(
        """
        import contextlib
        import importlib.abc
        import io
        import os
        import sys
        from pathlib import Path

        blocked_roots = {
            "PIL",
            "PySide6",
            "ctranslate2",
            "cv2",
            "fastembed",
            "faster_whisper",
            "fitz",
            "nudenet",
            "numpy",
            "onnxruntime",
            "pdfminer",
            "pytesseract",
        }

        class OptionalEngineBlocker(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                del path, target
                if fullname.partition(".")[0] in blocked_roots:
                    raise ModuleNotFoundError(
                        f"blocked optional engine: {fullname}",
                        name=fullname,
                    )
                return None

        sys.meta_path.insert(0, OptionalEngineBlocker())

        from neocortex.capabilities import (
            ROUTE_CAPABILITY_NAMES,
            inspect_runtime_capabilities,
        )

        capability_statuses = inspect_runtime_capabilities()
        declared_routes = tuple(
            status.capability
            for status in capability_statuses
            if status.capability in ROUTE_CAPABILITY_NAMES
        )
        if declared_routes != ROUTE_CAPABILITY_NAMES:
            raise SystemExit(f"route capability declarations changed: {declared_routes!r}")
        if any(
            status.to_dict()["models_loaded"]
            or status.to_dict()["models_downloaded"]
            for status in capability_statuses
        ):
            raise SystemExit("lightweight capability probe touched models")

        from neocortex.cli import entrypoint

        for option in ("--version", "--help"):
            output = io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                try:
                    entrypoint((option,))
                except SystemExit as exc:
                    if exc.code != 0:
                        raise
                else:
                    raise SystemExit(f"{option} did not terminate through argparse")

        from neocortex.sdk import (
            KnowledgeQuery,
            KnowledgeSearchService,
            KnowledgeStatePaths,
        )

        state_directory = Path(os.environ["NEOCORTEX_TEST_STATE"])
        if state_directory.exists():
            raise SystemExit("base probe state unexpectedly exists")
        service = KnowledgeSearchService(
            KnowledgeStatePaths.from_directory(state_directory)
        )
        snapshot = service.status()
        expected_owners = (
            "audio",
            "catalog",
            "code",
            "docx",
            "framework",
            "image",
            "inventory",
            "office",
            "pdf",
            "semantic",
        )
        observed_owners = tuple(owner.owner for owner in snapshot.owners)
        if observed_owners != expected_owners or {
            owner.state.value for owner in snapshot.owners
        } != {"absent"}:
            raise SystemExit(
                f"base status did not preserve absent owners: {observed_owners!r}"
            )
        query = KnowledgeQuery("relay protection")
        result = service.search(query)
        if result.complete or result.hits:
            raise SystemExit("absent search did not return explicit partial emptiness")
        service.context(query)
        if state_directory.exists():
            raise SystemExit("base Knowledge calls created state")

        loaded_optional = sorted(
            name
            for name in sys.modules
            if any(
                name == root or name.startswith(root + ".")
                for root in blocked_roots
            )
        )
        if loaded_optional:
            raise SystemExit("base surfaces loaded engines: " + ",".join(loaded_optional))
        print("BASE_OPTIONAL_ISOLATION_OK")
        """,
        NEOCORTEX_TEST_STATE=str(state_directory),
    )

    assert completed.returncode == 0, completed.stderr
    assert "BASE_OPTIONAL_ISOLATION_OK" in completed.stdout
    assert not state_directory.exists()


def test_base_knowledge_reads_existing_image_state_without_pillow(
    tmp_path: Path,
) -> None:
    from _04_Nucleo_Operativo.image_state import initialize_image_state

    state_directory = tmp_path / "state"
    state_directory.mkdir()
    initialize_image_state(state_directory / "image.sqlite3")

    completed = _run_isolated(
        """
        import importlib.abc
        import os
        import sys
        from pathlib import Path

        blocked_roots = {
            "PIL",
            "PySide6",
            "ctranslate2",
            "cv2",
            "fastembed",
            "faster_whisper",
            "fitz",
            "nudenet",
            "numpy",
            "onnxruntime",
            "pdfminer",
            "pytesseract",
        }

        class OptionalEngineBlocker(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                del path, target
                if fullname.partition(".")[0] in blocked_roots:
                    raise ModuleNotFoundError(
                        f"blocked optional engine: {fullname}",
                        name=fullname,
                    )
                return None

        sys.meta_path.insert(0, OptionalEngineBlocker())

        from neocortex.sdk import (
            KnowledgeQuery,
            KnowledgeSearchService,
            KnowledgeStatePaths,
        )

        paths = KnowledgeStatePaths.from_directory(
            Path(os.environ["NEOCORTEX_TEST_STATE"])
        )
        service = KnowledgeSearchService(paths)
        snapshot = service.status()
        image_owner = next(owner for owner in snapshot.owners if owner.owner == "image")
        if image_owner.state.value != "available":
            raise SystemExit(f"image owner was not available: {image_owner.state.value}")

        query = KnowledgeQuery("relay protection")
        service.search(query)
        service.context(query)

        loaded_optional = sorted(
            name
            for name in sys.modules
            if any(
                name == root or name.startswith(root + ".")
                for root in blocked_roots
            )
        )
        if loaded_optional:
            raise SystemExit(
                "existing image state loaded engines: " + ",".join(loaded_optional)
            )
        if "_04_Nucleo_Operativo.image_document" in sys.modules:
            raise SystemExit("image state loaded the Pillow-backed OCR module")
        print("BASE_EXISTING_IMAGE_STATE_OK")
        """,
        NEOCORTEX_TEST_STATE=str(state_directory),
    )

    assert completed.returncode == 0, completed.stderr
    assert "BASE_EXISTING_IMAGE_STATE_OK" in completed.stdout


# endregion [04]
