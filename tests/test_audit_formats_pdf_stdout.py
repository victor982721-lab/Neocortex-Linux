from __future__ import annotations

import json
import os
import subprocess
import sys


def test_pdf_capability_probe_keeps_stdout_json_clean() -> None:
    script = r'''
import json
import pymupdf
from neocortex.capabilities.formats.pdf.pdf_admin import doctor_pdf_runtime
from neocortex.capabilities.formats.pdf.pdf_layout import map_page_layout

with pymupdf.open() as document:
    page = document.new_page()
    page.insert_text((72, 72), "PDF stdout regression")
    layout = map_page_layout(page)
report = doctor_pdf_runtime(ocr_mode="never")
print(json.dumps({
    "checks": [check.name for check in report.checks],
    "source_kind": layout["source_kind"],
}, sort_keys=True))
'''
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=os.environ.copy(),
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    assert completed.stdout.count("\n") == 1
    payload = json.loads(completed.stdout)
    assert payload["source_kind"] == "native_text"
    assert "pymupdf" in payload["checks"]
