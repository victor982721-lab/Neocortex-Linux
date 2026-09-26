"""Audit regressions for bounded media detection and route MIME joins."""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from neocortex.platform.content_capability_manifest import content_capabilities_for_mime
from neocortex.platform.content_types import identify


E2E_PROBE_AUDIO = Path(
    "/tmp/neocortex-audit-01a0dbb9/e2e-fixtures/probe-only/audio"
)
AMR_FIXTURE = Path("/tmp/neocortex-audit-01a0dbb9/amr-fixture/amr-nb.amr")
AMR_LOWRATE_FIXTURE = Path(
    "/tmp/neocortex-audit-01a0dbb9/media-review/lowrate-6000.amr"
)
REAL_AUDIO_CASES = (
    ("audio-aac.aac", "audio/aac"),
    ("audio-aiff.aiff", "audio/x-aiff"),
    ("audio-caf.caf", "audio/x-caf"),
    ("audio-wma.wma", "audio/x-ms-wma"),
)


def _probe_fixture(name: str) -> Path:
    path = E2E_PROBE_AUDIO / name
    if not path.is_file():
        pytest.skip(f"E2E fixture is not present: {path}")
    return path


def _amr_fixture() -> Path:
    if not AMR_FIXTURE.is_file():
        pytest.skip(f"validated AMR fixture is not present: {AMR_FIXTURE}")
    return AMR_FIXTURE


def _lowrate_amr_fixture() -> Path:
    if not AMR_LOWRATE_FIXTURE.is_file():
        pytest.skip(f"validated low-rate AMR fixture is not present: {AMR_LOWRATE_FIXTURE}")
    return AMR_LOWRATE_FIXTURE


@pytest.mark.parametrize(("name", "expected_mime"), REAL_AUDIO_CASES)
def test_real_e2e_audio_probe_fixtures_join_to_audio_route(
    name: str, expected_mime: str
) -> None:
    source = _probe_fixture(name)

    decision = identify(source)

    assert decision.status == "known"
    assert decision.mime == expected_mime
    assert decision.accepts(source)
    capabilities = content_capabilities_for_mime(expected_mime)
    assert tuple(item.capability_id for item in capabilities) == ("audio",)


def test_audio_only_webm_keeps_shared_video_mime_contract() -> None:
    source = _probe_fixture("audio-only.webm")

    decision = identify(source)

    assert decision.mime == "video/webm"
    assert tuple(item.capability_id for item in content_capabilities_for_mime("video/webm")) == (
        "video",
    )


def test_validated_amr_nb_fixture_joins_to_audio_route() -> None:
    source = _amr_fixture()

    decision = identify(source)

    assert decision.status == "known"
    assert decision.mime == "audio/amr"
    assert decision.evidence == "magic:amr-nb"
    assert decision.accepts(source)
    assert tuple(item.capability_id for item in content_capabilities_for_mime("audio/amr")) == (
        "audio",
    )


def test_amr_signature_beats_spoofed_extension(tmp_path: Path) -> None:
    spoofed = tmp_path / "recording.txt"
    spoofed.write_bytes(_amr_fixture().read_bytes())

    decision = identify(spoofed)

    assert decision.mime == "audio/amr"
    assert decision.accepts(spoofed) is False


def test_amr_magic_without_frame_is_unknown(tmp_path: Path) -> None:
    path = tmp_path / "header-only.amr"
    path.write_bytes(b"#!AMR\n")

    assert identify(path).status == "unknown"


def test_amr_truncated_final_frame_is_unknown(tmp_path: Path) -> None:
    source = _amr_fixture().read_bytes()
    path = tmp_path / "truncated.amr"
    path.write_bytes(source[:-1])

    assert identify(path).status == "unknown"


@pytest.mark.parametrize("frame_count", (4096, 4100, 6000))
def test_long_valid_amr_is_known_after_bounded_frame_sample(
    tmp_path: Path, frame_count: int
) -> None:
    source = _lowrate_amr_fixture().read_bytes()
    frame_size = 1 + 12  # AMR-NB FT0: one TOC byte plus 95-bit payload.
    path = tmp_path / f"lowrate-{frame_count}.amr"
    path.write_bytes(source[: 6 + frame_count * frame_size])

    decision = identify(path)

    assert decision.status == "known"
    assert decision.mime == "audio/amr"


@pytest.mark.parametrize(
    "toc",
    (
        0x3D,  # reserved padding bit set on an otherwise valid FT7 frame
        0x4C,  # AMR-NB reserved/unsupported FT9
    ),
)
def test_amr_invalid_toc_layout_is_unknown(tmp_path: Path, toc: int) -> None:
    source = bytearray(_amr_fixture().read_bytes())
    source[6] = toc
    path = tmp_path / "invalid-toc.amr"
    path.write_bytes(source)

    assert identify(path).status == "unknown"


def test_amr_wideband_magic_is_not_mapped_to_audio_amr(tmp_path: Path) -> None:
    path = tmp_path / "wideband.amr"
    path.write_bytes(b"#!AMR-WB\n" + b"\x3c" + b"\0" * 31)

    assert identify(path).status == "unknown"


@pytest.mark.parametrize(
    ("name", "prefix_length"),
    (
        ("audio-aac.aac", 7),
        ("audio-aiff.aiff", 12),
        ("audio-caf.caf", 8),
        ("audio-wma.wma", 30),
    ),
)
def test_truncated_media_headers_abstain_even_with_audio_suffix(
    tmp_path: Path, name: str, prefix_length: int
) -> None:
    source = _probe_fixture(name)
    truncated = tmp_path / f"truncated-{name}"
    truncated.write_bytes(source.read_bytes()[:prefix_length])

    decision = identify(truncated)

    assert decision.status == "unknown"
    assert decision.mime is None


@pytest.mark.parametrize(("name", "expected_mime"), REAL_AUDIO_CASES)
def test_media_signature_beats_spoofed_extension(
    tmp_path: Path, name: str, expected_mime: str
) -> None:
    source = _probe_fixture(name)
    spoofed = tmp_path / f"{Path(name).stem}.txt"
    spoofed.write_bytes(source.read_bytes())

    decision = identify(spoofed)

    assert decision.mime == expected_mime
    assert decision.accepts(spoofed) is False


def test_adts_invalid_frequency_index_does_not_become_audio_aac(tmp_path: Path) -> None:
    source = bytearray(_probe_fixture("audio-aac.aac").read_bytes())
    source[2] = (source[2] & 0xC3) | 0x3C
    path = tmp_path / "spoof.aac"
    path.write_bytes(source)

    assert identify(path).status == "unknown"


def test_aiff_invalid_comm_chunk_does_not_become_audio_aiff(tmp_path: Path) -> None:
    source = bytearray(_probe_fixture("audio-aiff.aiff").read_bytes())
    struct.pack_into(">I", source, 16, 17)
    path = tmp_path / "spoof.aiff"
    path.write_bytes(source)

    assert identify(path).status == "unknown"


def test_caf_invalid_description_size_does_not_become_audio_caf(tmp_path: Path) -> None:
    source = bytearray(_probe_fixture("audio-caf.caf").read_bytes())
    struct.pack_into(">Q", source, 12, 31)
    path = tmp_path / "spoof.caf"
    path.write_bytes(source)

    assert identify(path).status == "unknown"


def test_wma_oversized_object_count_does_not_become_audio_wma(tmp_path: Path) -> None:
    source = bytearray(_probe_fixture("audio-wma.wma").read_bytes())
    struct.pack_into("<I", source, 24, 0xFFFFFFFF)
    path = tmp_path / "spoof.wma"
    path.write_bytes(source)

    assert identify(path).status == "unknown"
