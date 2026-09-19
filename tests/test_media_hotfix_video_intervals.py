"""Differential timestamp policy and bounded access, proposed for local Codex."""

from __future__ import annotations

import math

import pytest

from neocortex.capabilities.formats.video import frames


def _reference_interval(duration, interval, frame_rate):
    """The base-commit implementation, used only for small differential cases."""
    duration_ms = max(0, math.floor(duration * 1000))
    guard = min(250, max(1, duration_ms // 10)) if duration_ms else 0
    if frame_rate is not None:
        frame_guard = duration_ms if frame_rate * duration <= 1.0 else math.ceil(1000 / frame_rate)
        guard = max(guard, frame_guard)
    final = max(0, duration_ms - guard)
    if frame_rate is not None:
        frame_slots = math.floor(duration * frame_rate + 0.5)
        if frame_slots > 0:
            final = min(final, max(0, math.floor((frame_slots - 1) * 1000 / frame_rate)))
    interval_ms = max(1, round(interval * 1000))
    values = list(range(0, final + 1, interval_ms))
    if not values or final - values[-1] >= min(interval_ms // 2, 1000):
        values.append(final)
    return tuple(sorted(set(values)))


@pytest.mark.parametrize("duration", (0, 0.001, 0.1, 1, 1.001, 2.25, 61))
@pytest.mark.parametrize("interval", (0.001, 0.002, 0.0025, 0.5, 1, 30))
@pytest.mark.parametrize("frame_rate", (None, 0.2, 1, 24, 29.97))
def test_arithmetic_intervals_preserve_endpoints_indexing_and_slices(duration, interval, frame_rate):
    actual = frames._interval_timestamps(duration, interval, frame_rate=frame_rate)
    expected = _reference_interval(duration, interval, frame_rate)
    assert tuple(actual) == expected
    assert len(actual) == len(expected)
    assert actual[0] == expected[0]
    assert actual[-1] == expected[-1]
    for section in (slice(None), slice(None, None, -1), slice(1, -1, 2), slice(-3, None)):
        assert actual[section] == expected[section]
    for index in (len(actual), -len(actual) - 1):
        with pytest.raises(IndexError):
            actual[index]
    with pytest.raises(TypeError):
        actual[0.0]


@pytest.mark.parametrize("max_frames", (1, 2, 3, 4, 8, 48, 256))
@pytest.mark.parametrize("duration,interval", ((0, 0.001), (1, 0.5), (61, 0.0025), (100, 30)))
@pytest.mark.parametrize("discovery", (False, True))
def test_full_frame_plan_matches_materialized_reference(monkeypatch, max_frames, duration, interval, discovery):
    arguments = {
        "duration_seconds": duration,
        "max_frames": max_frames,
        "interval_seconds": interval,
        "scene_timestamps_ms": (0, 100, 1000) if discovery else (),
        "keyframe_timestamps_ms": (0, 250, 1000) if discovery else (),
        "frame_rate": 24.0,
    }
    actual = frames.build_frame_plan(**arguments)
    monkeypatch.setattr(
        frames, "_interval_timestamps",
        lambda duration_seconds, interval_seconds, *, frame_rate=None:
            _reference_interval(duration_seconds, interval_seconds, frame_rate),
    )
    assert actual == frames.build_frame_plan(**arguments)


def test_six_hour_millisecond_plan_does_not_iterate_twenty_million_ticks(monkeypatch):
    original = frames._IntervalTimestamps.__getitem__
    accesses = 0

    def bounded_access(self, index):
        nonlocal accesses
        accesses += 1
        assert accesses <= 300
        return original(self, index)

    def forbidden_iteration(self):
        raise AssertionError("long interval grids must be sampled by index")

    monkeypatch.setattr(frames._IntervalTimestamps, "__getitem__", bounded_access)
    monkeypatch.setattr(frames._IntervalTimestamps, "__iter__", forbidden_iteration)
    plan = frames.build_frame_plan(
        duration_seconds=6 * 60 * 60,
        max_frames=256,
        interval_seconds=0.001,
    )
    assert len(plan) == 256
    assert plan[0].timestamp_ms == 0
    assert plan[-1].timestamp_ms == 21_599_750
    assert accesses <= 256


def test_empty_discovery_donates_budget_without_expanding_large_grid():
    config = frames.VideoFrameSamplingConfig(interval_seconds=0.001)
    config.validate()
    grid = frames._interval_timestamps(6 * 60 * 60, config.interval_seconds)
    assert len(grid) == 21_599_751
    assert grid[-1] == 21_599_750
    assert len(frames.build_frame_plan(
        duration_seconds=6 * 60 * 60,
        max_frames=config.max_frames,
        interval_seconds=config.interval_seconds,
    )) == config.max_frames
