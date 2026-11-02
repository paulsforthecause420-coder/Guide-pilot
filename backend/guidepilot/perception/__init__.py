"""Screen perception: display detection, capture, change detection, observation."""

from .display import DisplayInfo, detect_display, list_monitors  # noqa: F401
from .capture import CaptureBackend, CaptureError, create_backend  # noqa: F401
from .observer import ScreenObserver  # noqa: F401

__all__ = [
    "DisplayInfo",
    "detect_display",
    "list_monitors",
    "CaptureBackend",
    "CaptureError",
    "create_backend",
    "ScreenObserver",
]
