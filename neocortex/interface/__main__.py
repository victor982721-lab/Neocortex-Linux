"""Allow development launches with ``python -m neocortex.interface``."""

from .entrypoint import entrypoint


if __name__ == "__main__":
    raise SystemExit(entrypoint())
