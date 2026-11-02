"""Window enumeration and geometry.

Uses ``xwininfo``/``xdotool``/``xprop`` when present. On a compositor-limited
Wayland session only the active-window title (via portal or desktop tools) may
be available; in that case the functions degrade to empty results with notes.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

from .. import util
from ..models import Rect, WindowInfo

# Example xwininfo -root -tree line:
#     0x1a0000a "GP DemoTarget": ("electron" "Electron")  640x520+120+100  +120+100
# The first pair after the size is geometry relative to the parent frame; the
# second (trailing) pair is the absolute position on the root window, which is
# what GuidePilot's overlays and coordinates use.
_TREE_RE = re.compile(
    r'^\s+(?P<id>0x[0-9a-fA-F]+)\s+"(?P<title>[^"]*)":\s+'
    r'\((?:"(?P<instance>[^"]*)"\s+"(?P<klass>[^"]*)")?\)\s+'
    r'(?P<w>\d+)x(?P<h>\d+)(?P<rel>[+\-]\d+[+\-]\d+)(?:\s+(?P<abs>[+\-]\d+[+\-]\d+))?',
    re.MULTILINE,
)

_POS_RE = re.compile(r"(?P<x>[+\-]\d+)(?P<y>[+\-]\d+)")

_GEOMETRY_RE = re.compile(
    r"Absolute upper-left X:\s*(-?\d+).*?Absolute upper-left Y:\s*(-?\d+).*?Width:\s*(\d+).*?Height:\s*(\d+)",
    re.DOTALL,
)

_SKIP_CLASSES = {"xfdesktop", "Xfce4-panel", "xfce4-panel", "Desktop", "xfce4-screensaver"}


@dataclass
class WindowListing:
    windows: list[WindowInfo]
    active_id: str | None
    notes: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "windows": [w.to_dict() for w in self.windows],
            "active_id": self.active_id,
            "notes": self.notes,
        }


def _x11_available() -> bool:
    return bool(os.environ.get("DISPLAY")) and bool(util.which("xwininfo"))


def _window_pid(window_id: str) -> int | None:
    xprop = util.which("xprop")
    if not xprop:
        return None
    result = util.run_cmd([xprop, "-id", window_id, "-notype", "_NET_WM_PID"], timeout=2)
    if not result.ok:
        return None
    match = re.search(r"=\s*(\d+)", result.stdout)
    return int(match.group(1)) if match else None


def window_geometry(window_id: str) -> Rect | None:
    """Absolute geometry for a window id (``0x...``)."""
    xwininfo = util.which("xwininfo")
    if not xwininfo:
        return None
    result = util.run_cmd([xwininfo, "-id", window_id], timeout=3)
    if not result.ok:
        return None
    match = _GEOMETRY_RE.search(result.stdout)
    if not match:
        return None
    x, y, w, h = (int(g) for g in match.groups())
    return Rect(x, y, w, h)


def active_window_id() -> str | None:
    if not os.environ.get("DISPLAY"):
        return None
    xdotool = util.which("xdotool")
    if xdotool:
        result = util.run_cmd([xdotool, "getactivewindow"], timeout=3)
        if result.ok and result.stdout.strip():
            try:
                return hex(int(result.stdout.strip()))
            except ValueError:
                return None
    xprop = util.which("xprop")
    if xprop:
        result = util.run_cmd([xprop, "-root", "-notype", "_NET_ACTIVE_WINDOW"], timeout=3)
        match = re.search(r"(0x[0-9a-fA-F]+)", result.stdout or "")
        if match:
            return match.group(1)
    return None


def list_windows(include_utility: bool = False) -> WindowListing:
    """Enumerate mapped top-level windows with geometry."""
    notes: list[str] = []
    windows: list[WindowInfo] = []
    if not _x11_available():
        notes.append(
            "Window enumeration requires X11 tools (xwininfo). On Wayland only screen-level guidance is available."
        )
        return WindowListing([], None, notes)
    xwininfo = util.which("xwininfo")
    assert xwininfo
    result = util.run_cmd([xwininfo, "-root", "-tree"], timeout=5)
    if not result.ok:
        notes.append(f"xwininfo failed: {util.truncated(result.stderr.strip(), 160)}")
        return WindowListing([], None, notes)
    active = active_window_id()
    for match in _TREE_RE.finditer(result.stdout):
        title = match.group("title").strip()
        klass = match.group("klass") or match.group("instance")
        width, height = int(match.group("w")), int(match.group("h"))
        if width < 40 or height < 40:
            continue
        if not include_utility and klass in _SKIP_CLASSES:
            continue
        if not title and klass in _SKIP_CLASSES:
            continue
        window_id = match.group("id")
        position = _POS_RE.match(match.group("abs") or match.group("rel") or "+0+0")
        x = int(position.group("x")) if position else 0
        y = int(position.group("y")) if position else 0
        windows.append(
            WindowInfo(
                id=window_id,
                title=title,
                app_class=klass,
                pid=_window_pid(window_id),
                rect=Rect(x, y, width, height),
                active=bool(active and window_id.lower() == active.lower()),
            )
        )
    if not windows:
        notes.append("No visible application windows were found")
    return WindowListing(windows, active, notes)


def active_window() -> WindowInfo | None:
    listing = list_windows()
    if listing.active_id:
        for window in listing.windows:
            if window.id.lower() == listing.active_id.lower():
                return window
    # Fall back to the first window flagged active by the WM if ids did not match.
    for window in listing.windows:
        if window.active:
            return window
    return None


def monitor_for_rect(rect: Rect, monitors: list) -> str | None:
    """Return the monitor id that contains the largest part of ``rect``."""
    best_id = None
    best_area = 0
    for monitor in monitors:
        mrect = monitor.rect
        inter_w = min(rect.right, mrect.right) - max(rect.x, mrect.x)
        inter_h = min(rect.bottom, mrect.bottom) - max(rect.y, mrect.y)
        area = max(0, inter_w) * max(0, inter_h)
        if area > best_area:
            best_area = area
            best_id = monitor.id
    return best_id
