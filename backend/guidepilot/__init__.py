"""GuidePilot — screen-aware desktop instructor core services.

The Python side owns everything that touches the operating system:

* display/session capability detection
* screen capture and change detection
* accessibility (AT-SPI) and OCR perception
* element grounding and coordinate transforms
* task orchestration, verification and persistence
* AI provider gateway, security and privacy controls

It is deliberately dependency-light (Python standard library plus Pillow when
available) so that it runs on a stock Debian/Kali desktop without a virtualenv.
"""

__all__ = ["__version__", "APP_NAME", "APP_TAGLINE"]

__version__ = "1.0.0"
APP_NAME = "GuidePilot"
APP_TAGLINE = "See it. Learn it. Do it."
