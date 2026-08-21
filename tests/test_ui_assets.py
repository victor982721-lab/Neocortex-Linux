# region [00] Contexto del módulo
# Módulo: tests/test_ui_assets.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]
# region [01] Dependencias del módulo
from __future__ import annotations

import unittest
from typing import ClassVar

from PIL import Image
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from neocortex.interface.presentation.assets import application_icon_path, asset_directory
# endregion [01]

# region [02] Implementación


class UiAssetTests(unittest.TestCase):
    application: ClassVar[QApplication]

    @classmethod
    def setUpClass(cls) -> None:
        instance = QApplication.instance()
        if instance is not None and not isinstance(instance, QApplication):
            raise RuntimeError("A non-GUI Qt application already exists")
        cls.application = instance or QApplication([])

    def test_application_icon_assets_are_complete(self) -> None:
        directory = asset_directory()
        svg = directory / "neocortex-app-icon.svg"
        png = directory / "neocortex-app-icon.png"
        ico = application_icon_path()
        self.assertTrue(svg.is_file())
        self.assertTrue(png.is_file())
        self.assertTrue(ico.is_file())

        with Image.open(png) as image:
            self.assertEqual(image.size, (1024, 1024))
            self.assertEqual(image.mode, "RGBA")
        with Image.open(ico) as image:
            sizes = image.info.get("sizes", set())
            self.assertIn((16, 16), sizes)
            self.assertIn((32, 32), sizes)
            self.assertIn((256, 256), sizes)
        self.assertFalse(QIcon(str(ico)).isNull())


if __name__ == "__main__":
    unittest.main()
# endregion [02]
