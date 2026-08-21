"""Allow development launches with ``python -m neocortex.interface``."""

from .application.app import main


if __name__ == "__main__":
    raise SystemExit(main())
