"""Authenticated, pip-independent bootstrap contract for the pinned pip wheel."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import urllib.request
import venv
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol


PIP_BOOTSTRAP_VERSION = "26.2.1"
PIP_BOOTSTRAP_FILENAME = f"pip-{PIP_BOOTSTRAP_VERSION}-py3-none-any.whl"
PIP_BOOTSTRAP_SHA256 = "71138adf1f4ca900cdb7d289c21b7494329f2332b6d85f0e1c42108c0384ed3e"
PIP_BOOTSTRAP_URL = (
    "https://files.pythonhosted.org/packages/f3/6e/"
    "1736e5b4ae2b778ef2f81c47d797de9f891d4d8acb047a24ca37a60294dd/"
    f"{PIP_BOOTSTRAP_FILENAME}"
)
PIP_WHEEL_RUNNER = (
    "import os,runpy,sys\n"
    "for key in tuple(os.environ):\n"
    "    if key.upper().startswith('PIP_'):\n"
    "        os.environ.pop(key)\n"
    "os.environ['PIP_CONFIG_FILE']=os.devnull\n"
    "wheel=sys.argv[1]\n"
    "sys.path.insert(0,wheel)\n"
    "sys.argv=sys.argv[1:]\n"
    "runpy.run_module('pip',run_name='__main__')"
)
PIP_VERIFY_SCRIPT = "import pip; print(pip.__version__)"


class PipBootstrapError(RuntimeError):
    """The authenticated pip bootstrap contract could not be satisfied."""


class EnvironmentBuilder(Protocol):
    """Minimal interface used from :class:`venv.EnvBuilder`."""

    def create(self, env_dir: str | Path) -> None: ...


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]
Downloader = Callable[[str, Path], None]
BuilderFactory = Callable[..., EnvironmentBuilder]


def _run(
    arguments: Sequence[str | os.PathLike[str]],
    *,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [os.fspath(argument) for argument in arguments],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[-4000:]
        raise PipBootstrapError(
            f"pip bootstrap command failed ({result.returncode}): {arguments[0]}: {detail}"
        )
    return result


def sha256_file(path: Path) -> str:
    """Return a streaming SHA-256 digest for one candidate wheel."""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def download_pip_bootstrap(url: str, destination: Path) -> None:
    """Download one artifact with urllib into a caller-owned workspace."""

    request = urllib.request.Request(url, headers={"User-Agent": "NeoCortex-pip-bootstrap/1"})
    with urllib.request.urlopen(request, timeout=120) as response, destination.open("xb") as output:
        shutil.copyfileobj(response, output, length=1024 * 1024)
        output.flush()
        os.fsync(output.fileno())


def require_pip_bootstrap(
    wheel: Path,
    *,
    filename: str = PIP_BOOTSTRAP_FILENAME,
    sha256: str = PIP_BOOTSTRAP_SHA256,
) -> None:
    """Reject a wheel unless both its canonical name and content hash match."""

    try:
        observed = sha256_file(wheel)
    except OSError as exc:
        raise PipBootstrapError("pip bootstrap wheel is unavailable") from exc
    if wheel.name != filename or observed != sha256:
        raise PipBootstrapError("pip bootstrap wheel failed exact SHA-256 validation")


def prepare_pip_bootstrap(
    workspace: Path,
    *,
    downloader: Downloader = download_pip_bootstrap,
    filename: str = PIP_BOOTSTRAP_FILENAME,
    url: str = PIP_BOOTSTRAP_URL,
    sha256: str = PIP_BOOTSTRAP_SHA256,
) -> Path:
    """Download and authenticate the pinned wheel without importing pip."""

    wheel = workspace / filename
    downloader(url, wheel)
    require_pip_bootstrap(wheel, filename=filename, sha256=sha256)
    return wheel


def pip_install_arguments(
    python: Path,
    wheel: Path,
) -> tuple[str | os.PathLike[str], ...]:
    """Return the isolated, network-free command that seeds the target."""

    return (
        python,
        "-I",
        "-c",
        PIP_WHEEL_RUNNER,
        wheel,
        "--isolated",
        "install",
        "--disable-pip-version-check",
        "--no-cache-dir",
        "--no-index",
        "--no-deps",
        "--force-reinstall",
        wheel,
    )


def verify_pip(
    python: Path,
    *,
    runner: CommandRunner = _run,
    expected_version: str = PIP_BOOTSTRAP_VERSION,
) -> str:
    """Verify the installed pip module under isolated interpreter startup."""

    observed = runner(
        (python, "-I", "-c", PIP_VERIFY_SCRIPT),
        timeout=60,
    ).stdout.strip()
    if observed != expected_version:
        raise PipBootstrapError(f"unexpected bootstrapped pip version: {observed}")
    return observed


def seed_pip(
    python: Path,
    wheel: Path,
    *,
    runner: CommandRunner = _run,
    expected_version: str = PIP_BOOTSTRAP_VERSION,
    filename: str = PIP_BOOTSTRAP_FILENAME,
    sha256: str = PIP_BOOTSTRAP_SHA256,
) -> str:
    """Authenticate the wheel, seed one interpreter, and verify exact pip."""

    require_pip_bootstrap(wheel, filename=filename, sha256=sha256)
    runner(pip_install_arguments(python, wheel), timeout=300)
    return verify_pip(
        python,
        runner=runner,
        expected_version=expected_version,
    )


def environment_python(root: Path) -> Path:
    """Return the interpreter location produced by venv on this platform."""

    if os.name == "nt":
        return root / "Scripts" / "python.exe"
    return root / "bin" / "python"


def create_pip_environment(
    root: Path,
    wheel: Path,
    *,
    runner: CommandRunner = _run,
    symlinks: bool,
    builder_factory: BuilderFactory = venv.EnvBuilder,
    expected_version: str = PIP_BOOTSTRAP_VERSION,
    filename: str = PIP_BOOTSTRAP_FILENAME,
    sha256: str = PIP_BOOTSTRAP_SHA256,
) -> Path:
    """Create a ``--without-pip`` venv and seed it only from the verified wheel."""

    # Validate before even creating the target, then validate again immediately
    # before execution to fail closed if the artifact changes in between.
    require_pip_bootstrap(wheel, filename=filename, sha256=sha256)
    builder_factory(with_pip=False, clear=False, symlinks=symlinks).create(root)
    python = environment_python(root)
    seed_pip(
        python,
        wheel,
        runner=runner,
        expected_version=expected_version,
        filename=filename,
        sha256=sha256,
    )
    return python


def bootstrap_python(
    python: Path,
    workspace: Path,
    *,
    runner: CommandRunner = _run,
    downloader: Downloader = download_pip_bootstrap,
) -> str:
    """Download, authenticate, seed, and verify one target interpreter."""

    wheel = prepare_pip_bootstrap(workspace, downloader=downloader)
    return seed_pip(python, wheel, runner=runner)


__all__ = [
    "PIP_BOOTSTRAP_FILENAME",
    "PIP_BOOTSTRAP_SHA256",
    "PIP_BOOTSTRAP_URL",
    "PIP_BOOTSTRAP_VERSION",
    "PIP_VERIFY_SCRIPT",
    "PIP_WHEEL_RUNNER",
    "BuilderFactory",
    "CommandRunner",
    "Downloader",
    "EnvironmentBuilder",
    "PipBootstrapError",
    "bootstrap_python",
    "create_pip_environment",
    "download_pip_bootstrap",
    "environment_python",
    "pip_install_arguments",
    "prepare_pip_bootstrap",
    "require_pip_bootstrap",
    "seed_pip",
    "sha256_file",
    "verify_pip",
]
