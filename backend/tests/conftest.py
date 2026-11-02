"""Shared fixtures for the GuidePilot backend test suite.

The tests are split into two kinds:

* **Pure** tests that exercise models, verification rules, planning, security
  helpers, storage and the HTTP layer with no desktop dependencies at all.
* **Engine** tests that drive the real ``TaskEngine`` state machine against a
  *fake* observer. The fake returns a screen the test wrote, which is exactly
  what the engine consumes in production — the engine is never mocked.

Nothing here touches the live X11 session, the accessibility bus, the user's
real database, or the user's real settings file.
"""

from __future__ import annotations

import copy
import struct
import sys
import zlib
from pathlib import Path
from typing import Any, Callable, Sequence

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from guidepilot.config import Settings  # noqa: E402
from guidepilot.models import (  # noqa: E402
    CaptureFrame,
    Element,
    ElementSource,
    Monitor,
    Observation,
    Rect,
    WindowInfo,
)
from guidepilot.perception.capture import CaptureResult  # noqa: E402
from guidepilot.storage.db import Database  # noqa: E402


# ---------------------------------------------------------------------------
# Settings and storage
# ---------------------------------------------------------------------------


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    """Isolated settings with capture switched on (the default is opt-in)."""
    instance = Settings(tmp_path / "settings.json")
    instance.set("privacy.share_enabled", True)
    instance.set("perception.use_ocr", False)
    instance.set("ai.provider", "none")
    instance.set("voice.enabled", False)
    return instance


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "guidepilot.db")
    yield database
    database.close()


@pytest.fixture()
def project_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "project"
    directory.mkdir()
    return directory


# ---------------------------------------------------------------------------
# Synthetic screen content
# ---------------------------------------------------------------------------


WINDOW = WindowInfo(
    id="0xabc",
    title="GuidePilot Demo Application",
    app_class="guidepilot-demo",
    pid=4242,
    rect=Rect(160, 120, 760, 620),
    active=True,
    monitor_id="DP-1",
)


def element(
    name: str,
    role: str = "push button",
    rect: Sequence[int] = (200, 200, 120, 32),
    value: str | None = None,
    element_id: str | None = None,
) -> Element:
    return Element(
        id=element_id or f"el-{name.lower().replace(' ', '-')}",
        name=name,
        role=role,
        rect=Rect(*rect),
        source=ElementSource.ATSPI,
        value=value,
        app="guidepilot-demo",
        window_id=WINDOW.id,
    )


# Chromium exposes a form control's `<label for>` text as the control's own
# accessible name, so the entries below are named. The separate label elements
# are kept because real trees contain them too.
FORM_ELEMENTS: list[Element] = [
    element("App Name", "label", (200, 180, 120, 20)),
    element("App Name", "entry", (330, 176, 300, 30)),
    element("Callback URL", "label", (200, 230, 120, 20)),
    element("Callback URL", "entry", (330, 226, 300, 30)),
    element("Region", "label", (200, 280, 120, 20)),
    element("Region", "combo box", (330, 276, 300, 30)),
    element("Enable telemetry", "check box", (330, 326, 20, 20)),
    element("Strict mode", "check box", (330, 366, 20, 20)),
    element("Save Configuration", "push button", (330, 420, 180, 36)),
    element("Reset Form", "push button", (530, 420, 120, 36)),
]


def make_observation(
    elements: Sequence[Element] | None = None,
    *,
    values_unavailable: bool = False,
    change_ratio: float = 1.0,
    sensitive: bool = False,
    notes: Sequence[str] | None = None,
) -> Observation:
    frame = CaptureFrame(
        id="frame-test",
        scope="active_window",
        target_id=WINDOW.id,
        rect=WINDOW.rect,
        width=760,
        height=620,
        scale=1.0,
        captured_at=1_700_000_000_000,
        backend="fake",
        signature="deadbeef",
        change_ratio=change_ratio,
    )
    observation = Observation(
        id="obs-test",
        created_at=1_700_000_000_000,
        session_type="x11",
        monitors=[Monitor(id="DP-1", name="Primary", x=0, y=0, width=1920, height=1080, primary=True)],
        active_window=WINDOW,
        windows=[WINDOW],
        # Deep-copied so a test that edits an element cannot leak into the next
        # test through the module-level FORM_ELEMENTS template.
        elements=copy.deepcopy(list(elements if elements is not None else FORM_ELEMENTS)),
        frame=frame,
        sources={
            "display": {"session_type": "x11", "overlay_supported": True},
            "capture": {"skipped": False, "backend": "fake"},
            "accessibility": {
                "used": True,
                "available": True,
                "values_unavailable": values_unavailable,
                "count": len(elements if elements is not None else FORM_ELEMENTS),
            },
        },
        notes=list(notes or []),
        sensitive=sensitive,
    )
    observation.id = f"obs-{abs(hash(id(observation)))}"
    return observation


# ---------------------------------------------------------------------------
# Fake observer
# ---------------------------------------------------------------------------


def solid_png(color: tuple[int, int, int] = (255, 255, 255), size: int = 16) -> bytes:
    """A real, decodable PNG so pixel comparisons in tests are meaningful."""
    raw = b"".join(b"\x00" + bytes(color) * size for _ in range(size))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


class FakeCaptureBackend:
    """Capture backend that returns a solid PNG of the requested region."""

    name = "fake"
    supports_window_capture = True

    def __init__(self) -> None:
        self.calls = 0
        self.png = solid_png()

    def available(self) -> tuple[bool, str]:
        return True, "fake backend"

    def capture_rect(self, rect: Rect) -> CaptureResult:
        self.calls += 1
        return CaptureResult(
            png=self.png,
            rect=rect,
            width=max(1, rect.width),
            height=max(1, rect.height),
            scale=1.0,
            backend=self.name,
            captured_at=1_700_000_000_000,
        )


class FakeDisplay:
    session_type = "x11"
    desktop = "XFCE"
    compositor = "xfwm4"
    overlay_supported = True
    overlay_backend = "x11_shape"
    overlay_notes: list[str] = []
    monitors = [Monitor(id="DP-1", name="Primary", x=0, y=0, width=1920, height=1080, primary=True)]

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_type": self.session_type,
            "desktop": self.desktop,
            "compositor": self.compositor,
            "overlay_supported": self.overlay_supported,
            "overlay_backend": self.overlay_backend,
            "monitors": [m.to_dict() for m in self.monitors],
        }


class FakeObserver:
    """Returns whatever observation the test tells it to return.

    ``screen`` may be a single observation or a callable taking the call index.
    """

    def __init__(self, screen: Observation | Callable[[int], Observation]) -> None:
        self._screen = screen
        self.calls: list[dict[str, Any]] = []
        self.backend = FakeCaptureBackend()
        self._latest: Observation | None = None
        # The API reads display capabilities for its /capabilities and
        # /settings responses, so the fake exposes the same shape.
        self.display = FakeDisplay()

    def _next(self, kwargs: dict[str, Any]) -> Observation:
        self.calls.append(kwargs)
        if callable(self._screen):
            observation = self._screen(len(self.calls) - 1)
        else:
            observation = self._screen
        self._latest = observation
        return observation

    def observe(self, *args: Any, **kwargs: Any) -> Observation:
        return self._next(kwargs)

    def frame_png(self, observation_id: str | None = None) -> bytes | None:
        return None

    def latest(self) -> Observation | None:
        return self._latest

    def clear(self) -> None:
        self._latest = None

    def capabilities(self) -> dict[str, Any]:
        return {
            "session": {"session_type": "x11", "overlay_supported": True},
            "capture": {"backend": "fake", "available": True},
            "accessibility": {"available": True, "reason": ""},
            "ocr": {"available": False, "reason": "disabled"},
            "stats": {},
        }


@pytest.fixture()
def form_observation() -> Observation:
    return make_observation()


@pytest.fixture()
def fake_observer(form_observation: Observation) -> FakeObserver:
    return FakeObserver(form_observation)
