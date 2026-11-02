"""What "I cannot read this field" is allowed to mean.

Applications differ wildly in what they publish. An empty field and an
application with no text support both read back as an empty string, and
confusing the two either accuses the user of a failure that did not happen or
claims a success that was never observed. These tests pin the distinction.
"""

from __future__ import annotations

import copy

from conftest import FORM_ELEMENTS, FakeObserver, make_observation, solid_png

from guidepilot.accessibility.atspi import fields_unreadable
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
