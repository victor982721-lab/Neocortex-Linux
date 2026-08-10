"""Behavioral contracts for bounded, visual-first FFprobe decoding."""

from __future__ import annotations

import pytest

from _04_Nucleo_Operativo.video_models import VideoProcessingError
from _04_Nucleo_Operativo.video_probe import decode_video_probe


def test_visual_only_video_is_a_valid_video_resource() -> None:
    result = decode_video_probe(
        {
            "format": {"duration": "12.500", "format_name": "matroska,webm"},
            "streams": [
                {
                    "index": 0,
                    "codec_type": "video",
                    "codec_name": "vp9",
                    "width": 1920,
                    "height": 1080,
                    "avg_frame_rate": "30000/1001",
                    "tags": {"rotate": "90"},
                },
                {
                    "index": 1,
                    "codec_type": "subtitle",
                    "codec_name": "webvtt",
                    "tags": {"language": "deu"},
                },
            ],
            "chapters": [{"id": 0}],
        }
    )

    assert result.duration_seconds == 12.5
    assert result.video_streams == 1
    assert result.audio_streams == 0
    assert result.video[0].rotation_degrees == 90
    assert result.video[0].frame_rate == pytest.approx(29.97002997)
    assert result.subtitles[0].language == "deu"
    assert result.chapters == 1


def test_audio_and_multiple_video_tracks_are_counted_without_guessing() -> None:
    result = decode_video_probe(
        {
            "format": {"duration": 5, "format_name": "mov,mp4"},
            "streams": [
                {
                    "index": 0,
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 1280,
                    "height": 720,
                    "r_frame_rate": "25/1",
                },
                {
                    "index": 1,
                    "codec_type": "video",
                    "codec_name": "mjpeg",
                    "width": 320,
                    "height": 180,
                    "r_frame_rate": "1/1",
                },
                {"index": 2, "codec_type": "audio", "codec_name": "aac"},
            ],
            "chapters": [],
        }
    )

    assert result.video_streams == 2
    assert result.audio_streams == 1
    assert [stream.index for stream in result.video] == [0, 1]


def test_non_video_media_fails_with_a_specific_reviewable_reason() -> None:
    with pytest.raises(VideoProcessingError) as raised:
        decode_video_probe(
            {
                "format": {"duration": "3"},
                "streams": [{"index": 0, "codec_type": "audio", "codec_name": "opus"}],
                "chapters": [],
            }
        )

    assert raised.value.code == "media_without_video_stream"
    assert raised.value.recommendation == "manual_review"
    assert not raised.value.retryable


@pytest.mark.parametrize(
    "payload",
    (
        {"format": {"duration": "nan"}, "streams": []},
        {"format": {"duration": "1"}, "streams": "not-an-array"},
        {
            "format": {"duration": "1"},
            "streams": [
                {
                    "index": 0,
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 0,
                    "height": 1080,
                }
            ],
            "chapters": [],
        },
    ),
)
def test_invalid_or_ambiguous_video_probe_fails_closed(payload: object) -> None:
    with pytest.raises(VideoProcessingError):
        decode_video_probe(payload)
