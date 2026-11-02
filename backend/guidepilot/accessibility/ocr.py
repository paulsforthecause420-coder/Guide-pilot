"""OCR fallback using the Tesseract command-line engine.

OCR is a *fallback*: accessibility metadata is preferred because it is exact.
When accessibility is unavailable and Tesseract is installed, recognised words
become approximate elements (identified by text and bounding box) that the
grounder can place a highlight on.

Word boxes are reported in captured-image pixels and mapped back to physical
screen coordinates here, so no caller has to deal with two coordinate systems.
"""

from __future__ import annotations

import contextlib
import csv
import io
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .. import util
from ..models import Element, ElementSource, Rect


@dataclass
class OcrWord:
    text: str
    rect: Rect
    confidence: float


class OcrEngine:
    """Wraps the ``tesseract`` binary."""

    def __init__(self, binary: str | None = None) -> None:
        self.binary = binary or util.which("tesseract")
        self.last_error = ""

    def available(self) -> tuple[bool, str]:
        if not self.binary:
            return False, (
                "Tesseract is not installed (install tesseract-ocr for OCR fallback). "
                "GuidePilot still works through accessibility metadata and vision models."
            )
        if not os.access(self.binary, os.X_OK):
            return False, f"{self.binary} is not executable"
        return True, ""

    def extract_words(self, png_bytes: bytes) -> list[OcrWord]:
        """Run OCR on a PNG buffer and return word boxes in image pixels."""
        if not self.binary or not png_bytes:
            return []
        handle = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        temp_path = Path(handle.name)
        try:
            handle.write(png_bytes)
            handle.close()
            result = util.run_cmd(
                [self.binary, str(temp_path), "stdout", "--psm", "6", "tsv"],
                timeout=25,
            )
            if not result.ok:
                self.last_error = util.truncated((result.stderr or "").strip(), 200)
                return []
            return parse_tsv(result.stdout)
        finally:
            with contextlib.suppress(OSError):
                temp_path.unlink()

    def read_text(self, png_bytes: bytes) -> str | None:
        """Read one field's text from its pixels, as a single line.

        Used only when the accessibility tree cannot answer: OCR is slower and
        approximate, so it may confirm a value but is never treated as proof that
        a value is wrong.
        """
        if not self.binary or not png_bytes:
            return None
        handle = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        temp_path = Path(handle.name)
        try:
            handle.write(png_bytes)
            handle.close()
            # psm 7 treats the image as a single line, which is what a text field
            # is; psm 6 would try to find paragraphs in a 400x40 crop.
            result = util.run_cmd([self.binary, str(temp_path), "stdout", "--psm", "7"], timeout=25)
            if not result.ok:
                self.last_error = util.truncated((result.stderr or "").strip(), 200)
                return None
            text = " ".join((result.stdout or "").split())
            return text or None
        finally:
            with contextlib.suppress(OSError):
                temp_path.unlink()

    def extract_elements(
        self,
        png_bytes: bytes,
        capture_rect: Rect,
        capture_width: int,
        capture_height: int,
        max_words: int = 220,
    ) -> list[Element]:
        """OCR words mapped into physical screen coordinates."""
        words = self.extract_words(png_bytes)
        if not words:
            return []
        scale_x = (capture_rect.width / capture_width) if capture_width else 1.0
        scale_y = (capture_rect.height / capture_height) if capture_height else 1.0
        elements: list[Element] = []
        for index, word in enumerate(words[:max_words]):
            text = word.text.strip()
            if len(text) < 2:
                continue
            rect = Rect(
                int(round(capture_rect.x + word.rect.x * scale_x)),
                int(round(capture_rect.y + word.rect.y * scale_y)),
                max(1, int(round(word.rect.width * scale_x))),
                max(1, int(round(word.rect.height * scale_y))),
            )
            elements.append(
                Element(
                    id=f"ocr-{index}",
                    name=text,
                    role="text",
                    rect=rect,
                    source=ElementSource.OCR,
                    confidence=max(0.0, min(1.0, word.confidence / 100.0)),
                    attributes={"engine": "tesseract"},
                )
            )
        return elements


def parse_tsv(payload: str) -> list[OcrWord]:
    """Parse Tesseract TSV output into words with boxes and confidence."""
    words: list[OcrWord] = []
    if not payload:
        return words
    reader = csv.DictReader(io.StringIO(payload), delimiter="\t", quoting=csv.QUOTE_NONE)
    for row in reader:
        text = (row.get("text") or "").strip()
        if not text:
            continue
        try:
            left = int(float(row.get("left") or 0))
            top = int(float(row.get("top") or 0))
            width = int(float(row.get("width") or 0))
            height = int(float(row.get("height") or 0))
            confidence = float(row.get("conf") or 0)
        except (TypeError, ValueError):
            continue
        if width <= 0 or height <= 0:
            continue
        words.append(OcrWord(text=text, rect=Rect(left, top, width, height), confidence=confidence))
    return words


def group_lines(words: Iterable[OcrWord], tolerance: int = 6) -> list[str]:
    """Group word boxes into readable lines (used for verification text checks)."""
    ordered = sorted(words, key=lambda w: (w.rect.y, w.rect.x))
    lines: list[list[OcrWord]] = []
    for word in ordered:
        if lines and abs(lines[-1][0].rect.y - word.rect.y) <= tolerance:
            lines[-1].append(word)
        else:
            lines.append([word])
    return [" ".join(w.text for w in sorted(line, key=lambda w: w.rect.x)) for line in lines]


def ocr_capability() -> dict[str, Any]:
    engine = OcrEngine()
    ok, reason = engine.available()
    return {"available": ok, "engine": "tesseract" if ok else None, "reason": reason}
