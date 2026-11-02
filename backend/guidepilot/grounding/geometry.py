"""Coordinate transforms between capture images, model output and the desktop.

Four coordinate spaces exist and are never mixed implicitly:

``physical_screen``
    X11 root-window coordinates (what the overlay and pointer use). Origin is
    the virtual screen origin, which can be negative on multi-monitor layouts.
``capture_pixels``
    Pixels of the captured frame. Equal to ``physical_screen`` scaled by
    ``frame.scale`` and offset by the capture rect origin.
``normalized_image``
    ``[0, 1]`` fractions of the captured frame, the usual vision-model output.
``logical`` (Electron/DIP)
    Logical pixels used for window bounds. On a 1x display identical to
    physical pixels; with X11 scaling the ratio is ``frame.scale``.

The rule enforced here: a rectangle is only ever produced from a *validated*
transform. ``validate_rect_on_screens`` rejects boxes that are outside every
monitor, degenerate, or implausibly large, so a hallucinated coordinate cannot
turn into a confident-looking highlight.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from ..models import CoordinateSpace, Monitor, Rect


class TransformError(ValueError):
    """Raised when a coordinate cannot be mapped with confidence."""


@dataclass
class CaptureGeometry:
    """Describes how a frame relates to the physical screen."""

    rect: Rect
    width: int
    height: int
    scale: float = 1.0

    @property
    def scale_x(self) -> float:
        if self.width <= 0 or self.rect.width <= 0:
            raise TransformError("capture has no usable size")
        return self.width / float(self.rect.width)

    @property
    def scale_y(self) -> float:
        if self.height <= 0 or self.rect.height <= 0:
            raise TransformError("capture has no usable size")
        return self.height / float(self.rect.height)

    def is_consistent(self, tolerance: float = 0.02) -> bool:
        """True when the frame's aspect ratio matches the captured region."""
        if self.rect.width <= 0 or self.rect.height <= 0 or self.width <= 0 or self.height <= 0:
            return False
        expected = self.rect.width / float(self.rect.height)
        actual = self.width / float(self.height)
        return abs(expected - actual) <= max(tolerance, expected * tolerance)


def capture_to_screen(
    rect_in_capture: Sequence[float],
    geometry: CaptureGeometry,
) -> Rect:
    """Map a rectangle expressed in capture pixels to physical screen pixels."""
    x, y, w, h = (float(v) for v in rect_in_capture)
    scale_x = geometry.scale_x
    scale_y = geometry.scale_y
    return Rect(
        int(round(geometry.rect.x + x / scale_x)),
        int(round(geometry.rect.y + y / scale_y)),
        max(1, int(round(w / scale_x))),
        max(1, int(round(h / scale_y))),
    )


def screen_to_capture(rect: Rect, geometry: CaptureGeometry) -> Rect:
    """Inverse of :func:`capture_to_screen`."""
    scale_x = geometry.scale_x
    scale_y = geometry.scale_y
    return Rect(
        int(round((rect.x - geometry.rect.x) * scale_x)),
        int(round((rect.y - geometry.rect.y) * scale_y)),
        max(1, int(round(rect.width * scale_x))),
        max(1, int(round(rect.height * scale_y))),
    )


def normalized_to_screen(
    box: Sequence[float],
    geometry: CaptureGeometry,
) -> Rect:
    """Map ``[x0, y0, x1, y1]`` fractions in ``[0, 1]`` to physical pixels.

    Accepts either corner form (x0,y0,x1,y1) or (x,y,w,h) when the third and
    fourth values stay inside the unit square and would exceed the box origin —
    models produce both, so both are handled deterministically.
    """
    if len(box) != 4:
        raise TransformError("normalized box must have four values")
    x0, y0, a, b = (float(v) for v in box)
    for value in (x0, y0, a, b):
        if value < -0.02 or value > 1.02:
            raise TransformError(f"normalized value {value} outside [0,1]")
    if a <= x0 and b <= y0 and a > 0 and b > 0:
        # (x, y, w, h) form.
        width_n, height_n = a, b
    else:
        width_n, height_n = a - x0, b - y0
    if width_n <= 0 or height_n <= 0:
        raise TransformError("normalized box has zero or negative extent")
    rect = Rect(
        int(round(geometry.rect.x + x0 * geometry.rect.width)),
        int(round(geometry.rect.y + y0 * geometry.rect.height)),
        max(1, int(round(width_n * geometry.rect.width))),
        max(1, int(round(height_n * geometry.rect.height))),
    )
    return rect


def map_model_box(
    box: Sequence[float],
    space: CoordinateSpace | str,
    geometry: CaptureGeometry,
) -> Rect:
    """Map a model-provided box from its declared space into physical pixels."""
    try:
        space_enum = CoordinateSpace(space)
    except ValueError as exc:
        raise TransformError(f"unsupported coordinate space: {space}") from exc
    if space_enum == CoordinateSpace.PHYSICAL_SCREEN:
        rect = Rect.from_any(box)
        if rect is None:
            raise TransformError("invalid physical_screen box")
        return rect
    if space_enum == CoordinateSpace.CAPTURE_PIXELS:
        return capture_to_screen(box, geometry)
    return normalized_to_screen(box, geometry)


def validate_rect_on_screens(
    rect: Rect,
    monitors: Sequence[Monitor],
    min_visible_ratio: float = 0.6,
    max_area_ratio: float = 0.98,
) -> tuple[bool, str]:
    """Decide whether ``rect`` is a plausible screen target.

    Returns ``(ok, reason)``. Rejects degenerate boxes, boxes mostly outside all
    monitors, and boxes covering essentially the whole screen (a model that
    "found" a target spanning everything has not grounded anything).
    """
    if rect.width <= 2 or rect.height <= 2:
        return False, "target box is degenerate"
    total_area = 0
    for monitor in monitors:
        total_area += monitor.rect.area
    if total_area <= 0:
        return False, "no monitor geometry available"
    if rect.area >= total_area * max_area_ratio:
        return False, "target box covers (almost) the whole screen"
    visible = 0
    for monitor in monitors:
        mrect = monitor.rect
        inter_w = min(rect.right, mrect.right) - max(rect.x, mrect.x)
        inter_h = min(rect.bottom, mrect.bottom) - max(rect.y, mrect.y)
        if inter_w > 0 and inter_h > 0:
            visible += inter_w * inter_h
    ratio = visible / max(1, rect.area)
    if ratio < min_visible_ratio:
        return False, f"target box is only {ratio:.0%} on screen"
    return True, ""


def clamp_to_monitors(rect: Rect, monitors: Sequence[Monitor]) -> Rect:
    """Clip a rectangle to the union of the monitors (used for overlay painting)."""
    if not monitors:
        return rect
    best = rect
    best_area = 0
    for monitor in monitors:
        mrect = monitor.rect
        x0 = max(rect.x, mrect.x)
        y0 = max(rect.y, mrect.y)
        x1 = min(rect.right, mrect.right)
        y1 = min(rect.bottom, mrect.bottom)
        if x1 <= x0 or y1 <= y0:
            continue
        area = (x1 - x0) * (y1 - y0)
        if area > best_area:
            best_area = area
            best = Rect(x0, y0, x1 - x0, y1 - y0)
    return best if best_area > 0 else rect


def scale_for_monitor(monitors: Sequence[Monitor], rect: Rect) -> float:
    for monitor in monitors:
        if monitor.rect.intersects(rect):
            return monitor.scale or 1.0
    return monitors[0].scale if monitors else 1.0
