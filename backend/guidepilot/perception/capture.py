"""Screen capture backends.

Design rules:

* Capture only happens while the user has explicitly enabled screen sharing.
* Frames live in memory or in a scratch directory and are deleted immediately
  after use; nothing is written to the database or to persistent storage.
* The chosen backend is the best available one for the detected session, and
  its limitations are reported rather than hidden.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import util
from ..models import Rect
from .change import crop_png, image_size
from .display import DisplayInfo

CAPTURE_TIMEOUT = 8.0


class CaptureError(RuntimeError):
    """Capture failed. ``reason`` is a stable machine-readable code."""

    def __init__(self, reason: str, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.hint = hint

    def to_dict(self) -> dict[str, Any]:
        return {"reason": self.reason, "message": self.message, "hint": self.hint}


@dataclass
class CaptureResult:
    png: bytes
    width: int
    height: int
    scale: float
    backend: str
    rect: Rect
    captured_at: int
    window_id: str | None = None

    def cleanup(self) -> None:
        # Frames are byte buffers; nothing to delete. Present for interface symmetry.
        return


def _run_bytes(argv: list[str], timeout: float = CAPTURE_TIMEOUT) -> tuple[bytes, str]:
    import subprocess

    try:
        completed = subprocess.run(argv, capture_output=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise CaptureError("no_backend", str(exc), "Install imagemagick or x11-apps") from exc
    except subprocess.TimeoutExpired as exc:
        raise CaptureError("timeout", f"{argv[0]} timed out after {timeout}s") from exc
    if completed.returncode != 0:
        stderr = (completed.stderr or b"").decode("utf-8", "replace").strip()
        raise CaptureError("failed", f"{os.path.basename(argv[0])} failed: {util.truncated(stderr, 200)}")
    if not completed.stdout:
        raise CaptureError("failed", f"{os.path.basename(argv[0])} produced no image data")
    return completed.stdout, stderr if False else ""


class CaptureBackend:
    """Interface implemented by every capture backend."""

    name = "none"
    supports_window_capture = False

    def available(self) -> tuple[bool, str]:
        raise NotImplementedError

    def capture_root(self) -> CaptureResult:
        raise NotImplementedError

    def capture_window(self, window_id: str) -> CaptureResult:
        raise NotImplementedError

    def capture_rect(self, rect: Rect) -> CaptureResult:
        return self._crop_from_root(rect)

    def _crop_from_root(self, rect: Rect) -> CaptureResult:
        root = self.capture_root()
        cropped = crop_png(
            root.png,
            (
                int((rect.x - root.rect.x) * root.scale),
                int((rect.y - root.rect.y) * root.scale),
                int((rect.x - root.rect.x + rect.width) * root.scale),
                int((rect.y - root.rect.y + rect.height) * root.scale),
            ),
        )
        if cropped is None:
            raise CaptureError(
                "pillow_missing",
                "Region capture needs Pillow to crop the frame",
                "Install python3-pil to enable cropped captures",
            )
        width, height = image_size(cropped)
        return CaptureResult(
            png=cropped,
            width=width,
            height=height,
            scale=root.scale,
            backend=self.name,
            rect=rect,
            captured_at=util.now_ms(),
        )


class X11ImportBackend(CaptureBackend):
    """ImageMagick ``import`` based X11 capture."""

    name = "x11-imagemagick"
    supports_window_capture = True

    def __init__(self, display: str = "") -> None:
        self.display = display or os.environ.get("DISPLAY", "")
        self.binary = util.which("import")

    def available(self) -> tuple[bool, str]:
        if not self.display:
            return False, "No X11 DISPLAY in the environment"
        if not self.binary:
            return False, "ImageMagick 'import' not found (install imagemagick)"
        probe = util.run_cmd([self.binary, "-window", "root", "-silent", "/dev/null"], timeout=6)
        if probe.code != 0:
            message = (probe.stderr or "").strip()
            if "unable to open display" in message.lower() or "could not open display" in message.lower():
                return False, "Cannot open the X11 display (is DISPLAY reachable for this user?)"
            return False, util.truncated(message or "import probe failed", 200)
        return True, ""

    def _screen_rect(self) -> Rect:
        from .display import list_monitors

        monitors, _ = list_monitors()
        if not monitors:
            return Rect(0, 0, 0, 0)
        x0 = min(m.x for m in monitors)
        y0 = min(m.y for m in monitors)
        x1 = max(m.x + m.width for m in monitors)
        y1 = max(m.y + m.height for m in monitors)
        return Rect(x0, y0, x1 - x0, y1 - y0)

    def capture_root(self) -> CaptureResult:
        assert self.binary
        png, _ = _run_bytes([self.binary, "-window", "root", "-silent", "png:-"])
        width, height = image_size(png)
        rect = self._screen_rect()
        scale = 1.0
        if rect.width and width:
            scale = width / float(rect.width)
        elif width:
            rect = Rect(0, 0, width, height)
        return CaptureResult(
            png=png,
            width=width,
            height=height,
            scale=scale,
            backend=self.name,
            rect=rect,
            captured_at=util.now_ms(),
        )

    def capture_window(self, window_id: str) -> CaptureResult:
        assert self.binary
        png, _ = _run_bytes([self.binary, "-window", window_id, "-silent", "png:-"])
        width, height = image_size(png)
        from ..accessibility.window import window_geometry

        geometry = window_geometry(window_id)
        rect = geometry if geometry else Rect(0, 0, width, height)
        scale = 1.0
        if rect.width and width:
            scale = width / float(rect.width)
        return CaptureResult(
            png=png,
            width=width,
            height=height,
            scale=scale,
            backend=self.name,
            rect=rect,
            captured_at=util.now_ms(),
            window_id=window_id,
        )


class X11XwdBackend(CaptureBackend):
    """``xwd`` + ImageMagick convert fallback for minimal installs."""

    name = "x11-xwd"
    supports_window_capture = True

    def __init__(self, display: str = "") -> None:
        self.display = display or os.environ.get("DISPLAY", "")
        self.xwd = util.which("xwd")
        self.convert = util.which("convert") or util.which("magick")

    def available(self) -> tuple[bool, str]:
        if not self.display:
            return False, "No X11 DISPLAY"
        if not self.xwd or not self.convert:
            return False, "xwd and convert are both required for the xwd backend"
        return True, ""

    def capture_root(self) -> CaptureResult:
        assert self.xwd and self.convert
        with tempfile.NamedTemporaryFile(suffix=".xwd", delete=False) as handle:
            temp_path = Path(handle.name)
        try:
            dump = util.run_cmd([self.xwd, "-root", "-silent", "-out", str(temp_path)], timeout=CAPTURE_TIMEOUT)
            if not dump.ok:
                raise CaptureError("failed", util.truncated(dump.stderr.strip() or "xwd failed", 200))
            png, _ = _run_bytes([self.convert, str(temp_path), "png:-"])
        finally:
            with contextlib.suppress(OSError):
                temp_path.unlink()
        width, height = image_size(png)
        rect = self._screen_rect()
        scale = (width / rect.width) if rect.width and width else 1.0
        return CaptureResult(png, width, height, scale, self.name, rect, util.now_ms())

    def _screen_rect(self) -> Rect:
        monitors, _ = _monitors()
        if not monitors:
            return Rect(0, 0, 0, 0)
        return Rect(
            min(m.x for m in monitors),
            min(m.y for m in monitors),
            max(m.x + m.width for m in monitors) - min(m.x for m in monitors),
            max(m.y + m.height for m in monitors) - min(m.y for m in monitors),
        )

    def capture_window(self, window_id: str) -> CaptureResult:
        assert self.xwd and self.convert
        with tempfile.NamedTemporaryFile(suffix=".xwd", delete=False) as handle:
            temp_path = Path(handle.name)
        try:
            dump = util.run_cmd(
                [self.xwd, "-id", window_id, "-silent", "-out", str(temp_path)], timeout=CAPTURE_TIMEOUT
            )
            if not dump.ok:
                raise CaptureError("failed", util.truncated(dump.stderr.strip() or "xwd window capture failed", 200))
            png, _ = _run_bytes([self.convert, str(temp_path), "png:-"])
        finally:
            with contextlib.suppress(OSError):
                temp_path.unlink()
        from ..accessibility.window import window_geometry

        width, height = image_size(png)
        rect = window_geometry(window_id) or Rect(0, 0, width, height)
        scale = (width / rect.width) if rect.width and width else 1.0
        return CaptureResult(png, width, height, scale, self.name, rect, util.now_ms(), window_id=window_id)


def _monitors():
    from .display import list_monitors

    return list_monitors()


class WaylandCaptureBackend(CaptureBackend):
    """Wayland-aware backend that reports its real capability.

    A full xdg-desktop-portal ScreenCast + PipeWire implementation is out of
    scope for this build. Where ``grim`` exists (wlroots compositors) still
    capture is possible; anything else reports a capability error instead of
    pretending to capture.
    """

    name = "wayland-grim"
    supports_window_capture = False

    def available(self) -> tuple[bool, str]:
        if not os.environ.get("WAYLAND_DISPLAY"):
            return False, "No WAYLAND_DISPLAY"
        if not util.which("grim"):
            return False, (
                "Wayland capture requires xdg-desktop-portal ScreenCast (not implemented) or grim for wlroots compositors"
            )
        return True, ""

    def capture_root(self) -> CaptureResult:
        grim = util.which("grim")
        if not grim:
            raise CaptureError(
                "unsupported",
                "Wayland capture is not available in this session",
                "Install grim (wlroots) or wait for portal-based capture support",
            )
        png, _ = _run_bytes([grim, "-t", "png", "-"])
        width, height = image_size(png)
        return CaptureResult(png, width, height, 1.0, self.name, Rect(0, 0, width, height), util.now_ms())

    def capture_window(self, window_id: str) -> CaptureResult:
        raise CaptureError(
            "unsupported",
            "Wayland compositors do not expose per-window capture for arbitrary applications",
            "Use full-monitor guidance mode on Wayland",
        )


class NullBackend(CaptureBackend):
    """Backend used when nothing works: fails loudly with an explanation."""

    name = "none"

    def __init__(self, reason: str, hint: str = "") -> None:
        self.reason = reason
        self.hint = hint

    def available(self) -> tuple[bool, str]:
        return False, self.reason

    def capture_root(self) -> CaptureResult:
        raise CaptureError("no_backend", self.reason, self.hint)

    def capture_window(self, window_id: str) -> CaptureResult:
        raise CaptureError("no_backend", self.reason, self.hint)


def create_backend(info: DisplayInfo | None = None) -> CaptureBackend:
    """Select the most capable available capture backend."""
    candidates: list[CaptureBackend] = []
    if "x11-imagemagick" in (info.capture_backends if info else []) or not info:
        candidates.append(X11ImportBackend())
    candidates.append(X11XwdBackend())
    if info and info.session_type == "wayland":
        candidates.append(WaylandCaptureBackend())
    hints: list[str] = []
    for candidate in candidates:
        ok, reason = candidate.available()
        if ok:
            return candidate
        if reason:
            hints.append(reason)
    return NullBackend(
        hints[0] if hints else "No screen capture backend is available",
        "Install imagemagick (`sudo apt install imagemagick`) for X11 capture",
    )
