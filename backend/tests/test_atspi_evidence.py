"""What "I cannot read this field" is allowed to mean.

Applications differ wildly in what they publish. An empty field and an
application with no text support both read back as an empty string, and
confusing the two either accuses the user of a failure that did not happen or
claims a success that was never observed. These tests pin the distinction.
"""

from __future__ import annotations

import copy
import struct
import zlib

from conftest import FORM_ELEMENTS, FakeObserver, FakeOcr, make_observation, solid_png

from guidepilot.accessibility.atspi import AtspiApp, AtspiClient, fields_unreadable
from guidepilot.providers.gateway import ModelGateway
from guidepilot.task_engine.engine import TaskEngine
from guidepilot.task_engine.planner import TaskPlanner
from guidepilot.verification.verifier import Verifier

CONTEXT = {"App Name": "GuidePilot Acceptance", "Callback URL": "https://acceptance.example.com/cb"}


def build_engine(settings, db, observer) -> TaskEngine:
    gateway = ModelGateway(settings)
    return TaskEngine(
        settings, observer, TaskPlanner(settings, gateway), gateway, Verifier(), db
    )


def test_an_empty_readable_form_is_not_reported_as_unreadable():
    # Every field implements Text and is simply empty: values are readable.
    assert fields_unreadable([True, True]) is False


def test_fields_without_text_support_are_unreadable():
    assert fields_unreadable([False, False]) is True
    # One readable field is enough to keep the window readable.
    assert fields_unreadable([False, True]) is False


def test_no_fields_at_all_is_not_a_readability_problem():
    assert fields_unreadable([]) is False


def test_unreadable_field_is_refined_into_useful_advice(settings, db):
    """Typed nothing yet: say so, instead of "cannot verify"."""
    observation = make_observation(values_unavailable=True)
    observer = FakeObserver(observation)
    observer.backend.png = solid_png((255, 255, 255))
    engine = build_engine(settings, db, observer)
    engine.create_task("Walk me through configuring this application", context_values=CONTEXT)

    state = engine.verify_current()
    verification = state["task"]["verification"]
    assert verification["outcome"] == "INCONCLUSIVE"
    assert "looks exactly as it did" in verification["summary"], verification["summary"]
    assert any(evidence["rule"] == "pixels" for evidence in verification["evidence"])


def test_unreadable_field_reports_a_visible_change_without_claiming_success(settings, db):
    observation = make_observation(values_unavailable=True)
    observer = FakeObserver(observation)
    observer.backend.png = solid_png((255, 255, 255))
    engine = build_engine(settings, db, observer)
    engine.create_task("Walk me through configuring this application", context_values=CONTEXT)

    # The field's pixels change between the instruction and the check.
    observer.backend.png = solid_png((0, 0, 0))
    state = engine.verify_current()
    verification = state["task"]["verification"]
    assert verification["outcome"] == "INCONCLUSIVE", "pixels must never be upgraded to a verified value"
    assert "visibly changed" in verification["summary"]
    assert "cannot read the exact value back" in verification["summary"]
    assert state["task"]["steps"][0]["status"] != "verified"


class _StubClient(AtspiClient):
    """A client whose bus is a list, so matching can be tested without a desktop."""

    def __init__(self, apps, frames):
        super().__init__()
        self._stub_apps = apps
        self._stub_frames = frames

    def apps(self, force: bool = False):
        return list(self._stub_apps)

    def _frame_titles(self, app):
        return self._stub_frames.get(app.dest, [])


def test_the_application_is_chosen_by_process_not_by_name_or_title():
    """A dead instance that never unregistered must not be read in the live one's place.

    Two `electron` apps can share the same window title; only the pid says which
    one owns the window in front of the user, and grounding on the wrong one
    yields stale rectangles and stale field values.
    """
    ghost = AtspiApp(dest=":1.17041", name="electron", pid=None)
    live = AtspiApp(dest=":1.24942", name="electron", pid=4242)
    frames = {ghost.dest: [("GuidePilot Demo", ghost.dest, "/root")],
              live.dest: [("GuidePilot Demo", live.dest, "/root")]}

    client = _StubClient([ghost, live], frames)
    assert client.find_app("electron", "GuidePilot Demo", pid=4242) is live


def test_a_window_whose_process_owns_no_tree_grounds_on_nothing():
    """When the bus reports pids and none matches, refusing beats guessing."""
    ghost = AtspiApp(dest=":1.17041", name="electron", pid=999)
    client = _StubClient([ghost], {ghost.dest: [("GuidePilot Demo", ghost.dest, "/root")]})
    assert client.find_app("electron", "GuidePilot Demo", pid=4242) is None


def test_toolkits_without_a_pid_still_ground_by_name_and_title():
    """A toolkit that publishes no ProcessId keeps the old behaviour."""
    app = AtspiApp(dest=":1.5", name="Thunar", pid=None)
    client = _StubClient([app], {app.dest: [("Downloads", app.dest, "/root")]})
    assert client.find_app("thunar", "Downloads", pid=4242) is app


def test_ocr_verifies_a_field_the_tree_cannot_read(settings, db):
    """No field text in the tree, exact text in the pixels: that is a pass."""
    observer = FakeObserver(make_observation(values_unavailable=True))
    observer.backend.png = solid_png((255, 255, 255))
    observer.ocr = FakeOcr("GuidePilot Acceptance")
    engine = build_engine(settings, db, observer)
    engine.create_task("Walk me through configuring this application", context_values=CONTEXT)

    observer.backend.png = solid_png((0, 0, 0))
    state = engine.verify_current()
    verification = state["task"]["verification"]
    assert verification["outcome"] == "VERIFIED", verification
    assert "pixels" in verification["summary"]
    assert any(e["rule"] == "ocr_value" and e["passed"] is True for e in verification["evidence"])
    assert observer.ocr.crops, "the field's pixels must have been read"


def test_a_misread_does_not_accuse_the_user(settings, db):
    """OCR is approximate; a different reading says "not sure", not "wrong"."""
    observer = FakeObserver(make_observation(values_unavailable=True))
    observer.backend.png = solid_png((255, 255, 255))
    observer.ocr = FakeOcr("GuidePilot Acceptanc")
    engine = build_engine(settings, db, observer)
    engine.create_task("Walk me through configuring this application", context_values=CONTEXT)

    observer.backend.png = solid_png((0, 0, 0))
    state = engine.verify_current()
    verification = state["task"]["verification"]
    assert verification["outcome"] == "INCONCLUSIVE"
    assert state["task"]["steps"][0]["status"] != "failed"


def test_ocr_never_overturns_a_readable_wrong_value(settings, db):
    """When the tree says the field holds something else, that verdict stands."""
    observer = FakeObserver(
        make_observation(_filled_elements(**{"App Name": "Something Else"}), values_unavailable=False)
    )
    observer.backend.png = solid_png((255, 255, 255))
    observer.ocr = FakeOcr("GuidePilot Acceptance")
    engine = build_engine(settings, db, observer)
    engine.create_task("Walk me through configuring this application", context_values=CONTEXT)

    observer.backend.png = solid_png((0, 0, 0))
    state = engine.verify_current()
    verification = state["task"]["verification"]
    assert verification["outcome"] == "FAILED"
    assert observer.ocr.crops == [], "OCR must not run when the tree gave an answer"


def test_ocr_is_skipped_when_it_is_not_installed(settings, db):
    observer = FakeObserver(make_observation(values_unavailable=True))
    observer.backend.png = solid_png((255, 255, 255))
    observer.ocr = FakeOcr("GuidePilot Acceptance", available=False)
    engine = build_engine(settings, db, observer)
    engine.create_task("Walk me through configuring this application", context_values=CONTEXT)

    observer.backend.png = solid_png((0, 0, 0))
    state = engine.verify_current()
    assert state["task"]["verification"]["outcome"] == "INCONCLUSIVE"
    assert observer.ocr.crops == []


def _png_with_glyphs(width: int = 120, height: int = 40, glyphs: int = 0, bar: int = 0) -> bytes:
    """A field-coloured PNG with a band of dark 'text' pixels, or a caret bar."""
    rows = []
    for y in range(height):
        row = bytearray()
        row.append(0)  # filter byte
        for x in range(width):
            text = 12 <= y < 24 and 4 <= x < 4 + glyphs * 5
            caret = bar and 12 <= y < 26 and 4 <= x < 4 + bar
            row.extend(b"\x10\x10\x10" if (text or caret) else b"\xff\xff\xff")
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def test_typed_text_in_a_field_is_noticed_even_though_it_is_tiny(settings, db):
    """Twenty characters cover far less than 2% of a field, and still count.

    The whole-frame perceptual grid cannot see them: the field reads as
    unchanged and the user is told they typed nothing.
    """
    observer = FakeObserver(make_observation(_filled_elements(), values_unavailable=False))
    observer.backend.png = _png_with_glyphs()
    engine = build_engine(settings, db, observer)
    engine.create_task("Walk me through configuring this application", context_values=CONTEXT)

    observer.backend.png = _png_with_glyphs(glyphs=8)
    state = engine.verify_current()
    verification = state["task"]["verification"]
    assert verification["outcome"] == "INCONCLUSIVE", verification
    assert "visibly changed" in verification["summary"]


def test_an_empty_field_that_stays_empty_is_not_reported_as_typed(settings, db):
    observer = FakeObserver(make_observation(_filled_elements(), values_unavailable=False))
    observer.backend.png = _png_with_glyphs()
    engine = build_engine(settings, db, observer)
    engine.create_task("Walk me through configuring this application", context_values=CONTEXT)

    observer.backend.png = _png_with_glyphs(bar=1)  # a blinker, nothing more
    state = engine.verify_current()
    assert state["task"]["verification"]["outcome"] == "FAILED"


def _filled_elements(**values: str) -> list:
    elements = copy.deepcopy(list(FORM_ELEMENTS))
    for item in elements:
        if item.role == "entry" and item.name in values:
            item.value = values[item.name]
    return elements


def test_blank_read_on_a_visibly_filled_field_is_not_the_users_mistake(settings, db):
    """An empty read on a field whose pixels changed must not be a false failure.

    Some applications expose a value interface and still read back empty for text
    that is visibly on screen. Reporting "the field is empty" there accuses the
    user of a failure that did not happen.
    """
    observer = FakeObserver(make_observation(_filled_elements(), values_unavailable=False))
    observer.backend.png = solid_png((255, 255, 255))
    engine = build_engine(settings, db, observer)
    engine.create_task("Walk me through configuring this application", context_values=CONTEXT)

    observer.backend.png = solid_png((0, 0, 0))
    state = engine.verify_current()
    verification = state["task"]["verification"]
    assert verification["outcome"] == "INCONCLUSIVE", verification
    assert "visibly changed" in verification["summary"]
    assert state["task"]["steps"][0]["status"] != "failed"


def test_unreadable_wrong_text_is_still_a_real_failure(settings, db):
    """A value that can be read but is wrong keeps its verdict, pixels or not."""
    observer = FakeObserver(
        make_observation(_filled_elements(**{"App Name": "Something Else"}), values_unavailable=False)
    )
    observer.backend.png = solid_png((255, 255, 255))
    engine = build_engine(settings, db, observer)
    engine.create_task("Walk me through configuring this application", context_values=CONTEXT)

    observer.backend.png = solid_png((0, 0, 0))
    state = engine.verify_current()
    verification = state["task"]["verification"]
    assert verification["outcome"] == "FAILED"
    assert "Something Else" in verification["summary"]


def test_blank_read_on_an_untouched_field_stays_a_failure(settings, db):
    """Nothing typed and nothing changed: the field really is empty."""
    observer = FakeObserver(make_observation(_filled_elements(), values_unavailable=False))
    observer.backend.png = solid_png((255, 255, 255))
    engine = build_engine(settings, db, observer)
    engine.create_task("Walk me through configuring this application", context_values=CONTEXT)

    state = engine.verify_current()
    assert state["task"]["verification"]["outcome"] == "FAILED"


def test_readable_form_still_verifies_exactly(settings, db):
    """The same step with readable text must verify the exact value."""
    elements = copy.deepcopy(list(FORM_ELEMENTS))
    for item in elements:
        if item.role == "entry" and item.name == "App Name":
            item.value = "GuidePilot Acceptance"
        if item.role == "entry" and item.name == "Callback URL":
            item.value = "https://acceptance.example.com/cb"
    observer = FakeObserver(make_observation(elements, values_unavailable=False))
    engine = build_engine(settings, db, observer)
    engine.create_task("Walk me through configuring this application", context_values=CONTEXT)
    state = engine.verify_current()
    assert state["task"]["verification"]["outcome"] == "VERIFIED"
