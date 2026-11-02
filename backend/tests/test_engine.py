"""The task engine may only advance on evidence, and must stop honestly."""

from __future__ import annotations

import copy

import pytest

from conftest import FORM_ELEMENTS, FakeObserver, element, make_observation

from guidepilot.models import ActionType, StepStatus, TaskState
from guidepilot.providers.gateway import ModelGateway
from guidepilot.task_engine.engine import EngineError, TaskEngine, MAX_STEP_ATTEMPTS
from guidepilot.task_engine.planner import TaskPlanner
from guidepilot.verification.verifier import Verifier


def build_engine(settings, db, observer) -> TaskEngine:
    # A real gateway configured with ai.provider = "none": the offline path has
    # to work exactly like this, so the tests use it rather than hiding it.
    gateway = ModelGateway(settings)
    planner = TaskPlanner(settings, gateway=gateway)
    return TaskEngine(settings, observer, planner, gateway=gateway, verifier=Verifier(), db=db)


def filled(elements=None, change_ratio: float = 0.05):
    """A screen where the first text field has been typed into."""

    def screen(index: int):
        items = copy.deepcopy(list(elements or FORM_ELEMENTS))
        if index > 0:
            for item in items:
                if item.role == "entry" and item.name == "App Name":
                    item.value = "GuidePilot Acceptance"
                if item.role == "entry" and item.name == "Callback URL":
                    item.value = "https://acceptance.example.com/api/oauth/callback"
        return make_observation(items, change_ratio=change_ratio)

    return screen


CONTEXT = {
    "App Name": "GuidePilot Acceptance",
    "Callback URL": "https://acceptance.example.com/api/oauth/callback",
}


def test_create_task_plans_from_the_observed_form(settings, db):
    engine = build_engine(settings, db, FakeObserver(make_observation()))
    state = engine.create_task("Walk me through configuring this application", context_values=CONTEXT)
    task = state["task"]
    assert task["steps"], "the planner found no steps in an obvious form"
    assert task["state"] in {"READY_FOR_USER", "OBSERVING"}
    assert task["steps"][0]["status"] == "active"
    assert state["guidance"]["highlight"] is not None
    assert state["guidance"]["highlight"]["rect"], "no rectangle was computed for the first control"
    assert task["target_window_id"] == "0xabc"


def test_creating_a_task_without_an_objective_is_rejected(settings, db):
    engine = build_engine(settings, db, FakeObserver(make_observation()))
    with pytest.raises(EngineError) as excinfo:
        engine.create_task("   ")
    assert excinfo.value.code == "invalid_objective"


def test_unperformed_step_is_never_verified(settings, db):
    engine = build_engine(settings, db, FakeObserver(make_observation()))
    engine.create_task("Walk me through configuring this application", context_values=CONTEXT)
    state = engine.verify_current()
    outcome = state["task"]["verification"]["outcome"]
    assert outcome in {"FAILED", "INCONCLUSIVE"}
    assert state["task"]["current_index"] == 0
    assert state["task"]["steps"][0]["status"] != "verified"


def test_verified_step_advances_and_persists(settings, db):
    engine = build_engine(settings, db, FakeObserver(filled()))
    engine.create_task("Walk me through configuring this application", context_values=CONTEXT)
    first = engine.current_task.steps[0].title
    state = engine.verify_current()
    assert state["task"]["verification"]["outcome"] == "VERIFIED"
    assert state["task"]["steps"][0]["status"] == "verified"
    assert state["task"]["current_index"] == 1
    assert state["task"]["steps"][1]["title"] != first
    # Persistence: a fresh engine reading the same database sees the progress.
    stored = db.load_task(state["task"]["id"])
    assert stored is not None and stored["steps"][0]["status"] == "verified"


def test_repeated_failure_blocks_instead_of_looping(settings, db):
    engine = build_engine(settings, db, FakeObserver(make_observation()))
    engine.create_task("Walk me through configuring this application", context_values=CONTEXT)
    states = []
    for _ in range(MAX_STEP_ATTEMPTS + 1):
        try:
            states.append(engine.verify_current()["task"]["state"])
        except EngineError:
            # Verifying a blocked task is refused rather than restarted: the
            # engine will not silently resume a task it just gave up on.
            break
    task = engine.current_task
    assert task.state == TaskState.BLOCKED
    assert task.steps[0].status == StepStatus.FAILED
    assert task.steps[0].attempts >= MAX_STEP_ATTEMPTS
    assert task.guidance is not None and "can't verify" in task.guidance.instruction


def test_skip_advances_but_records_the_absence_of_proof(settings, db):
    engine = build_engine(settings, db, FakeObserver(make_observation()))
    engine.create_task("Walk me through configuring this application", context_values=CONTEXT)
    state = engine.skip_current("I will do this myself")
    assert state["task"]["steps"][0]["status"] == "skipped"
    assert state["task"]["current_index"] == 1
    assert any(entry.get("kind") == "step_skipped" for entry in state["task"]["history"])


def test_pause_resume_and_cancel(settings, db):
    engine = build_engine(settings, db, FakeObserver(make_observation()))
    engine.create_task("Walk me through configuring this application", context_values=CONTEXT)
    paused = engine.pause("testing")
    assert paused["task"]["state"] == "PAUSED"
    resumed = engine.resume()
    assert resumed["task"]["state"] != "PAUSED"
    cancelled = engine.cancel("done for now")
    assert cancelled["task"]["state"] == "CANCELLED"


def test_emergency_stop_prevents_new_tasks_until_cleared(settings, db):
    engine = build_engine(settings, db, FakeObserver(make_observation()))
    engine.create_task("Walk me through configuring this application", context_values=CONTEXT)
    stopped = engine.emergency_stop()
    assert stopped["stopped"] is True
    assert engine.current_task.state == TaskState.PAUSED
    assert engine.settings.get("privacy.share_enabled") is False
    with pytest.raises(EngineError) as excinfo:
        engine.create_task("another objective")
    assert excinfo.value.code == "emergency_stopped"
    engine.clear_stop()
    assert engine.create_task("Walk me through configuring this application")["task"]["steps"]


def test_confirmation_marks_a_step_that_needed_the_user(settings, db):
    screen = make_observation(FORM_ELEMENTS)
    engine = build_engine(settings, db, FakeObserver(screen))
    state = engine.create_task("Walk me through configuring this application")
    step = state["task"]["steps"][0]
    assert step["requires_user_input"] is True, "a value the user must supply should be requested, not invented"
    confirmed = engine.confirm_current()
    assert confirmed["task"]["steps"][0]["status"] in {"verified", "user_confirmed"}
    assert confirmed["task"]["current_index"] == 1


def test_watch_tick_does_not_advance_on_an_unchanged_screen(settings, db):
    engine = build_engine(settings, db, FakeObserver(make_observation(change_ratio=0.0)))
    engine.create_task("Walk me through configuring this application", context_values=CONTEXT)
    state = engine.watch_tick()
    assert state["task"]["current_index"] == 0
    assert state["task"]["steps"][0]["status"] != "verified"


def test_non_terminal_objective_on_an_empty_screen_asks_instead_of_inventing(settings, db):
    engine = build_engine(settings, db, FakeObserver(make_observation([])))
    state = engine.create_task("Book me a flight to Lisbon", context_values={})
    steps = state["task"]["steps"]
    # No shell command may be offered for an objective that is not terminal work.
    assert all(step["action_type"] != "command" for step in steps), steps
    assert all(not (step["text_value"] or {}).get("value") for step in steps)
    assert steps[0]["requires_user_input"] is True
    assert state["task"]["summary"]


def test_secret_context_values_are_not_echoed(settings, db):
    engine = build_engine(settings, db, FakeObserver(make_observation()))
    state = engine.create_task(
        "Walk me through configuring this application",
        context_values={"API key": {"value": "s3cr3t-value", "sensitive": True}},
    )
    serialized = str(state)
    assert "s3cr3t-value" not in serialized


def test_step_without_rules_is_never_auto_verified(settings, db):
    from guidepilot.task_engine.planner import Plan, PlanStep

    plan = Plan(
        title="Experimental",
        summary="Experimental",
        steps=[
            PlanStep(
                title="Save the form",
                instruction="Click Save Configuration.",
                action_type=ActionType.CLICK,
                target_label="Save Configuration",
                target_role="push button",
            )
        ],
        source="local",
    )
    engine = build_engine(settings, db, FakeObserver(make_observation()))
    state = engine.create_task(
        "Walk me through configuring this application", context_values=CONTEXT, plan_override=plan
    )
    assert state["task"]["steps"][0]["rules"] == []
    verified = engine.verify_current()
    assert verified["task"]["verification"]["outcome"] == "INCONCLUSIVE"
    assert verified["task"]["steps"][0]["status"] != "verified"


def test_history_lists_persisted_tasks(settings, db):
    engine = build_engine(settings, db, FakeObserver(make_observation()))
    engine.create_task("Walk me through configuring this application", context_values=CONTEXT)
    history = engine.history()
    assert history and history[0]["objective"]
