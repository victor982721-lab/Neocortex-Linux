"""Authenticated pip bootstrap contracts shared by CI and release tooling."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import pip_bootstrap
from tools import bootstrap_pip, release_linux


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _synthetic_wheel(tmp_path: Path) -> tuple[Path, str]:
    payload = b"synthetic authenticated pip wheel"
    wheel = tmp_path / pip_bootstrap.PIP_BOOTSTRAP_FILENAME
    wheel.write_bytes(payload)
    return wheel, hashlib.sha256(payload).hexdigest()


def _runner(
    calls: list[tuple[str, ...]],
    *,
    version: str,
):
    def run(arguments, **_kwargs):
        call = tuple(os.fspath(argument) for argument in arguments)
        calls.append(call)
        stdout = f"{version}\n" if pip_bootstrap.PIP_VERIFY_SCRIPT in call else ""
        return subprocess.CompletedProcess(call, 0, stdout, "")

    return run


def test_release_linux_import_does_not_require_posix_fcntl() -> None:
    script = (
        "import sys\n"
        "sys.modules['fcntl'] = None\n"
        f"sys.path.insert(0, {str(PROJECT_ROOT)!r})\n"
        "import tools.release_linux\n"
        "print('ok')\n"
    )

    completed = subprocess.run(
        (sys.executable, "-I", "-B", "-c", script),
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "ok"


def test_release_contract_preserves_pip_bootstrap_reexports() -> None:
    assert pip_bootstrap.PIP_BOOTSTRAP_VERSION == "26.2.1"
    assert pip_bootstrap.PIP_BOOTSTRAP_FILENAME == "pip-26.2.1-py3-none-any.whl"
    assert len(pip_bootstrap.PIP_BOOTSTRAP_SHA256) == 64
    assert pip_bootstrap.PIP_BOOTSTRAP_URL.startswith("https://files.pythonhosted.org/")
    assert release_linux.PIP_BOOTSTRAP_VERSION == pip_bootstrap.PIP_BOOTSTRAP_VERSION
    assert release_linux._PIP_WHEEL_RUNNER == pip_bootstrap.PIP_WHEEL_RUNNER


def test_wrong_download_hash_never_executes_the_target_interpreter(tmp_path: Path) -> None:
    calls: list[tuple[str, ...]] = []

    def forged_download(_url: str, destination: Path) -> None:
        destination.write_bytes(b"forged")

    with pytest.raises(pip_bootstrap.PipBootstrapError, match="exact SHA-256"):
        pip_bootstrap.bootstrap_python(
            tmp_path / "target-python",
            tmp_path,
            runner=_runner(calls, version=pip_bootstrap.PIP_BOOTSTRAP_VERSION),
            downloader=forged_download,
        )

    assert calls == []


def test_divergent_installed_pip_version_fails_closed(tmp_path: Path) -> None:
    wheel, sha256 = _synthetic_wheel(tmp_path)
    calls: list[tuple[str, ...]] = []

    with pytest.raises(pip_bootstrap.PipBootstrapError, match="unexpected bootstrapped"):
        pip_bootstrap.seed_pip(
            tmp_path / "target-python",
            wheel,
            runner=_runner(calls, version="26.1.1"),
            sha256=sha256,
        )

    assert len(calls) == 2


def test_seed_command_is_isolated_and_network_free(tmp_path: Path) -> None:
    wheel, sha256 = _synthetic_wheel(tmp_path)
    target = tmp_path / "target-python"
    calls: list[tuple[str, ...]] = []

    observed = pip_bootstrap.seed_pip(
        target,
        wheel,
        runner=_runner(calls, version=pip_bootstrap.PIP_BOOTSTRAP_VERSION),
        sha256=sha256,
    )

    assert observed == pip_bootstrap.PIP_BOOTSTRAP_VERSION
    install, verify = calls
    assert install[:4] == (
        os.fspath(target),
        "-I",
        "-c",
        pip_bootstrap.PIP_WHEEL_RUNNER,
    )
    assert install.count(os.fspath(wheel)) == 2
    assert "--isolated" in install
    assert "--no-index" in install
    assert "--no-deps" in install
    assert "-m" not in install
    assert "PIP_CONFIG_FILE" in pip_bootstrap.PIP_WHEEL_RUNNER
    assert "os.devnull" in pip_bootstrap.PIP_WHEEL_RUNNER
    assert verify == (
        os.fspath(target),
        "-I",
        "-c",
        pip_bootstrap.PIP_VERIFY_SCRIPT,
    )


def test_wheel_runner_discards_ambient_pip_configuration(tmp_path: Path) -> None:
    wheel = tmp_path / pip_bootstrap.PIP_BOOTSTRAP_FILENAME
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("pip/__init__.py", "")
        archive.writestr(
            "pip/__main__.py",
            """import json,os,sys
print(json.dumps({
    'config': os.environ.get('PIP_CONFIG_FILE'),
    'target': os.environ.get('PIP_TARGET'),
    'argv': sys.argv,
}, sort_keys=True))
""",
        )
    environment = dict(os.environ)
    environment["PIP_CONFIG_FILE"] = os.fspath(tmp_path / "untrusted.ini")
    environment["PIP_TARGET"] = os.fspath(tmp_path / "redirected")

    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            pip_bootstrap.PIP_WHEEL_RUNNER,
            os.fspath(wheel),
            "--isolated",
            "install",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        env=environment,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload == {
        "argv": [os.fspath(wheel), "--isolated", "install"],
        "config": os.devnull,
        "target": None,
    }


def test_environment_creation_is_explicitly_without_pip(tmp_path: Path) -> None:
    wheel, sha256 = _synthetic_wheel(tmp_path)
    root = tmp_path / "environment"
    builders: list[dict[str, object]] = []
    calls: list[tuple[str, ...]] = []

    class FakeBuilder:
        def __init__(self, **kwargs: object) -> None:
            builders.append(kwargs)

        def create(self, env_dir: str | Path) -> None:
            python = pip_bootstrap.environment_python(Path(env_dir))
            python.parent.mkdir(parents=True)
            python.write_bytes(b"synthetic")

    python = pip_bootstrap.create_pip_environment(
        root,
        wheel,
        runner=_runner(calls, version=pip_bootstrap.PIP_BOOTSTRAP_VERSION),
        symlinks=False,
        builder_factory=FakeBuilder,
        sha256=sha256,
    )

    assert builders == [{"with_pip": False, "clear": False, "symlinks": False}]
    assert python == pip_bootstrap.environment_python(root)
    assert len(calls) == 2


def test_environment_interpreter_path_supports_windows_layout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_os_name = os.name
    original_path = Path()
    monkeypatch.setattr(pip_bootstrap, "os", SimpleNamespace(name="nt"))

    assert pip_bootstrap.environment_python(tmp_path) == tmp_path / "Scripts" / "python.exe"
    assert os.name == original_os_name
    assert Path() == original_path


def test_bootstrap_cli_seeds_an_explicit_offline_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    wheel = tmp_path / pip_bootstrap.PIP_BOOTSTRAP_FILENAME
    target = tmp_path / "environment" / "python"
    observed: list[tuple[Path, Path]] = []

    def seed(python: Path, candidate: Path) -> str:
        observed.append((python, candidate))
        return pip_bootstrap.PIP_BOOTSTRAP_VERSION

    monkeypatch.setattr(pip_bootstrap, "seed_pip", seed)

    assert bootstrap_pip.main(("--python", str(target), "--wheel", str(wheel))) == 0

    assert observed == [(target.absolute(), wheel.absolute())]
    assert '"pip": "26.2.1"' in capsys.readouterr().out


def test_bootstrap_script_is_directly_executable_from_checkout() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            str(PROJECT_ROOT / "tools" / "bootstrap_pip.py"),
            "--help",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert "--python" in result.stdout
