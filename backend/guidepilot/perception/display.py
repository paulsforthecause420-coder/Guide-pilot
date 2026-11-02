"""Display server detection, monitor enumeration and overlay capability reporting.

GuidePilot must never claim capability it does not have. Everything in this
module reports *observed* facts about the current session:

* whether we are on X11 or Wayland
* the desktop environment / window manager / compositor in use
* every monitor with its logical geometry and scale
* which capture backends are actually usable
* whether a transparent always-on-top overlay is expected to work

Wayland is a first-class *detected* environment but overlays and capture are
restricted by the compositor's security model. We therefore report the
limitations instead of silently falling back to a broken path.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from typing import Any

from .. import util
from ..models import Monitor, Rect

_XRANDR_MONITOR_RE = re.compile(
    r"^\s*(?P<index>\d+):\s+(?P<primary>\+?\*?)?\+?(?P<name>\S+)\s+"
    r"(?P<w>\d+)/\d+x(?P<h>\d+)/\d+\+(?P<x>-?\d+)\+(?P<y>-?\d+)\s+(?P<output>\S+)",
    re.MULTILINE,
)

_XDPYINFO_RE = re.compile(r"dimensions:\s+(?P<w>\d+)x(?P<h>\d+)\s+pixels", re.IGNORECASE)

# Processes that own the X compositing selection on common desktops.
_COMPOSITORS = {
    "xfwm4": "xfwm4",
    "mutter": "mutter",
    "kwin_x11": "kwin_x11",
    "kwin_wayland": "kwin_wayland",
    "gnome-shell": "gnome-shell",
    "picom": "picom",
    "compton": "compton",
    "compiz": "compiz",
    "sway": "sway",
    "hyprland": "hyprland",
}


@dataclass
class DisplayInfo:
    session_type: str  # x11 | wayland | unknown
    display: str
    wayland_display: str
    desktop: str
    window_manager: str
    compositor: str
    monitors: list[Monitor] = field(default_factory=list)
    overlay_supported: bool = False
    overlay_backend: str = "none"
    overlay_notes: list[str] = field(default_factory=list)
    capture_backends: list[str] = field(default_factory=list)
    capture_notes: list[str] = field(default_factory=list)
    accessibility_backend: str = "unavailable"
    oki: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_type": self.session_type,
            "display": self.display,
            "wayland_display": self.wayland_display,
            "desktop": self.desktop,
            "window_manager": self.window_manager,
            "compositor": self.compositor,
            "monitors": [m.to_dict() for m in self.monitors],
            "overlay_supported": self.overlay_supported,
            "overlay_backend": self.overlay_backend,
            "overlay_notes": self.overlay_notes,
            "capture_backends": self.capture_backends,
            "capture_notes": self.capture_notes,
            "accessibility_backend": self.accessibility_backend,
            "ok": self.oki,
        }

    @property
    def primary_monitor(self) -> Monitor | None:
        for monitor in self.monitors:
            if monitor.primary:
                return monitor
        return self.monitors[0] if self.monitors else None

    def monitor_for_point(self, x: int, y: int) -> Monitor | None:
        for monitor in self.monitors:
            if monitor.rect.contains_point(x, y):
                return monitor
        return None

    def virtual_bounds(self) -> Rect:
        if not self.monitors:
            return Rect(0, 0, 0, 0)
        x0 = min(m.x for m in self.monitors)
        y0 = min(m.y for m in self.monitors)
        x1 = max(m.x + m.width for m in self.monitors)
        y1 = max(m.y + m.height for m in self.monitors)
        return Rect(x0, y0, x1 - x0, y1 - y0)


def _detect_compositor() -> str:
    for proc, label in _COMPOSITORS.items():
        if util.which("pgrep") and util.run_cmd(["pgrep", "-x", proc], timeout=2).ok:
            return label
    return "unknown"


def _detect_window_manager() -> str:
    if not os.environ.get("DISPLAY"):
        return "unknown"
    command = util.which("xprop")
    if not command:
        return "unknown"
    result = util.run_cmd([command, "-root", "-notype", "_NET_SUPPORTING_WM_CHECK"], timeout=3)
    if not result.ok:
        return "unknown"
    # xprop prints the window id; the WM name lives on that window.
    match = re.search(r"(0x[0-9a-f]+)", result.stdout)
    if not match:
        return "unknown"
    name = util.run_cmd([command, "-id", match.group(1), "-notype", "_NET_WM_NAME"], timeout=3)
    if name.ok:
        value = name.stdout.split("=", 1)[-1].strip().strip('"')
        if value:
            return value
    return "unknown"


def parse_xrandr_monitors(output: str) -> list[Monitor]:
    """Parse ``xrandr --listmonitors`` output into Monitor objects."""
    monitors: list[Monitor] = []
    total_width = 0
    for match in _XRANDR_MONITOR_RE.finditer(output or ""):
        width = int(match.group("w"))
        height = int(match.group("h"))
        monitors.append(
            Monitor(
                id=f"monitor-{len(monitors)}",
                name=match.group("name"),
                x=int(match.group("x")),
                y=int(match.group("y")),
                width=width,
                height=height,
                scale=1.0,
                primary="*" in (match.group("primary") or ""),
            )
        )
        total_width += width
    if monitors and not any(m.primary for m in monitors):
        monitors[0].primary = True
    return monitors


def list_monitors() -> tuple[list[Monitor], list[str]]:
    """Enumerate monitors. Returns (monitors, notes)."""
    notes: list[str] = []
    xrandr = util.which("xrandr")
    if xrandr and os.environ.get("DISPLAY"):
        result = util.run_cmd([xrandr, "--listmonitors"], timeout=5)
        if result.ok:
            monitors = parse_xrandr_monitors(result.stdout)
            if monitors:
                return monitors, notes
            notes.append("xrandr reported no monitors; falling back to X screen size")
        else:
            notes.append(f"xrandr failed: {util.truncated(result.stderr.strip(), 120)}")

    xdpyinfo = util.which("xdpyinfo")
    if xdpyinfo and os.environ.get("DISPLAY"):
        result = util.run_cmd([xdpyinfo], timeout=5)
        match = _XDPYINFO_RE.search(result.stdout or "")
        if result.ok and match:
            monitor = Monitor(
                id="monitor-0",
                name="X11-screen",
                x=0,
                y=0,
                width=int(match.group("w")),
                height=int(match.group("h")),
                primary=True,
            )
            notes.append("Monitor geometry derived from total X screen size; multi-monitor offsets unknown")
            return [monitor], notes

    notes.append("No X11 display available; monitor geometry unavailable")
    return [], notes


def detect_session_type() -> str:
    session = os.environ.get("XDG_SESSION_TYPE", "").strip().lower()
    if session in {"x11", "wayland"}:
        return session
    if os.environ.get("WAYLAND_DISPLAY"):
        return "wayland"
    if os.environ.get("DISPLAY"):
        return "x11"
    return "unknown"


def _binary_present(*names: str) -> bool:
    return any(util.which(name) for name in names)


def detect_display(accessibility_backend: str = "unavailable") -> DisplayInfo:
    """Inspect the current graphical session."""
    session_type = detect_session_type()
    desktop = os.environ.get("XDG_CURRENT_DESKTOP") or os.environ.get("DESKTOP_SESSION") or "unknown"
    monitors: list[Monitor] = []
    notes: list[str] = []
    capture_backends: list[str] = []
    capture_notes: list[str] = []
    overlay_notes: list[str] = []

    compositor = _detect_compositor()
    window_manager = _detect_window_manager()

    if session_type == "x11" or (session_type == "wayland" and os.environ.get("DISPLAY")):
        monitors, monitor_notes = list_monitors()
        notes.extend(monitor_notes)
        if _binary_present("import"):
            capture_backends.append("x11-imagemagick")
        if _binary_present("xwd") and (_binary_present("convert") or _binary_present("magick")):
            capture_backends.append("x11-xwd")
        if _binary_present("ffmpeg"):
            capture_backends.append("x11grab")
        if not capture_backends:
            capture_notes.append(
                "No X11 capture tool found. Install imagemagick (`sudo apt install imagemagick`) or x11-apps + imagemagick."
            )

    if session_type == "wayland":
        if _binary_present("grim") and _binary_present("slurp"):
            capture_backends.append("wayland-grim")
        portal = _binary_present("xdg-desktop-portal") or _binary_present("gdbus")
        capture_notes.append(
            "Wayland session detected. Portal-based capture (xdg-desktop-portal ScreenCast + PipeWire) "
            "requires a user approval dialog and is not implemented in this build; "
            + ("grim/slurp found, so wlroots capture may work via grim." if "wayland-grim" in capture_backends else "grim/slurp are not installed.")
        )
        if not portal:
            capture_notes.append("xdg-desktop-portal was not found; Wayland capture is unavailable.")
        overlay_notes.append(
            "Wayland does not let ordinary applications position always-on-top transparent overlay windows. "
            "GuidePilot drives the XWayland overlay path if XWayland is present, and otherwise shows highlights "
            "inside the assistant window while reporting overlay as unsupported. Global click-through overlays "
            "require a compositor-specific protocol (e.g. wlr-layer-shell) that is not enabled here."
        )

    if session_type == "x11":
        overlay_backend = "electron-x11-transparent"
        overlay_notes.append(
            "X11 transparent always-on-top overlay is supported. Requires a running compositor for per-pixel "
            f"transparency (detected: {compositor})."
        )
        overlay_supported = True
    elif session_type == "wayland":
        overlay_backend = "xwayland-overlay" if os.environ.get("DISPLAY") else "in-window-only"
        overlay_supported = bool(os.environ.get("DISPLAY"))
    else:
        overlay_backend = "none"
        overlay_supported = False
        overlay_notes.append("No graphical session detected; overlay highlighting is unavailable.")

    if compositor == "unknown" and session_type == "x11":
        overlay_notes.append(
            "No known compositor process detected; the overlay may render with a solid background instead of transparency."
        )

    return DisplayInfo(
        session_type=session_type,
        display=os.environ.get("DISPLAY", ""),
        wayland_display=os.environ.get("WAYLAND_DISPLAY", ""),
        desktop=desktop,
        window_manager=window_manager,
        compositor=compositor,
        monitors=monitors,
        overlay_supported=overlay_supported,
        overlay_backend=overlay_backend,
        overlay_notes=overlay_notes,
        capture_backends=capture_backends,
        capture_notes=capture_notes,
        accessibility_backend=accessibility_backend,
        oki=bool(monitors) and bool(capture_backends),
    )
