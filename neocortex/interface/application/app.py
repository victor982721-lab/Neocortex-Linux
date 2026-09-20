"""Desktop application bootstrap kept independent from the operational worker."""

from __future__ import annotations

import multiprocessing
import sys
from collections.abc import Sequence
from typing import TYPE_CHECKING

from neocortex import __version__
from .arguments import parse_arguments as _parse_arguments

if TYPE_CHECKING:
    from ..presentation.windows.main import MainWindow


# region [01] Bootstrap


def create_window(arguments: Sequence[str] = ()) -> MainWindow:
    parsed = _parse_arguments(arguments)
    from ..presentation.windows.main import MainWindow

    return MainWindow(
        initial_root=parsed.root,
        state_directory=parsed.state_directory,
    )


def main(arguments: Sequence[str] | None = None) -> int:
    multiprocessing.freeze_support()
    parsed_arguments = sys.argv[1:] if arguments is None else list(arguments)
    _parse_arguments(parsed_arguments)

    from PySide6.QtCore import QCoreApplication, Qt
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication

    from ..presentation.assets import application_icon_path
    from ..presentation.theme import STYLESHEET

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
