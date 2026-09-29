from __future__ import annotations

import struct
import zipfile
from pathlib import Path

from neocortex.capabilities.formats.archive.intake import decide_zip, intake_zip


def _valid_elf() -> bytes:
    header = bytearray(64)
    header[:4] = b"\x7fELF"
    header[4:7] = b"\x02\x01\x01"
    struct.pack_into("<HHI", header, 16, 2, 0x3E, 1)
    struct.pack_into("<QQ", header, 32, 0, 0)
    struct.pack_into("<HHHHH", header, 52, 64, 56, 0, 64, 0)
    return bytes(header)


def _valid_pe() -> bytes:
    image = bytearray(400)
    image[:2] = b"MZ"
    struct.pack_into("<I", image, 0x3C, 64)
    image[64:68] = b"PE\0\0"
    struct.pack_into("<HHIIIHH", image, 68, 0x8664, 1, 0, 0, 0, 0xF0, 0x0002)
    struct.pack_into("<H", image, 88, 0x10B)
    return bytes(image)


def _zip(path: Path, members: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)


def test_runtime_sdk_is_held_before_generic_extraction(tmp_path: Path) -> None:
    source = tmp_path / "SDK.zip"
    _zip(
        source,
        {
            "sdk/bin/tool.exe": _valid_pe(),
            "sdk/lib/libcore.so": _valid_elf(),
            "sdk/include/core.h": b"header\n",
        },
    )

    decision = decide_zip(source)
    assert decision.classification.kind == "atomic_package"
    assert decision.classification.unit_kind == "artifact_runtime_hold"
    assert any(item.startswith("artifact_policy:") for item in decision.classification.evidence)
    assert decision.content_proof is not None
    outcome = intake_zip(source, apply=False)
    assert outcome.status == "atomic"
    assert source.exists()


def test_sdk_name_without_runtime_proof_remains_generic(tmp_path: Path) -> None:
    source = tmp_path / "SDK.zip"
    _zip(source, {"docs/readme.md": b"useful documentation\n"})
    decision = decide_zip(source)
    assert decision.classification.kind == "generic_zip"
    assert decision.classification.unit_kind != "artifact_runtime_hold"


def test_mixed_document_and_runtime_members_are_not_held_as_disposable(tmp_path: Path) -> None:
    source = tmp_path / "runtime.zip"
    _zip(
        source,
        {
            "sdk/bin/tool.exe": _valid_pe(),
            "sdk/lib/libcore.so": _valid_elf(),
            "reports/acceptance.pdf": b"%PDF-1.7\nreport\n",
        },
    )
    decision = decide_zip(source)
    assert decision.classification.kind == "generic_zip"
    assert decision.classification.unit_kind != "artifact_runtime_hold"


def test_wheel_and_license_members_preserve_a_mixed_archive(tmp_path: Path) -> None:
    source = tmp_path / "SDK.zip"
    _zip(
        source,
        {
            "sdk/bin/tool.exe": _valid_pe(),
            "packages/example.whl": b"wheel bytes",
            "licenses/LICENSE.txt": b"license\n",
        },
    )
    decision = decide_zip(source)
    assert decision.classification.kind == "generic_zip"
    assert decision.classification.unit_kind != "artifact_runtime_hold"
