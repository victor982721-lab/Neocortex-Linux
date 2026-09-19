"""Exact grid/signature parity for the native pixel view, without geometry edits."""

from types import SimpleNamespace

import pytest

from neocortex.capabilities.formats.pdf.pdf_layout import _visual_grid


TEST_CAPABILITIES = ("documents",)


@pytest.mark.parametrize("rotation", (0, 90, 180, 270))
def test_visual_grid_matches_a_copied_pixel_buffer(rotation):
    import fitz

    document = fitz.open()
    try:
        page = document.new_page(width=600, height=800)
        page.draw_rect(fitz.Rect(15, 25, 500, 110), color=(0, 0, 0), fill=(0, 0, 0))
        page.draw_rect(fitz.Rect(80, 160, 420, 650), color=(0.3, 0.3, 0.3), fill=(0.6, 0.6, 0.6))
        page.set_rotation(rotation)
        actual = _visual_grid(page)

        def copied_pixels(**kwargs):
            pixmap = page.get_pixmap(**kwargs)
            return SimpleNamespace(width=pixmap.width, height=pixmap.height,
                                   stride=pixmap.stride, samples_mv=memoryview(pixmap.samples))

        expected = _visual_grid(SimpleNamespace(get_pixmap=copied_pixels))
        assert actual[-1] is None
        assert actual == expected
    finally:
        document.close()
