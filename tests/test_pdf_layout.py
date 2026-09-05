# region [00] Contexto del módulo
# Módulo: tests/test_pdf_layout.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]
# region [01] Dependencias del módulo
from __future__ import annotations


import io
import tempfile
import unittest
from pathlib import Path

import fitz  # type: ignore[import-untyped]
from PIL import Image, ImageDraw

from neocortex.capabilities.formats.pdf.pdf_layout import map_page_layout, signature_similarity


TEST_CAPABILITIES = ('documents',)
# endregion [01]

# region [02] Implementación


class PdfLayoutTests(unittest.TestCase):
    def test_scanned_pages_keep_visual_header_evidence_without_raster_artifacts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            signatures = []
            for number in range(2):
                image = Image.new("L", (600, 800), 255)
                drawing = ImageDraw.Draw(image)
                drawing.rectangle((20, 15, 580, 90), outline=0, width=5)
                drawing.rectangle((40, 30, 160, 70), fill=0)
                drawing.text((200, 40), "CFE FORMATO", fill=0)
                drawing.text((80, 300 + number * 80), f"contenido {number}", fill=0)
                encoded = io.BytesIO()
                image.save(encoded, format="PNG")
                image.close()

                path = root / f"scan-{number}.pdf"
                document = fitz.open()
                page = document.new_page(width=600, height=800)
                page.insert_image(page.rect, stream=encoded.getvalue())
                document.save(path)
                document.close()
                with fitz.open(path) as reopened:
                    layout = map_page_layout(reopened[0])
                self.assertEqual(layout["source_kind"], "image_only")
                self.assertEqual(len(layout["visual_grid"]), 320)
                self.assertGreater(layout["header_ink"], 0)
                self.assertEqual(layout["blocks"][0]["kind"], "image")
                signatures.append(layout["header_simhash64"])

            self.assertGreater(signature_similarity(*signatures), 0.90)
            self.assertEqual(list(root.glob("*.png")), [])


if __name__ == "__main__":
    unittest.main()
# endregion [02]
