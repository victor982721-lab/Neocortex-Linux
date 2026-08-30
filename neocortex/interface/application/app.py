"""Desktop application bootstrap kept independent from the operational worker."""

from __future__ import annotations

import argparse
import ctypes
import multiprocessing
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

from neocortex.platform_policy import default_corpus_root

from PySide6.QtCore import QCoreApplication, Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from neocortex import __version__
from neocortex.runtime.config.app_paths import default_state_directory

from ..presentation.assets import application_icon_path
from ..presentation.theme import STYLESHEET
from ..presentation.windows.main import MainWindow


# region [01] Bootstrap


def _parse_arguments(arguments: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="Neocortex --ui", allow_abbrev=False)
    parser.add_argument("--root", type=Path, default=default_corpus_root())
    return parser.parse_args(list(arguments))


def create_window(arguments: Sequence[str] = ()) -> MainWindow:
    parsed = _parse_arguments(arguments)
    return MainWindow(
        initial_root=parsed.root,
        state_directory=default_state_directory(),
    )


def _set_windows_application_identity() -> None:
    if os.name == "nt":
        windll = cast(Any, ctypes).windll
        windll.shell32.SetCurrentProcessExplicitAppUserModelID("Neocortex.Desktop")


def main(arguments: Sequence[str] | None = None) -> int:
    multiprocessing.freeze_support()
    parsed_arguments = sys.argv[1:] if arguments is None else list(arguments)
    QCoreApplication.setOrganizationName("NeoCortex")
    QCoreApplication.setApplicationName("NeoCortex")
    QCoreApplication.setApplicationVersion(__version__)
    instance = QApplication.instance()
    if instance is None:
        application = QApplication(["Neocortex"])
    elif isinstance(instance, QApplication):
        application = instance
    else:
        raise RuntimeError("A non-GUI Qt application already exists")
    _set_windows_application_identity()
    application.setApplicationDisplayName("NeoCortex")
    application.setAttribute(
        Qt.ApplicationAttribute.AA_DontShowIconsInMenus,
        False,
    )
    application.setStyleSheet(STYLESHEET)
    icon = QIcon(str(application_icon_path()))
    if not icon.isNull():
        application.setWindowIcon(icon)
    window = create_window(parsed_arguments)
    if not icon.isNull():
        window.setWindowIcon(icon)
    window.show()
    return application.exec()


# endregion [01]
