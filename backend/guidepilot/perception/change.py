"""Perceptual change detection to avoid redundant captures and model calls.

Two independent implementations:

* Pillow present: 16x16 grayscale difference hash plus mean absolute pixel
  difference, both cheap enough for a CPU-only laptop.
* Pillow missing: content hash of the encoded frame. Coarser (any pixel change
  counts) but never wrong in the unsafe direction, and it keeps GuidePilot
  usable on a minimal install.

The signatures contain no screen content — only coarse luminance gradients.
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from typing import Any

try:  # pragma: no cover - exercised implicitly
    from PIL import Image

    PIL_AVAILABLE = True
except Exception:  # pragma: no cover
    Image = None  # type: ignore[assignment]
    PIL_AVAILABLE = False


@dataclass
class FrameSignature:
    kind: str  # "perceptual" | "bytes"
    hash: str
    gray: bytes | None = None
    width: int = 0
    height: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "hash": self.hash, "width": self.width, "height": self.height}


def signature_from_png(png_bytes: bytes, grid: int = 16) -> FrameSignature:
    """Compute a stable signature for an encoded PNG frame."""
    if not png_bytes:
        return FrameSignature("bytes", hashlib.sha256(b"").hexdigest())
    if not PIL_AVAILABLE:
        return FrameSignature("bytes", hashlib.sha256(png_bytes).hexdigest())
    try:
        with Image.open(io.BytesIO(png_bytes)) as image:
            gray = image.convert("L").resize((grid, grid), Image.Resampling.BOX)
            data = gray.tobytes()
    except Exception:
        return FrameSignature("bytes", hashlib.sha256(png_bytes).hexdigest())
    digest = hashlib.sha256(data).hexdigest()[:32]
    return FrameSignature("perceptual", digest, data, grid, grid)


def _dhash(data: bytes, grid: int) -> str:
    """Difference hash of a grid x grid grayscale buffer."""
    bits = []
    for row in range(grid):
        base = row * grid
        for col in range(grid - 1):
            bits.append(1 if data[base + col] > data[base + col + 1] else 0)
        bits.append(0)
    value = 0
    for bit in bits:
        value = (value << 1) | bit
    return f"{value:0{(len(bits) + 3) // 4}x}"


def compare(previous: FrameSignature | None, current: FrameSignature) -> tuple[float, float]:
    """Return (change_ratio, hamming_ratio) in [0, 1].

    ``change_ratio`` is the fraction of grid cells whose luminance moved more
    than a small threshold. It resists blinking cursors and anti-aliasing noise
    while still noticing a new dialog or a page transition.
    """
    if previous is None:
        return 1.0, 1.0
    if previous.kind != current.kind or previous.hash == current.hash:
        # Identical hash means identical frame regardless of kind.
        if previous.hash == current.hash:
            return 0.0, 0.0
        return 1.0, 1.0
    if not previous.gray or not current.gray or len(previous.gray) != len(current.gray):
        return 1.0, 1.0
    changed = 0
    total_diff = 0
    for before, after in zip(previous.gray, current.gray):
        delta = abs(before - after)
        total_diff += delta
        if delta > 12:
            changed += 1
    cells = len(previous.gray)
    change_ratio = changed / cells
    grid = int(round(cells ** 0.5))
    before_hash = _dhash(previous.gray, grid)
    after_hash = _dhash(current.gray, grid)
    hamming = sum(1 for a, b in zip(before_hash, after_hash) if a != b) / max(1, len(after_hash))
    return min(1.0, change_ratio + 0.0), min(1.0, hamming)


def image_size(png_bytes: bytes) -> tuple[int, int]:
    if PIL_AVAILABLE and png_bytes:
        try:
            with Image.open(io.BytesIO(png_bytes)) as image:
                return image.size
        except Exception:
            return (0, 0)
    return (0, 0)


def crop_png(png_bytes: bytes, rect: tuple[int, int, int, int]) -> bytes | None:
    """Crop an encoded PNG; returns None when Pillow is unavailable."""
    if not PIL_AVAILABLE or not png_bytes:
        return None
    try:
        with Image.open(io.BytesIO(png_bytes)) as image:
            left, top, right, bottom = rect
            left = max(0, left)
            top = max(0, top)
            right = min(image.width, right)
            bottom = min(image.height, bottom)
            if right <= left or bottom <= top:
                return None
            cropped = image.crop((left, top, right, bottom))
            buffer = io.BytesIO()
            cropped.convert("RGB").save(buffer, format="PNG", optimize=False)
            return buffer.getvalue()
    except Exception:
        return None


def downscale_png(png_bytes: bytes, max_width: int) -> tuple[bytes, float]:
    """Downscale for model inference; returns (bytes, applied_scale)."""
    if not PIL_AVAILABLE or not png_bytes or max_width <= 0:
        return png_bytes, 1.0
    try:
        with Image.open(io.BytesIO(png_bytes)) as image:
            scale = min(1.0, max_width / float(image.width or 1))
            if scale >= 0.999:
                buffer = io.BytesIO()
                image.convert("RGB").save(buffer, format="PNG", optimize=False)
                return buffer.getvalue(), 1.0
            size = (max(1, int(image.width * scale)), max(1, int(image.height * scale)))
            resized = image.convert("RGB").resize(size, Image.Resampling.BILINEAR)
            buffer = io.BytesIO()
            resized.save(buffer, format="PNG", optimize=False)
            return buffer.getvalue(), scale
    except Exception:
        return png_bytes, 1.0
