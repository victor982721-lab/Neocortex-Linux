"""Public consultation and help do not load independent processing engines."""

from __future__ import annotations

import json
import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_PROCESSING_DEPENDENCIES = (
    "PIL",
    "numpy",
    "PySide6",
    "fastembed",
    "onnxruntime",
    "ctranslate2",
    "faster_whisper",
    "torch",
    "pymupdf",
    "fitz",
    "pdfminer",
    "pytesseract",
    "mcp",
)


def _probe(
    arguments: tuple[str, ...],
    tmp_path: Path,
    *,
    no_site_packages: bool = False,
    blocked: tuple[str, ...] = _PROCESSING_DEPENDENCIES,
) -> dict[str, object]:
    # Record pre-existing imports instead of blaming NeoCortex for modules
    # preloaded by an embedding host, sitecustomize or a test plugin.
    script = f"""
import contextlib
import importlib.abc
import io
import json
import sys
sys.path.insert(0, {str(REPOSITORY_ROOT)!r})
blocked = {blocked!r}
attempted = []
class IndependentDependencies(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == prefix or fullname.startswith(prefix + '.') for prefix in blocked):
            attempted.append(fullname)
            raise ModuleNotFoundError('test dependency unavailable: ' + fullname, name=fullname)
sys.meta_path.insert(0, IndependentDependencies())
before = frozenset(sys.modules)
out, err = io.StringIO(), io.StringIO()
with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
    from neocortex.interface.entrypoint import entrypoint
    code = entrypoint({arguments!r})
introduced = sorted(set(sys.modules) - before)
print(json.dumps(dict(code=code, stdout=out.getvalue(), stderr=err.getvalue(),
                     introduced=introduced, attempted=attempted)))
"""
    command = [sys.executable, "-I", "-B"]
    if no_site_packages:
        command.append("-S")
    completed = subprocess.run(
        [*command, "-c", script],
        cwd=tmp_path,
        env={
            **os.environ,
            "HOME": str(tmp_path / "home"),
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "XDG_CACHE_HOME": str(tmp_path / "cache"),
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


@pytest.mark.parametrize(
    "arguments",
    (
        ("--help",),
        ("--version",),
        ("help",),
        ("status", "--help"),
        ("--ui", "--help"),
        ("models", "status", "--help"),
        ("doctor", "capabilities", "--help"),
    ),
)
def test_public_help_works_without_any_site_packages(
    arguments: tuple[str, ...], tmp_path: Path
) -> None:
    probe = _probe(
        arguments,
        tmp_path,
        no_site_packages=True,
        blocked=(*_PROCESSING_DEPENDENCIES, "xxhash", "rich", "packaging"),
    )

    assert probe["code"] == 0, probe["stderr"]
    assert probe["stdout"]
    assert probe["stderr"] == ""
    assert probe["attempted"] == []
    assert "neocortex.capabilities.formats.video.frames" not in probe["introduced"]
    assert "neocortex.interface.application.app" not in probe["introduced"]
    assert "neocortex.knowledge.knowledge_service" not in probe["introduced"]
    assert not (tmp_path / "state").exists()


def test_status_requires_no_optional_processing_dependency(tmp_path: Path) -> None:
    probe = _probe(("status", "--scope", "all", "--json"), tmp_path)

    assert probe["code"] == 3, probe["stderr"]
    payload = json.loads(str(probe["stdout"]))
    assert payload["status"] == "empty"
    assert payload["read_only"] is True
    assert probe["attempted"] == []
    assert probe["stderr"] == ""
    assert not (tmp_path / "state").exists()


def test_published_text_owner_status_needs_no_processing_engine(tmp_path: Path) -> None:
    from neocortex.capabilities.formats.text.text_state import initialize_text_state

    state = tmp_path / "state" / "Neocortex" / "state"
    state.mkdir(parents=True)
    initialize_text_state(state / "text.sqlite3")
    before = {
        path.relative_to(state): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in state.rglob("*") if path.is_file()
    }

    first = _probe(("status", "--scope", "all", "--json"), tmp_path)
    second = _probe(("status", "--scope", "all", "--json"), tmp_path)
    payload = json.loads(str(first["stdout"]))
    replay = json.loads(str(second["stdout"]))
    snapshot = payload["scopes"][0]["snapshot"]

    assert first["code"] == second["code"] == 0
    assert payload["read_only"] is True
    assert first["attempted"] == second["attempted"] == []
    assert snapshot["snapshot_id"] == replay["scopes"][0]["snapshot"]["snapshot_id"]
    assert any(
        owner["owner"] == "text" and owner["state"] == "available"
        for owner in snapshot["owners"]
    )
    assert {
        path.relative_to(state): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in state.rglob("*") if path.is_file()
    } == before


def test_missing_base_dependency_is_reported_in_status_envelope(tmp_path: Path) -> None:
    probe = _probe(
        ("status", "--scope", "all", "--json"), tmp_path, no_site_packages=True
    )

    assert probe["code"] == 1
    payload = json.loads(str(probe["stdout"]))
    assert payload["read_only"] is True
    assert payload["coverage"] == "unavailable"
    assert all("xxhash" in item["reason"] for item in payload["scopes"])
    assert probe["stderr"] == ""
    assert not (tmp_path / "state").exists()


def test_desktop_without_qt_names_the_blocked_operation(tmp_path: Path) -> None:
    probe = _probe(("--ui",), tmp_path, no_site_packages=True)

    assert probe["code"] == 1
    assert "PySide6" in str(probe["stderr"])
    assert "--ui" in str(probe["stderr"])
    assert "Traceback" not in str(probe["stderr"])
    assert not (tmp_path / "state").exists()


def test_desktop_parser_accepts_explicit_isolated_state_without_qt(tmp_path: Path) -> None:
    from neocortex.interface.application.arguments import parse_arguments

    parsed = parse_arguments(
        ["--root", str(tmp_path / "corpus"), "--state-directory", str(tmp_path / "state")]
    )

    assert parsed.root == tmp_path / "corpus"
    assert parsed.state_directory == tmp_path / "state"
    assert not parsed.state_directory.exists()


def test_model_resource_arguments_preserve_an_explicit_selection(tmp_path: Path) -> None:
    from neocortex.api.cli.cli_models_surface import validate_models_arguments
    from neocortex.api.cli.cli_parser import build_parser
    from neocortex.interface.entrypoint import _translate_canonical_arguments

    root = tmp_path / "local-models"
    identifiers = [
        "jinaai/jina-embeddings-v2-base-es",
        "Qdrant/clip-ViT-B-32-text",
    ]
    arguments = _translate_canonical_arguments(
        [
            "models", "status", "--models-root", str(root),
            "--models-model-id", identifiers[0], "--models-model-id", identifiers[1],
        ]
    )
    parsed = build_parser().parse_args(arguments)
    validate_models_arguments(parsed)

    assert parsed.models_root == root
    assert parsed.models_model_id == identifiers
    assert parsed.models_status is True
