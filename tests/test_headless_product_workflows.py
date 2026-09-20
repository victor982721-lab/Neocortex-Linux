"""Bounded workflows against the installed product, never an alternate implementation.

Use the pytest interpreter's installation, or set NEOCORTEX_TEST_PYTHON to the
absolute Python executable of another isolated installation. Every product
subprocess uses -I, an unrelated cwd, and a private HOME/XDG/state/corpus. An
explicit interpreter that is not installed fails rather than silently skipping.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

TEST_CAPABILITIES = ("base", "documents", "image", "platform")
_FIXTURES = Path(__file__).parent / "fixtures" / "headless_product"
_REPOSITORY = Path(__file__).parents[1]
_PROCESS_TIMEOUT = 120


def _hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix not in {".pyc", ".pyo"}
    }


def _assert_completed(result: subprocess.CompletedProcess[str], expected: int = 0) -> None:
    assert result.returncode == expected, (
        f"exit={result.returncode}; expected={expected}\n"
        f"stdout tail:\n{result.stdout[-1800:]}\nstderr tail:\n{result.stderr[-2400:]}"
    )


@dataclass
class _ProductLab:
    python: Path
    directory: Path
    environment: dict[str, str]
    invocation: int = 0

    @property
    def corpus(self) -> Path:
        return self.directory / "corpus"

    @property
    def state(self) -> Path:
        return self.directory / "state"

    def copy_group(self, group: str) -> dict[str, str]:
        expected = json.loads((_FIXTURES / "expected.json").read_text(encoding="utf-8"))
        source = _FIXTURES / group
        assert _hashes(source) == expected["groups"][group]["files"]
        shutil.copytree(
            source,
            self.corpus,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
        )
        return _hashes(self.corpus)

    def execute(
        self, arguments: list[str], *, console: bool = False
    ) -> subprocess.CompletedProcess[str]:
        self.invocation += 1
        if console:
            command = [str(self.python.parent / "Neocortex"), *arguments]
        else:
            command = [str(self.python), "-I", "-B", *arguments]
        result = subprocess.run(
            command,
            cwd=self.directory / "work",
            env=self.environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=_PROCESS_TIMEOUT,
            check=False,
        )
        for name, value in (("stdout", result.stdout), ("stderr", result.stderr)):
            (self.directory / f"command-{self.invocation:02d}.{name}").write_text(
                value, encoding="utf-8"
            )
        return result

    def cli(self, *arguments: str, console: bool = False) -> subprocess.CompletedProcess[str]:
        prefix = [] if console else ["-m", "neocortex"]
        return self.execute([*prefix, *arguments], console=console)

    def process(self, routes: str, *arguments: str) -> subprocess.CompletedProcess[str]:
        result = self.cli(
            "--root",
            str(self.corpus),
            "--state-directory",
            str(self.state),
            "--route",
            routes,
            "--no-document-catalog",
            "--strict-exit-codes",
            *arguments,
        )
        _assert_completed(result)
        return result

    def python_json(self, script: str, *arguments: str) -> Any:
        result = self.execute(["-c", script, *arguments])
        _assert_completed(result)
        return json.loads(result.stdout)

    def query(self, database: str, sql: str, parameters: tuple[Any, ...] = ()) -> list[list[Any]]:
        # The preceding subprocess is terminal. Read through the real sidecar-safe
        # owner kernel, not sqlite3.connect(mode=ro), even in these private fixtures.
        return self.python_json(
            "import json,sys\n"
            "from neocortex.persistence.sqlite_immutable import SQLiteReadSession\n"
            "with SQLiteReadSession(sys.argv[1],mode='snapshot_temp') as connection:\n"
            " rows=connection.execute(sys.argv[2],json.loads(sys.argv[3])).fetchall()\n"
            " print(json.dumps([list(row) for row in rows]))\n",
            str(self.state / database),
            sql,
            json.dumps(parameters),
        )

    def hide_system_tools(self, *visible: str) -> None:
        private_bin = self.directory / "bin"
        private_bin.mkdir(exist_ok=True)
        for name in visible:
            executable = shutil.which(name)
            if executable is None:
                pytest.skip(f"real executable required by this workflow: {name}")
            (private_bin / name).symlink_to(executable)
        self.environment["PATH"] = str(private_bin)


@pytest.fixture
def product_lab(tmp_path: Path) -> _ProductLab:
    explicit = os.environ.get("NEOCORTEX_TEST_PYTHON")
    # Do not resolve the venv Python symlink: its invocation path selects the venv.
    python = Path(os.path.abspath(explicit or sys.executable))
    environment = os.environ.copy()
    for key in ("PYTHONPATH", "PYTHONHOME", "DISPLAY", "WAYLAND_DISPLAY"):
        environment.pop(key, None)
    for key, name in (
        ("HOME", "home"),
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_CACHE_HOME", "cache"),
        ("XDG_DATA_HOME", "data"),
        ("XDG_STATE_HOME", "xdg-state"),
        ("TMPDIR", "tmp"),
    ):
        path = tmp_path / name
        path.mkdir()
        environment[key] = str(path)
    (tmp_path / "work").mkdir()
    environment.update(
        NEOCORTEX_PROGRESS_STREAM="1",
        HF_HUB_OFFLINE="1",
        TRANSFORMERS_OFFLINE="1",
        QT_QPA_PLATFORM="offscreen",
    )
    lab = _ProductLab(python, tmp_path, environment)
    probe = lab.execute(
        [
            "-c",
            "import json,neocortex,importlib.metadata as m; "
            "print(json.dumps({'path':neocortex.__file__,"
            "'version':m.version('neocortex-framework')}))",
        ]
    )
    if not explicit and os.environ.get("NEOCORTEX_REQUIRE_INSTALLED") == "1":
        pytest.fail(
            "installed-product acceptance requires NEOCORTEX_TEST_PYTHON to point to an isolated venv"
        )
    if not explicit and probe.returncode != 0:
        pytest.skip("requires an installed product; set NEOCORTEX_TEST_PYTHON to its isolated venv")
    _assert_completed(probe)
    package = Path(json.loads(probe.stdout)["path"])
    assert not package.is_relative_to(_REPOSITORY / "neocortex"), (
        "the installed workflow cannot use the source checkout"
    )
    assert "site-packages" in package.parts, (
        "requires the wheel installation, not an editable source"
    )
    return lab


def _fields(line: str) -> dict[str, str]:
    return dict(word.split("=", 1) for word in line.split() if "=" in word)


def _summary(result: subprocess.CompletedProcess[str], prefix: str) -> dict[str, str]:
    return _fields(next(line for line in result.stdout.splitlines() if line.startswith(prefix)))


def _progress(result: subprocess.CompletedProcess[str]) -> list[dict[str, Any]]:
    assert "NEOCORTEX_PROGRESS " not in result.stdout
    assert "\x1b[" not in result.stderr
    events = [
        json.loads(line.removeprefix("NEOCORTEX_PROGRESS "))
        for line in result.stderr.splitlines()
        if line.startswith("NEOCORTEX_PROGRESS ")
    ]
    assert events
    assert any(event["finished"] and event["phase"] == "complete" for event in events)
    return events


def _terminal_status(lab: _ProductLab, routes: set[str]) -> list[dict[str, Any]]:
    result = lab.cli("--state-directory", str(lab.state), "--status", "--status-json")
    _assert_completed(result)
    rows = [json.loads(line) for line in result.stdout.splitlines()]
    assert rows
    run = rows[0]["runs"][0] if "runs" in rows[0] else rows[0]
    assert run["status"] == "completed"
    assert run["root"] == str(lab.corpus)
    assert run["recovery_required_actions"] == 0
    assert {route["route_name"] for route in run["routes"]} == routes
    assert all(route["status"] == "completed" for route in run["routes"])
    return rows


@pytest.mark.capability("base")
def test_installed_entrypoints_and_fresh_status_are_headless(product_lab: _ProductLab) -> None:
    lab = product_lab
    module_version = lab.cli("--version")
    console_version = lab.cli("--version", console=True)
    _assert_completed(module_version)
    _assert_completed(console_version)
    assert module_version.stdout == console_version.stdout
    assert module_version.stdout.startswith("Neocortex ")
    help_result = lab.cli("--help")
    _assert_completed(help_result)
    resources = lab.python_json(
        "import json,sys\n"
        "before=set(sys.modules)\n"
        "import neocortex,importlib.metadata as metadata,importlib.resources as resources\n"
        "distribution=metadata.distribution('neocortex-framework')\n"
        "icons=resources.files('neocortex.interface.presentation').joinpath('assets')\n"
        "sizes={suffix:len(icons.joinpath('neocortex-app-icon.'+suffix).read_bytes()) "
        "for suffix in ('png','svg','ico')}\n"
        "heavy=('PIL','numpy','PySide6','fastembed','faster_whisper','onnxruntime')\n"
        "new=[name for name in set(sys.modules)-before if name.split('.')[0] in heavy]\n"
        "print(json.dumps({'sizes':sizes,'introduced_optional':new,"
        "'version_matches':neocortex.__version__==distribution.version,"
        "'bundled_wheels':[str(path) for path in distribution.files if str(path).endswith('.whl')]}))\n"
    )
    assert all(size > 0 for size in resources["sizes"].values())
    assert resources["introduced_optional"] == []
    assert resources["version_matches"] and resources["bundled_wheels"] == []
    status = lab.cli("--state-directory", str(lab.state), "--status", "--status-json")
    _assert_completed(status, expected=2)
    status_payload = json.loads(status.stdout)
    assert status_payload["error"]["code"] == "state_unavailable"
    assert "Traceback" not in status.stderr
    assert not lab.state.exists(), "a read must not create or migrate absent state"




@pytest.mark.capability("base")
def test_installed_inventory_owner_resumes_a_bounded_partial_scan(product_lab: _ProductLab) -> None:
    lab = product_lab
    originals = lab.copy_group("base")
    result = lab.python_json(
        """import json,sys
from pathlib import Path
from neocortex.deduplication import DedupIndex,InventoryScanBudgetExceeded,InventoryWorkBudget
from neocortex.deduplication.inventory.resume import InventoryResumeCheckpointStore
root=Path(sys.argv[1]); state=Path(sys.argv[2]); state.mkdir(mode=0o700)
checkpoint=state/'inventory.json'
def rows(index,scan_id):
 return [(Path(row.path).relative_to(root).as_posix(),row.size,row.file_id,row.birthtime_ns)
         for row in index.snapshots(scan_id)]
with DedupIndex(state/'dedup.sqlite3') as index:
 stopped=False
 try:
  index.scan(root,excluded_paths=(),batch_size=2,checkpoint_path=checkpoint,
             work_budget=InventoryWorkBudget(max_files=5))
 except InventoryScanBudgetExceeded:
  stopped=True
 partial=InventoryResumeCheckpointStore(checkpoint,root=root).read()
 assert stopped and partial.status=='partial'
 assert index.inventory_checkpoint(root) is None
 resumed=index.scan(root,excluded_paths=(),batch_size=2,checkpoint_path=checkpoint,
                    resume=True,work_budget=InventoryWorkBudget(max_files=25))
 expected=rows(index,resumed.scan_id)
 replay=index.scan(root,excluded_paths=(),batch_size=2,checkpoint_path=checkpoint,
                   resume=True,work_budget=InventoryWorkBudget(max_files=25))
 assert replay==resumed and rows(index,replay.scan_id)==expected
 final=InventoryResumeCheckpointStore(checkpoint,root=root).read()
 assert final.status=='complete' and final.stop_reason is None
with DedupIndex(state/'clean.sqlite3') as clean:
 scan=clean.scan(root,excluded_paths=(),deterministic=True,batch_size=2)
 assert rows(clean,scan.scan_id)==expected
print(json.dumps({'partial_files':partial.files_seen,'files':final.files_seen,
                  'status':final.status,'replayed_same_scan':replay.scan_id==resumed.scan_id}))
""",
        str(lab.corpus),
        str(lab.state),
    )
    assert result == {
        "partial_files": 5,
        "files": 20,
        "status": "complete",
        "replayed_same_scan": True,
    }
    assert _hashes(lab.corpus) == originals


@pytest.mark.capability("documents")
def test_real_pdf_docx_odt_extraction_does_not_require_optional_tools(
    product_lab: _ProductLab,
) -> None:
    lab = product_lab
    originals = lab.copy_group("documents")
    lab.hide_system_tools()
    options = (
        "--max-count",
        "25",
        "--docx-max-count",
        "25",
        "--office-max-count",
        "25",
        "--ocr",
        "never",
        "--pdf-workers",
        "1",
        "--ocr-workers",
        "1",
    )
    first = lab.process("pdf,docx,office", *options)
    _progress(first)
    for route in ("pdf", "docx", "office"):
        summary = _summary(first, f"route={route} ")
        assert summary["extracted"] == "1" and summary["errors"] == "0"
    _terminal_status(lab, {"pdf", "docx", "office"})
    assert lab.query(
        "pdf.sqlite3", "SELECT page_count,completed_pages,is_partial FROM documents"
    ) == [[1, 1, 0]]
    assert lab.query(
        "pdf.sqlite3", "SELECT COUNT(*) FROM page_fts WHERE page_fts MATCH 'NEOCORTEX_PDF'"
    ) == [[1]]
    for database, token in (("docx", "NEOCORTEX_DOCX"), ("office", "NEOCORTEX_ODT")):
        assert lab.query(
            f"{database}.sqlite3",
            "SELECT COUNT(*) FROM document_fts WHERE document_fts MATCH ?",
            (token,),
        ) == [[1]]
    replay = lab.process("pdf,docx,office", *options)
    for route in ("pdf", "docx", "office"):
        summary = _summary(replay, f"route={route} ")
        assert summary["extracted"] == "0" and summary["cache_hits"] == "1"
    assert lab.query(
        "framework.sqlite3",
        "SELECT COUNT(*) FROM file_actions WHERE apply_requested<>0 OR status NOT IN ('planned','skipped')",
    ) == [[0]]
    assert _hashes(lab.corpus) == originals


@pytest.mark.capability("image")
def test_real_image_classification_and_replay_without_ocr_or_inference(
    product_lab: _ProductLab,
) -> None:
    lab = product_lab
    originals = lab.copy_group("image")
    lab.hide_system_tools()
    options = ("--image-max-count", "25", "--image-workers", "1", "--image-document-ocr", "never")
    first = lab.process("image", *options)
    _progress(first)
    summary = _summary(first, "route=image ")
    assert summary["classified"] == "1" and summary["errors"] == "0"
    assert summary["document_ocr_attempts"] == "0"
    rows = lab.query(
        "image.sqlite3", "SELECT category,processing_signature,features_json,error_type FROM images"
    )
    assert len(rows) == 1 and rows[0][0] and rows[0][1] and json.loads(rows[0][2])
    assert rows[0][3] is None
    replay = lab.process("image", *options)
    assert _summary(replay, "route=image ")["classified"] == "0"
    assert _summary(replay, "route=image ")["cache_hits"] == "1"
    _terminal_status(lab, {"image"})
    assert _hashes(lab.corpus) == originals


@pytest.mark.capability("documents")
def test_real_tesseract_image_ocr_is_headless_and_has_durable_text(
    product_lab: _ProductLab,
) -> None:
    lab = product_lab
    originals = lab.copy_group("image")
    lab.hide_system_tools("tesseract")
    first = lab.process(
        "image",
        "--image-max-count",
        "25",
        "--image-workers",
        "1",
        "--image-document-ocr",
        "auto",
        "--image-ocr-lang",
        "eng",
        "--image-ocr-timeout",
        "20",
    )
    summary = _summary(first, "route=image ")
    assert summary["document_ocr_attempts"] == "1"
    assert summary["document_ocr_failures"] == "0"
    rows = lab.python_json(
        "import json,sys,zlib\n"
        "from neocortex.persistence.sqlite_immutable import SQLiteReadSession\n"
        "with SQLiteReadSession(sys.argv[1],mode='snapshot_temp') as connection:\n"
        " row=connection.execute('SELECT ocr_text_zlib,ocr_text_chars,evidence_json FROM images').fetchone()\n"
        " print(json.dumps({'text':zlib.decompress(row[0]).decode(),'chars':row[1], 'evidence':json.loads(row[2])}))\n",
        str(lab.state / "image.sqlite3"),
    )
    assert {"NEOCORTEX", "ORCHID"} <= set(rows["text"].split())
    assert rows["chars"] > 30 and rows["evidence"]
    provenance = rows["evidence"]["document_text"]
    assert provenance["provenance"].startswith("tesseract-")
    assert provenance["effective_languages"] == ["eng"]
    assert provenance["traineddata_hashes"][0][0] == "eng"
    assert len(provenance["traineddata_hashes"][0][1]) == 32
    replay = lab.process(
        "image",
        "--image-max-count",
        "25",
        "--image-workers",
        "1",
        "--image-document-ocr",
        "auto",
        "--image-ocr-lang",
        "eng",
        "--image-ocr-timeout",
        "20",
    )
    assert _summary(replay, "route=image ")["cache_hits"] == "1"
    assert _summary(replay, "route=image ")["document_ocr_attempts"] == "0"
    assert _hashes(lab.corpus) == originals


@pytest.mark.capability("platform")
def test_real_ffmpeg_video_without_audio_or_models_is_headless(product_lab: _ProductLab) -> None:
    lab = product_lab
    originals = lab.copy_group("video")
    lab.hide_system_tools("ffmpeg", "ffprobe")
    options = (
        "--video-max-count",
        "25",
        "--video-max-frames",
        "2",
        "--video-interval-seconds",
        "1",
        "--video-ocr",
        "never",
        "--video-file-timeout",
        "45",
    )
    first = lab.process("video", *options)
    _progress(first)
    summary = _summary(first, "route=video ")
    assert summary["processed"] == "1" and summary["errors"] == "0"
    _terminal_status(lab, {"video"})
    replay = lab.process("video", *options)
    assert _summary(replay, "route=video ")["cache_hits"] == "1"
    assert _summary(replay, "route=video ")["cache_hits"] == "1"
    assert not (lab.state / "audio.sqlite3").exists()
    assert _hashes(lab.corpus) == originals


@pytest.mark.capability("platform")
def test_real_low_frame_rate_video_does_not_seek_beyond_last_frame(
    product_lab: _ProductLab,
) -> None:
    """Container duration is not a guarantee of a frame at duration minus 200 ms."""
    lab = product_lab
    originals = lab.copy_group("video_low_fps")
    lab.hide_system_tools("ffmpeg", "ffprobe")
    first = lab.process(
        "video",
        "--video-max-count",
        "25",
        "--video-max-frames",
        "2",
        "--video-interval-seconds",
        "1",
        "--video-ocr",
        "never",
        "--video-file-timeout",
        "45",
    )
    summary = _summary(first, "route=video ")
    assert summary["complete"] == "1" and summary["partial"] == "0"
    assert summary["frames_sampled"] == "2" and summary["errors"] == "0"
    assert lab.query("video.sqlite3", "SELECT status,frame_count,warnings_json FROM documents") == [
        ["complete", 2, "[]"]
    ]
    assert _hashes(lab.corpus) == originals


@pytest.mark.capability("documents")
def test_all_keeps_missing_inference_visible_and_independent_results_readable(
    product_lab: _ProductLab,
) -> None:
    """An unavailable requested route is not a successful, smaller meaning of --all."""
    lab = product_lab
    engines = lab.python_json(
        "import json,importlib.util as u; "
        "print(json.dumps({name:u.find_spec(name) is not None "
        "for name in ('fastembed','faster_whisper','ctranslate2')}))"
    )
    if any(engines.values()):
        pytest.skip(
            "negative --all workflow requires a documents/image venv without inference engines"
        )
    lab.copy_group("base")
    originals = lab.copy_group("audio")
    assert len(originals) == 21
    lab.hide_system_tools("ffmpeg", "ffprobe")
    model_cache = lab.directory / "models"
    model_cache.mkdir(mode=0o700)
    audit_path = lab.directory / "network-audit.json"
    # Instrument the installed CLI entrypoint, not a replacement orchestrator.
    # Python audit hooks are process-local; child commands are recorded too, but
    # this deliberately does not claim interception of arbitrary native children.
    script = """import json,socket,sys
from pathlib import Path
report=Path(sys.argv[1]); network=[]; children=[]
def audit(event,args):
 if event=='subprocess.Popen':
  command=args[1]
  children.append(list(map(str,command)) if isinstance(command,(list,tuple)) else [str(command)])
 blocked=event in {'socket.getaddrinfo','socket.gethostbyname','urllib.Request'}
 if event in {'socket.connect','socket.sendto'}:
  blocked=getattr(args[0],'family',None)!=socket.AF_UNIX
 if blocked:
  network.append(event)
  raise RuntimeError('offline test observed a network attempt: '+event)
sys.addaudithook(audit)
try:
 from neocortex.interface.entrypoint import entrypoint
 exit_code=entrypoint(sys.argv[2:])
finally:
 report.write_text(json.dumps({'network_attempts':network,'children':children}))
raise SystemExit(exit_code)
"""
    options = [
        "--all",
        "--root",
        str(lab.corpus),
        "--state-directory",
        str(lab.state),
        "--no-document-catalog",
        "--strict-exit-codes",
        "--audio-local-models-only",
        "--audio-model-cache",
        str(model_cache),
        "--semantic-model-cache",
        str(model_cache),
        "--ocr",
        "never",
        "--image-document-ocr",
        "never",
        "--video-ocr",
        "never",
        "--pdf-workers",
        "1",
        "--ocr-workers",
        "1",
        "--image-workers",
        "1",
    ]
    for argument in (
        "--max-count",
        "--docx-max-count",
        "--office-max-count",
        "--text-max-count",
        "--audio-max-count",
        "--video-max-count",
        "--image-max-count",
    ):
        options.extend((argument, "25"))
    result = lab.execute(["-c", script, str(audit_path), *options])
    _assert_completed(result, expected=2)
    assert "ERROR route_execution_failed status=failed completion=incomplete" in result.stderr
    assert "Traceback" not in result.stderr
    assert "faster-whisper" in result.stdout + result.stderr
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    assert audit["network_attempts"] == []
    # With no inference backend, only bounded local media probes are allowed.
    # ffprobe may inspect the selected audio fixture before the typed missing
    # model result is published; no inference/worker process may be launched.
    assert all(Path(command[0]).name in {"ffmpeg", "ffprobe"} for command in audit["children"])
    assert all(
        Path(command[0]).name == "ffmpeg"
        or command[1:] == ["-version"]
        or command[1:3] == ["-v", "error"]
        for command in audit["children"]
    )
    assert _hashes(model_cache) == {}, "a missing model is not permission to download weights"

    registered = lab.python_json(
        "import json; from neocortex.runtime.orchestration.route_selection import BUILTIN_ROUTE_ORDER; "
        "print(json.dumps(BUILTIN_ROUTE_ORDER))"
    )
    status = lab.cli("--state-directory", str(lab.state), "--status", "--status-json")
    _assert_completed(status)
    status_payload = json.loads(status.stdout.splitlines()[0])
    run = status_payload["runs"][0]
    assert run["status"] == "failed"
    routes = {route["route_name"]: route for route in run["routes"]}
    assert set(routes) == set(registered)
    assert routes["audio"]["status"] == "failed"
    # The current public degradation contract names the unavailable optional
    # capability explicitly; older releases surfaced WhisperRuntimeError.
    assert routes["audio"]["error_type"] == "AudioRuntimeUnavailableError"
    assert routes["text"]["status"] == "completed"
    audio_error = lab.query(
        "framework.sqlite3", "SELECT error_message FROM route_runs WHERE route_name='audio'"
    )
    assert "faster-whisper" in audio_error[0][0]
    assert lab.query("audio.sqlite3", "SELECT COUNT(*) FROM documents") == [[0]]
    assert lab.query("audio.sqlite3", "SELECT COUNT(*) FROM segments") == [[0]]
    assert not (lab.state / "semantic.sqlite3").exists()
    assert lab.query(
        "text.sqlite3",
        "SELECT COUNT(*) FROM document_fts WHERE document_fts MATCH 'NEOCORTEX_UNICODE'",
    ) == [[1]]
    assert lab.query(
        "framework.sqlite3",
        "SELECT COUNT(*) FROM file_actions WHERE apply_requested<>0 "
        "OR status NOT IN ('planned','skipped')",
    ) == [[0]]
    assert _hashes(lab.corpus) == originals
