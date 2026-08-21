# Progress

`neocortex.progress` owns the backend-neutral event contract and its terminal,
headless and recording adapters. Product capabilities emit `ProgressEvent`;
interfaces select a reporter without importing UI code into the event schema.

```python
from neocortex.progress import ProgressEvent, RichProgress

with RichProgress() as progress:
    progress(ProgressEvent("example", "read", "Reading", 50, 100, "files"))
```

`ProgressEvent.metrics` carries structured `ProgressMetric` counters. The Rich
adapter renders them, while `LineProgress`, `NullProgress` and
`RecordingProgress` consume the same events without parsing terminal text.
This package starts no work by itself.
