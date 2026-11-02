"""Accessibility and window-management adapters."""

from .window import list_windows, active_window, window_geometry  # noqa: F401
from .atspi import AtspiClient, atspi_available  # noqa: F401
from .ocr import OcrEngine  # noqa: F401

__all__ = ["list_windows", "active_window", "window_geometry", "AtspiClient", "atspi_available", "OcrEngine"]
