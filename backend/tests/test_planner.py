"""Planning and value resolution: exact instructions, no invented values."""

from __future__ import annotations

from conftest import FORM_ELEMENTS, make_observation

from guidepilot.security.risk import classify_command
from guidepilot.task_engine.planner import local_plan
from guidepilot.task_engine.values import derive_callback_url, missing_information, resolve_value


def plan_for(objective: str, context=None, project_dir=None, elements=None):
    observation = make_observation(elements if elements is not None else FORM_ELEMENTS)
    return local_plan(objective, observation, context or {}, project_dir)


CONTEXT = {
    "App Name": "GuidePilot Acceptance",
    "Callback URL": "https://acceptance.example.com/api/oauth/callback",
}


def test_form_plan_follows_the_observed_field_order():
    plan = plan_for("Walk me through configuring this application", CONTEXT)
    titles = [step.title for step in plan.steps]
    assert titles[0] == "Enter App Name"
    assert titles[1] == "Enter Callback URL"
    assert "Region" in titles[2]
    assert titles[-1].startswith("Click ")
    assert plan.source == "local"


def test_exact_values_are_taken_from_what_the_user_supplied():
    plan = plan_for("Walk me through configuring this application", CONTEXT)
    first = plan.steps[0]
    assert first.text_value is not None
    assert first.text_value["value"] == "GuidePilot Acceptance"
    assert first.text_value["source"] == "user_provided"
    assert first.requires_user_input is False
    assert any(rule.kind == "element_value_equals" for rule in first.rules)


def test_unknown_field_becomes_a_question_not_an_invention():
    plan = plan_for("Walk me through configuring this application", {"App Name": "Only this one"})
    second = plan.steps[1]
    assert second.text_value is None
    assert second.requires_user_input is True
    assert any(rule.kind == "user_confirmation" for rule in second.rules)
    assert any(item["field"] == "Callback URL" for item in plan.missing_information)


def test_password_fields_are_never_filled_by_the_assistant():
    elements = [
        FORM_ELEMENTS[0],
        FORM_ELEMENTS[1],
        type(FORM_ELEMENTS[1])(id="el-pw", name="Password", role="password text", rect=FORM_ELEMENTS[1].rect),
    ]
    plan = plan_for("Walk me through configuring this application", {"App Name": "x", "Password": "hunter2"}, elements=elements)
    password_step = next(step for step in plan.steps if "Password" in step.title)
    assert password_step.text_value["sensitive"] is True
    assert "hunter2" not in str(plan.to_dict())


def test_non_terminal_objective_never_produces_shell_commands():
    plan = plan_for("Book me a flight to Lisbon", {})
    assert all(step.action_type.value != "command" for step in plan.steps)
    assert plan.missing_information


def test_terminal_objective_produces_reviewed_commands():
    # A terminal objective with no form on screen: the command path applies.
    plan = plan_for("install nginx for me", {}, elements=[])
    commands = [step.text_value["value"] for step in plan.steps if step.text_value]
    assert any("apt" in command for command in commands)
    install = next(step for step in plan.steps if "Install" in step.title)
    assert install.risk == "modify"
    assert classify_command(install.text_value["value"]).requires_confirmation is True
    assert classify_command(install.text_value["value"]).level != "inspect"


def test_resolve_value_prefers_context_then_project_config(project_dir):
    (project_dir / ".env").write_text("APP_NAME=FromProjectConfig\n", encoding="utf-8")
    from_config = resolve_value("App Name", {}, project_dir=project_dir)
    assert from_config["value"] == "FromProjectConfig"
    assert from_config["source"] == "project_config"

    from_context = resolve_value("App Name", {"App Name": "FromUser"}, project_dir=project_dir)
    assert from_context["value"] == "FromUser"
    assert from_context["source"] == "user_provided"


def test_resolve_value_returns_none_instead_of_guessing(project_dir):
    assert resolve_value("Favourite colour", {}, project_dir=project_dir) is None


def test_sensitive_value_never_reads_from_project_config(project_dir):
    (project_dir / ".env").write_text("API_KEY=super-secret\n", encoding="utf-8")
    value = resolve_value("API key", {}, project_dir=project_dir)
    assert value["value"] == ""
    assert value["sensitive"] is True
    assert "super-secret" not in str(value)


def test_callback_url_derivation_is_labelled_as_an_assumption():
    derived = derive_callback_url("https://example.com")
    assert derived["value"] == "https://example.com/api/oauth/callback"
    assert derived["source"] == "derived"
    assert "assumption" in derived["explanation"].lower()
    assert derive_callback_url("not a url") is None


def test_missing_information_lists_unresolved_fields():
    missing = missing_information(["App Name", "Callback URL"], {"App Name": "x"})
    assert [item["field"] for item in missing] == ["Callback URL"]
