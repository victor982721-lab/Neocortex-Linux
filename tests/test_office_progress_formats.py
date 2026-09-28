"""Office format counters describe actual work, independently of aggregate rows."""

from types import SimpleNamespace

from neocortex.capabilities.formats.office.route import (
    OfficeRoute,
    _OfficeCandidateOutcome,
    _OfficeRunMetrics,
)


def test_format_progress_preserves_cache_new_and_unknown_total() -> None:
    metrics = _OfficeRunMetrics(3, 3, 3, processed=3, cache_hits=1, extracted=1, errors=1)
    metrics.record_format("xlsx", cached=True)
    metrics.record_format("xlsx", cached=False, outcome=_OfficeCandidateOutcome(extracted=1))
    metrics.record_format("pptx", cached=False, outcome=_OfficeCandidateOutcome(errors=1))
    events = []
    route = OfficeRoute.__new__(OfficeRoute)
    route.progress = events.append
    route.memory_gate = SimpleNamespace(wait_count=0)
    route._report(metrics)
    formats = {event.phase: event for event in events if event.phase.startswith("format:")}
    assert formats["format:xlsx"].total is None
    assert formats["format:xlsx"].completed == 2
    assert {m.name: m.value for m in formats["format:xlsx"].metrics} == {
        "cache_hits": 1, "cached_errors": 0, "new_work": 1, "errors": 0,
    }
    assert {m.name: m.value for m in formats["format:pptx"].metrics}["errors"] == 1
    events.clear()
    route._report(metrics, finished=True)
    assert sum(event.completed for event in events if event.phase.startswith("format:")) == 3
    assert all(event.total == event.completed for event in events)
