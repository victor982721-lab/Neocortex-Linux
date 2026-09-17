"""Shared bounds for video arguments and processing, without engine imports."""

MAX_VIDEO_FRAMES = 256
MAX_VIDEO_FRAME_PIXELS = 40_000_000
# Keep the default per-worker reservation within the minimum one-gibibyte
# adaptive global budget.  A larger fixed default can be rejected before a
# small, valid fixture is even admitted on a constrained Linux cgroup.
DEFAULT_VIDEO_WORKER_MEMORY_BYTES = 1024 * 1024 * 1024

__all__ = (
    "DEFAULT_VIDEO_WORKER_MEMORY_BYTES",
    "MAX_VIDEO_FRAMES",
    "MAX_VIDEO_FRAME_PIXELS",
)
