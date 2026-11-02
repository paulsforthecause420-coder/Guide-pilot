"""Persistence: tasks, workflows, audit trail, preferences, purge."""

from __future__ import annotations

import json

import pytest

from conftest import FakeObserver, make_observation

from guidepilot.models import ActionType, Step, Task, TaskState, VerificationRule
from guidepilot.providers.gateway import ModelGateway
from guidepilot.task_engine.engine import TaskEngine
from guidepilot.task_engine.planner import TaskPlanner
from guidepilot.task_engine.workflows import (
    check_drift,
    export_workflow,
    import_workflow,
    workflow_from_task,
    workflow_to_plan,
)
from guidepilot.verification.verifier import Verifier

CONTEXT = {"App Name": "GuidePilot Acceptance", "Callback URL": "https://acceptance.example.com/cb"}


def make_engine(settings, db, observer=None) -> TaskEngine:
    gateway = ModelGateway(settings)
    return TaskEngine(settings, observer or FakeObserver(make_observation()), TaskPlanner(settings, gateway), gateway, Verifier(), db)


def test_task_round_trip_through_the_database(settings, db):
    engine = make_engine(settings, db)
    state = engine.create_task("Walk me through configuring this application", context_values=CONTEXT)
    task_id = state["task"]["id"]
    stored = db.load_task(task_id)
    assert stored is not None
    assert stored["objective"] == "Walk me through configuring this application"
    assert len(stored["steps"]) == len(state["task"]["steps"])
    listed = db.list_tasks()
    assert any(item["id"] == task_id for item in listed)


def test_deleting_and_clearing_tasks(settings, db):
    engine = make_engine(settings, db)
    created = engine.create_task("Walk me through configuring this application", context_values=CONTEXT)
    assert db.delete_task(created["task"]["id"]) is True
    assert db.load_task(created["task"]["id"]) is None
    engine.create_task("Walk me through configuring this application", context_values=CONTEXT)
    assert db.clear_tasks() >= 1
    assert db.list_tasks() == []


def test_audit_trail_records_events(settings, db):
    engine = make_engine(settings, db)
    engine.create_task("Walk me through configuring this application", context_values=CONTEXT)
    engine.skip_current("manual")
    kinds = [event["kind"] for event in db.list_audit()]
    assert kinds, "no audit events were recorded"
    assert db.clear_audit() >= 1


def test_preferences_are_stored_and_read_back(db):
    db.set_preference("panel.opacity", 0.72)
    assert db.get_preference("panel.opacity") == 0.72
    assert db.get_preference("missing", "fallback") == "fallback"


def test_usage_summary_counts_model_calls(db):
    db.log_model_call(role="reasoning", provider="openai_compat", model="gpt-test", ok=True, tokens=30)
    usage = db.usage_summary()
    assert usage["total_calls"] == 1
    assert usage["by_role"][0]["tokens"] == 30
    assert usage["by_role"][0]["successes"] == 1


def test_purge_removes_everything(settings, db):
    engine = make_engine(settings, db)
    engine.create_task("Walk me through configuring this application", context_values=CONTEXT)
    db.save_workflow({"name": "w", "steps": []})
    counts = db.purge_all()
    assert sum(counts.values()) >= 1
    assert db.list_tasks() == []
    assert db.list_workflows() == []
    assert db.list_audit() == []


def test_workflow_captures_steps_without_secrets():
    task = Task(
        id="task-1",
        objective="Configure the integration",
        state=TaskState.COMPLETED,
        steps=[
            Step(
                id="step-001",
                index=0,
                title="Enter App Name",
                instruction="Enter the name",
                action_type=ActionType.TYPE,
                target_description="App Name",
                target_selector={"role": "entry", "label": "App Name"},
                text_value={"value": "GuidePilot Acceptance", "source": "user_provided"},
                rules=[VerificationRule("element_value_equals", {"label": "App Name", "value": "GuidePilot Acceptance"})],
            ),
            Step(
                id="step-002",
                index=1,
                title="Enter API key",
                instruction="Enter the key",
                action_type=ActionType.TYPE,
                target_description="API key",
                target_selector={"role": "password text", "label": "API key"},
                text_value={"value": "s3cr3t", "source": "user_provided", "sensitive": True},
                rules=[VerificationRule("user_confirmation", {"label": "API key"})],
            ),
        ],
    )
    workflow = workflow_from_task(task, "Configure integration")
    dumped = json.dumps(workflow)
    assert "s3cr3t" not in dumped, "a secret leaked into the saved workflow"
    assert len(workflow["steps"]) == 2
    assert workflow["fingerprint"]


def test_workflow_round_trips_through_export_and_import():
    task = Task(id="t", objective="Do it", state=TaskState.COMPLETED, steps=[
        Step(id="s1", index=0, title="Click Save", instruction="Save", target_description="Save", target_selector={"role": "push button"})
    ])
    workflow = workflow_from_task(task)
    exported = export_workflow(workflow)
    restored = import_workflow(exported)
    assert restored["name"] == workflow["name"]
    assert len(restored["steps"]) == len(workflow["steps"])


def test_import_rejects_a_broken_payload():
    with pytest.raises(Exception):
        import_workflow({"name": "", "steps": "not a list"})


def test_workflow_becomes_a_plan_that_asks_for_unknown_values():
    task = Task(
        id="t",
        objective="Configure",
        state=TaskState.COMPLETED,
        steps=[
            Step(
                id="s1",
                index=0,
                title="Enter App Name",
                instruction="Type it",
                action_type=ActionType.TYPE,
                target_description="App Name",
                target_selector={"role": "entry"},
            )
        ],
    )
    plan = workflow_to_plan(workflow_from_task(task))
    assert plan.source == "workflow"
    assert plan.steps[0].requires_user_input is True


def test_drift_detects_a_changed_screen():
    elements = [element for element in make_observation().elements]
    workflow = {"steps": [{"target_description": "App Name", "target_role": "entry"}]}
    matched = check_drift(workflow, make_observation(elements))
    assert matched["checked"] is True
    assert matched["matched"] == 1

    workflow_missing = {"steps": [{"target_description": "Invoice total", "target_role": "entry"}]}
    missing = check_drift(workflow_missing, make_observation(elements))
    assert missing["missing"] == ["Invoice total"]
