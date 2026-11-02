"""Reusable workflows: save a completed task as a guide, check it against a live screen.

A workflow stores *structure* (labels, roles, instructions, verification rules)
and never credentials, screen pixels or secret values. Before reuse, the steps
are checked against the current screen; mismatched steps are reported so the
plan can adapt instead of confidently pointing at controls that no longer exist.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Sequence

from .. import util, validation
from ..grounding.matcher import best_match
from ..models import Element, Observation, Task, VerificationRule
from .planner import Plan, PlanStep, PlanStep as _PlanStep  # noqa: F401  (alias kept for typing clarity)
from ..models import ActionType

WORKFLOW_SCHEMA = "workflow"


def _safe_template(step) -> str:
    text_value = step.text_value or {}
    if not isinstance(text_value, dict):
        return ""
    if text_value.get("sensitive"):
        return ""
    source = text_value.get("source", "unknown")
    if source not in {"user_provided", "project_config", "derived", "documentation"}:
        return ""
    return str(text_value.get("value") or "")


def workflow_from_task(task: Task, name: str = "") -> dict[str, Any]:
    """Convert a task's steps into a reusable, credential-free workflow."""
    steps: list[dict[str, Any]] = []
    for step in task.steps:
        steps.append(
            {
                "title": step.title,
                "instruction": step.instruction,
                "action_type": step.action_type.value,
                "target_description": step.target_description or step.title,
                "target_role": str((step.target_selector or {}).get("role") or ""),
                "text_template": _safe_template(step),
                "expected_result": step.expected_result,
                "why": step.why,
                "verification": [
                    {
                        "kind": rule.kind,
                        "params": rule.params,
                        "required": rule.required,
                        "description": rule.description,
                    }
                    for rule in step.rules
                ],
            }
        )
    workflow = {
        "name": util.truncated(name or task.objective or "Saved workflow", 160),
        "description": util.truncated(task.summary or "", 800),
        "version": 1,
        "objective": util.truncated(task.objective, 1000),
        "app_hint": util.truncated(task.app_context, 200),
        "tags": [],
        "fingerprint": fingerprint(steps),
        "steps": steps,
    }
    validation.validate_against(WORKFLOW_SCHEMA, workflow)
    return workflow


def fingerprint(steps: Sequence[dict[str, Any]]) -> str:
    material = "|".join(
        f"{s.get('target_role','')}:{util.normalize_text(str(s.get('target_description','')))}" for s in steps
    )
    return hashlib.sha256(material.encode("utf-8", "replace")).hexdigest()[:24]


def workflow_to_plan(workflow: dict[str, Any], adaptation_notes: list[str] | None = None) -> Plan:
    """Turn a stored workflow into a runnable plan."""
    steps: list[PlanStep] = []
    for raw in workflow.get("steps", []):
        try:
            action = ActionType(raw.get("action_type", "click"))
        except ValueError:
            action = ActionType.MANUAL
        rules: list[VerificationRule] = []
        for item in raw.get("verification", []) or []:
            rule = VerificationRule.from_dict(item)
            if rule is not None:
                rules.append(rule)
        if not rules:
            rules = [VerificationRule("user_confirmation", {})]
        template = raw.get("text_template") or ""
        steps.append(
            PlanStep(
                title=str(raw.get("title", "Step")),
                instruction=str(raw.get("instruction", "")),
                action_type=action,
                target_label=str(raw.get("target_description") or ""),
                target_role=str(raw.get("target_role") or ""),
                text_value=(
                    {"value": template, "source": "user_provided", "sensitive": False, "explanation": "From the saved workflow"}
                    if template
                    else None
                ),
                expected_result=str(raw.get("expected_result", "")),
                why=str(raw.get("why", "")),
                rules=rules,
                requires_user_input=not template and action in {ActionType.TYPE, ActionType.SELECT},
            )
        )
    return Plan(
        title=workflow.get("name", "Workflow"),
        summary=workflow.get("description", ""),
        steps=steps,
        source="workflow",
        notes=list(adaptation_notes or []) + ["Loaded from your saved workflow library."],
    )


def check_drift(workflow: dict[str, Any], observation: Observation | None) -> dict[str, Any]:
    """Compare workflow targets with the live screen."""
    if observation is None:
        return {"checked": False, "reason": "no observation available", "missing": [], "matched": 0, "total": 0}
    elements: list[Element] = list(observation.elements)
    missing: list[str] = []
    matched = 0
    total = 0
    for raw in workflow.get("steps", []):
        label = str(raw.get("target_description") or "").strip()
        if not label:
            continue
        total += 1
        result = best_match(
            elements,
            description=label,
            role_hint=str(raw.get("target_role") or ""),
            monitors=observation.monitors,
            min_confidence=0.55,
        )
        if result.ok:
            matched += 1
        else:
            missing.append(label)
    return {
        "checked": True,
        "matched": matched,
        "total": total,
        "missing": missing,
        "matches_expected": not missing,
        "ratio": (matched / total) if total else 0.0,
    }


def export_workflow(workflow: dict[str, Any]) -> str:
    validation.validate_against(WORKFLOW_SCHEMA, workflow)
    return json.dumps(workflow, indent=2, sort_keys=True)


def import_workflow(payload: str | dict[str, Any]) -> dict[str, Any]:
    """Validate and normalise an imported workflow."""
    if isinstance(payload, str):
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise validation.ValidationError([f"Invalid JSON: {exc}"]) from exc
    else:
        data = payload
    if not isinstance(data, dict):
        raise validation.ValidationError(["Workflow must be a JSON object"])
    # Drop unknown top-level keys such as an id from another machine.
    allowed = {"name", "description", "version", "objective", "app_hint", "tags", "fingerprint", "steps", "created_at", "updated_at", "id"}
    cleaned = {k: v for k, v in data.items() if k in allowed}
    cleaned.pop("id", None)
    cleaned.pop("created_at", None)
    cleaned.pop("updated_at", None)
    cleaned.setdefault("version", 1)
    validation.validate_against(WORKFLOW_SCHEMA, cleaned)
    cleaned["fingerprint"] = fingerprint(cleaned["steps"])
    return cleaned
