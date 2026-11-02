"""The screen observation pipeline.

``ScreenObserver.observe()`` produces an :class:`Observation`: window context,
monitor geometry, an in-memory frame, accessibility elements and OCR words that
have been merged and de-duplicated. It is the single place where "what is on the
screen right now" is assembled.

Guarantees
----------
* Nothing is captured while capture is stopped, paused, or the sharing toggle is
  off; the returned observation explains why in plain language.
* Windows matching the privacy exclusion list are never captured and are marked
  ``sensitive``; no elements are read from them.
* Frames are kept only in memory with a small bound. No screenshot touches disk
  outside a scratch file that is deleted inside the OCR call.
"""

from __future__ import annotations

import collections
import threading
import uuid
from dataclasses import dataclass
from typing import Any

from .. import util
from ..accessibility.atspi import AtspiClient
from ..accessibility.ocr import OcrEngine
from ..accessibility.window import WindowListing, list_windows, monitor_for_rect
from ..config import Settings
from ..models import CaptureFrame, Element, ElementSource, Monitor, Observation, Rect, WindowInfo
from .capture import CaptureBackend, CaptureError, CaptureResult, create_backend
from .change import compare, signature_from_png
from .display import DisplayInfo, detect_display


@dataclass
class CachedFrame:
    observation_id: str
    signature: Any
    png: bytes
    capture: CaptureResult
    target_key: str


# GuidePilot's own windows must never become the guidance target: the user is
# working in another application while GuidePilot floats above it. Matching is
# exact/deliberately narrow so that *other* applications whose titles merely
# contain the product name are still guided normally.
SELF_CLASSES = {"guidepilot", "guidepilot-desktop", "guidepilot-overlay"}
SELF_TITLES = {"guidepilot", "guidepilot assistant", "guidepilot overlay", "guidepilot settings"}
SELF_TITLE_PREFIXES = ("guidepilot —", "guidepilot -", "guidepilot overlay", "guidepilot assistant")


class ScreenObserver:
    """Capture + perception orchestration, safe for concurrent use."""

    def __init__(
        self,
        settings: Settings,
        display: DisplayInfo | None = None,
        backend: CaptureBackend | None = None,
        atspi: AtspiClient | None = None,
        ocr: OcrEngine | None = None,
    ) -> None:
        self.settings = settings
        self.display = display or detect_display()
        self.backend = backend or create_backend(self.display)
        self.atspi = atspi or AtspiClient()
        self.ocr = ocr or OcrEngine()
        self._lock = threading.RLock()
        self._frames: collections.deque[CachedFrame] = collections.deque(maxlen=8)
        self._latest: Observation | None = None
        self._last_signature: dict[str, Any] = {}
        self.stats = {
            "captures": 0,
            "skipped_unchanged": 0,
            "sensitive_skips": 0,
            "capture_errors": 0,
            "a11y_snapshots": 0,
            "ocr_runs": 0,
            "self_window_fallbacks": 0,
        }
        self._last_target_window_id: str | None = None

    # -- capability reporting ---------------------------------------------
    def capabilities(self) -> dict[str, Any]:
        backend_ok, backend_reason = self.backend.available()
        try:
            from ..accessibility.atspi import atspi_available

            atspi_ok, atspi_reason = atspi_available()
        except Exception as exc:  # pragma: no cover - defensive
            atspi_ok, atspi_reason = False, str(exc)
        ocr_ok, ocr_reason = self.ocr.available()
        return {
            "session": self.display.to_dict(),
            "capture": {
                "backend": self.backend.name,
                "available": backend_ok,
                "reason": backend_reason,
                "supports_window_capture": bool(getattr(self.backend, "supports_window_capture", False)),
            },
            "accessibility": {"available": atspi_ok, "reason": atspi_reason, "backend": "at-spi2 (gdbus)"},
            "ocr": {"available": ocr_ok, "reason": ocr_reason, "engine": self.ocr.binary},
            "stats": dict(self.stats),
        }

    # -- privacy -----------------------------------------------------------
    def _sensitivity_reason(self, window: WindowInfo | None) -> str | None:
        if window is None:
            return None
        patterns = self.settings.get("privacy.exclusion_patterns", []) or []
        haystack = f"{window.title} {window.app_class}".casefold()
        for pattern in patterns:
            cleaned = (pattern or "").strip().casefold()
            if cleaned and cleaned in haystack:
                return f"Active window matches privacy exclusion pattern '{pattern}'"
        return None

    def _capture_blocked_reason(self) -> str | None:
        privacy = self.settings.section("privacy")
        if privacy.get("stop_capture"):
            return "Screen capture is stopped. Enable screen sharing to continue."
        if privacy.get("paused"):
            return "Observation is paused."
        if not privacy.get("share_enabled"):
            return "Screen sharing is off. GuidePilot will not capture your screen until you enable it."
        return None

    # -- main entry point --------------------------------------------------
    def observe(
        self,
        force: bool = False,
        scope: str | None = None,
        app_hint: str | None = None,
        window_id: str | None = None,
    ) -> Observation:
        observation_id = f"obs-{uuid.uuid4().hex[:10]}"
        created_at = util.now_ms()
        notes: list[str] = []
        sources: dict[str, Any] = {}
        elements: list[Element] = []
        windows_listing: WindowListing = list_windows()
        monitors = self.display.monitors
        active = self._select_active(windows_listing, window_id)
        if active and not active.monitor_id:
            active.monitor_id = monitor_for_rect(active.rect, monitors)
        sensitivity = self._sensitivity_reason(active)
        obs_scope = scope or self.settings.get("observation.scope", "active_window")

        observation = Observation(
            id=observation_id,
            created_at=created_at,
            session_type=self.display.session_type,
            monitors=monitors,
            active_window=active,
            windows=windows_listing.windows,
            elements=[],
            frame=None,
            sources=sources,
            notes=notes,
        )
        sources["display"] = {
            "session_type": self.display.session_type,
            "desktop": self.display.desktop,
            "compositor": self.display.compositor,
            "overlay_supported": self.display.overlay_supported,
        }
        sources["windows"] = {
            "count": len(windows_listing.windows),
            "active_id": windows_listing.active_id,
            "notes": windows_listing.notes,
        }

        if sensitivity:
            observation.sensitive = True
            notes.append(sensitivity + "; capture was skipped to protect sensitive content.")
            self.stats["sensitive_skips"] += 1
            sources["capture"] = {"skipped": True, "reason": "sensitive_window"}
            self._latest = observation
            return observation

        blocked = self._capture_blocked_reason()
        if blocked:
            notes.append(blocked)
            sources["capture"] = {"skipped": True, "reason": "privacy_disabled"}
            self._latest = observation
            return observation

        capture = self._perform_capture(obs_scope, active, monitors, notes, sources)
        frame_png: bytes | None = None
        if capture is not None:
            frame_png = capture.png
            signature = signature_from_png(capture.png)
            target_key = f"{obs_scope}:{capture.window_id or capture.rect.x},{capture.rect.y}"
            previous = self._last_signature.get(target_key)
            change_ratio, hamming = compare(previous, signature)
            self._last_signature[target_key] = signature
            if previous is not None and change_ratio < float(self.settings.get("observation.min_change_ratio", 0.004)):
                self.stats["skipped_unchanged"] += 1
            observation.frame = CaptureFrame(
                id=f"frame-{uuid.uuid4().hex[:8]}",
                scope=obs_scope,
                target_id=capture.window_id,
                rect=capture.rect,
                width=capture.width,
                height=capture.height,
                scale=capture.scale,
                captured_at=capture.captured_at,
                backend=capture.backend,
                signature=signature.hash,
                change_ratio=change_ratio,
            )
            with self._lock:
                self._frames.append(
                    CachedFrame(
                        observation_id=observation_id,
                        signature=signature,
                        png=frame_png,
                        capture=capture,
                        target_key=target_key,
                    )
                )
                self.stats["captures"] += 1
            sources["capture"] = {
                "skipped": False,
                "backend": capture.backend,
                "rect": capture.rect.to_list(),
                "size": [capture.width, capture.height],
                "scale": round(capture.scale, 4),
                "changed": change_ratio >= float(self.settings.get("observation.min_change_ratio", 0.004)),
                "change_ratio": round(change_ratio, 5),
                "hamming": round(hamming, 5),
            }
        elements.extend(self._accessibility_elements(active, obs_scope, app_hint, notes, sources))
        if frame_png is not None and self.settings.get("perception.use_ocr", True):
            elements.extend(self._ocr_elements(frame_png, capture, notes, sources))
        observation.elements = self._merge_elements(elements)
        if observation.elements:
            notes.append(f"{len(observation.elements)} labelled elements detected")
        self._latest = observation
        return observation

    # -- helpers -----------------------------------------------------------
    def _is_self_window(self, window: WindowInfo) -> bool:
        klass = (window.app_class or "").strip().casefold()
        title = (window.title or "").strip().casefold()
        if klass in SELF_CLASSES:
            return True
        if title in SELF_TITLES:
            return True
        return title.startswith(SELF_TITLE_PREFIXES)

    def _select_active(self, listing: WindowListing, window_id: str | None) -> WindowInfo | None:
        if window_id:
            for window in listing.windows:
                if window.id.lower() == window_id.lower():
                    return window
        active: WindowInfo | None = None
        if listing.active_id:
            active = next((w for w in listing.windows if w.id.lower() == listing.active_id.lower()), None)
        if active is None:
            active = next((w for w in listing.windows if w.active), None)
        if active is not None and not self._is_self_window(active):
            self._last_target_window_id = active.id
            return active
        if active is not None and self._is_self_window(active):
            # The user clicked GuidePilot itself. Keep guiding the last real
            # target window instead of analysing our own UI.
            self.stats["self_window_fallbacks"] += 1
            if self._last_target_window_id:
                previous = next(
                    (w for w in listing.windows if w.id.lower() == self._last_target_window_id.lower()), None
                )
                if previous is not None:
                    return previous
            candidates = [
                w
                for w in listing.windows
                if not self._is_self_window(w) and w.rect.area >= 120000 and w.title.strip()
            ]
            if candidates:
                # Prefer the largest real application window rather than a
                # popup or utility window.
                candidates.sort(key=lambda w: w.rect.area, reverse=True)
                self._last_target_window_id = candidates[0].id
                return candidates[0]
        return active

    def _perform_capture(
        self,
        scope: str,
        active: WindowInfo | None,
        monitors: list[Monitor],
        notes: list[str],
        sources: dict[str, Any],
    ) -> CaptureResult | None:
        try:
            result: CaptureResult
            if scope == "active_window" and active is not None and getattr(self.backend, "supports_window_capture", False):
                result = self.backend.capture_window(active.id)
            elif scope == "active_window" and active is not None:
                result = self.backend.capture_rect(active.rect)
            elif scope == "monitor":
                monitor_id = self.settings.get("observation.monitor_id")
                monitor = next((m for m in monitors if m.id == monitor_id), None) or (
                    active and next((m for m in monitors if m.id == active.monitor_id), None)
                )
                monitor = monitor or (monitors[0] if monitors else None)
                if monitor is None:
                    raise CaptureError("no_monitor", "No monitor geometry is available")
                result = self.backend.capture_rect(monitor.rect)
            elif scope == "all_monitors":
                if not monitors:
                    raise CaptureError("no_monitor", "No monitor geometry is available")
                x0 = min(m.x for m in monitors)
                y0 = min(m.y for m in monitors)
                x1 = max(m.x + m.width for m in monitors)
                y1 = max(m.y + m.height for m in monitors)
                result = self.backend.capture_rect(Rect(x0, y0, x1 - x0, y1 - y0))
            else:  # pragma: no cover - defensive
                result = self.backend.capture_root()
        except CaptureError as exc:
            self.stats["capture_errors"] += 1
            notes.append(f"Screen capture failed: {exc.message}")
            if exc.hint:
                notes.append(exc.hint)
            sources["capture"] = {"skipped": True, "reason": exc.reason, "error": exc.message, "hint": exc.hint}
            return None
        except Exception as exc:  # pragma: no cover - defensive
            self.stats["capture_errors"] += 1
            notes.append(f"Screen capture failed: {exc}")
            sources["capture"] = {"skipped": True, "reason": "unexpected", "error": str(exc)}
            return None
        return result

    def _accessibility_elements(
        self,
        active: WindowInfo | None,
        scope: str,
        app_hint: str | None,
        notes: list[str],
        sources: dict[str, Any],
    ) -> list[Element]:
        if not self.settings.get("perception.use_accessibility", True):
            sources["accessibility"] = {"used": False, "reason": "disabled in settings"}
            return []
        hint = app_hint or (active.app_class if active else "") or ""
        try:
            snapshot = self.atspi.snapshot(
                app_hint=hint,
                window_title=active.title if active else None,
                window_rect=active.rect if (active and scope == "active_window") else None,
                # Anchors the tree to the process that owns the window being
                # observed, so a stale registration of the same application can
                # never be read in its place.
                window_pid=active.pid if active else None,
            )
        except Exception as exc:  # pragma: no cover - defensive
            sources["accessibility"] = {"used": False, "reason": str(exc)}
            notes.append(f"Accessibility read failed: {util.truncated(str(exc), 160)}")
            return []
        self.stats["a11y_snapshots"] += 1
        sources["accessibility"] = {
            "used": snapshot.available,
            "values_unavailable": snapshot.values_unavailable,
            "app": snapshot.app_name,
            "frame": snapshot.frame_title,
            "element_count": len(snapshot.elements),
            "node_count": snapshot.node_count,
            "truncated": snapshot.truncated,
            "duration_ms": snapshot.duration_ms,
            "notes": snapshot.notes,
        }
        for note in snapshot.notes:
            notes.append(note)
        return snapshot.elements

    def _ocr_elements(
        self,
        png: bytes,
        capture: CaptureResult | None,
        notes: list[str],
        sources: dict[str, Any],
    ) -> list[Element]:
        if capture is None:
            return []
        ok, reason = self.ocr.available()
        if not ok:
            sources["ocr"] = {"used": False, "reason": reason}
            return []
        try:
            words = self.ocr.extract_elements(png, capture.rect, capture.width, capture.height)
        except Exception as exc:  # pragma: no cover - defensive
            sources["ocr"] = {"used": False, "reason": str(exc)}
            return []
        self.stats["ocr_runs"] += 1
        sources["ocr"] = {"used": True, "element_count": len(words)}
        if not words:
            notes.append("OCR found no readable text in the captured region")
        return words

    def _merge_elements(self, elements: list[Element]) -> list[Element]:
        """Prefer accessibility elements; drop duplicate OCR words inside them."""
        a11y = [e for e in elements if e.source == ElementSource.ATSPI]
        others = [e for e in elements if e.source != ElementSource.ATSPI]
        kept: list[Element] = list(a11y)
        for element in others:
            if element.rect.is_degenerate():
                continue
            duplicate = False
            for existing in a11y:
                if existing.rect.area <= 0 or element.rect.area <= 0:
                    continue
                inter_w = min(existing.rect.right, element.rect.right) - max(existing.rect.x, element.rect.x)
                inter_h = min(existing.rect.bottom, element.rect.bottom) - max(existing.rect.y, element.rect.y)
                overlap = max(0, inter_w) * max(0, inter_h)
                if overlap / max(1, element.rect.area) > 0.55:
                    duplicate = True
                    break
            if not duplicate:
                kept.append(element)
        kept.sort(key=lambda e: (e.rect.y, e.rect.x))
        return kept

    # -- cache access ------------------------------------------------------
    def frame_png(self, observation_id: str | None = None) -> bytes | None:
        with self._lock:
            for cached in reversed(self._frames):
                if observation_id is None or cached.observation_id == observation_id:
                    return cached.png
        return None

    def latest(self) -> Observation | None:
        return self._latest

    def last_change_ratio(self) -> float | None:
        observation = self._latest
        if not observation or not observation.frame:
            return None
        return observation.frame.change_ratio

    def clear(self) -> None:
        with self._lock:
            self._frames.clear()
            self._last_signature.clear()
            self._latest = None
