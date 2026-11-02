"""Task planning: a local form/command planner and an optional model planner.

The local planner is not a stand-in for an AI: it is the product's offline path.
It reasons over the *observed* accessibility elements (labels, roles, order) and
the user's own supplied values, which is enough to guide a form step by step.
The model planner is used when configured, and its output is validated against
``shared/schemas/plan.schema.json`` before any step is accepted.

Neither planner may invent credentials, URLs or identifiers: unknown values
become an explicit question to the user.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .. import util
from ..grounding.matcher import ROLE_ALIASES, rank_candidates
from ..models import ActionType, Element, Observation, Rect, VerificationRule
from ..providers.base import ChatMessage, ProviderError
from ..providers.gateway import ROLE_REASONING, ModelGateway
from ..security.injection import UNTRUSTED_DATA_RULE, wrap_untrusted
from ..security.redaction import is_sensitive_label, redact_elements
from ..security.risk import classify_command
from .values import missing_information, resolve_value

TEXT_ROLES = {"entry", "text", "password text", "spin button", "text field"}
SELECT_ROLES = {"combo box", "list box"}
CHECK_ROLES = {"check box", "radio button"}

ACTION_WORDS = {
    "save",
    "submit",
    "apply",
    "continue",
    "next",
    "finish",
    "create",
    "confirm",
    "install",
    "connect",
    "register",
    "sign in",
    "log in",
    "enable",
    "start",
    "run",
}

SUCCESS_WORDS = ["saved", "success", "successfully", "updated", "applied", "complete", "configured", "created"]

KNOWN_PACKAGES = [
    "postgresql",
    "nginx",
    "docker",
    "docker-compose",
    "nodejs",
    "python3",
    "git",
    "redis",
    "mysql",
    "mariadb",
    "apache2",
    "mongodb",
    "ollama",
    "vscode",
    "golang",
    "rust",
]


class PlanningUnavailable(RuntimeError):
    """Model planning was requested but is not possible right now."""


@dataclass
class PlanStep:
    title: str
    instruction: str
    action_type: ActionType
    target_label: str = ""
    target_role: str = ""
    text_value: dict[str, Any] | None = None
    expected_result: str = ""
    why: str = ""
    rules: list[VerificationRule] = field(default_factory=list)
    requires_user_input: bool = False
    risk: str = "none"

    def to_selector(self) -> dict[str, Any]:
        selector = {"label": self.target_label, "role": self.target_role}
        aliases = ROLE_ALIASES.get(util.normalize_text(self.target_role))
        if aliases:
            selector["role_aliases"] = sorted(aliases)
        return selector


@dataclass
class Plan:
    title: str
    summary: str
    steps: list[PlanStep]
    source: str
    missing_information: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "summary": self.summary,
            "source": self.source,
            "steps": [
                {
                    "title": s.title,
                    "instruction": s.instruction,
                    "action_type": s.action_type.value,
                    "target_label": s.target_label,
                    "target_role": s.target_role,
                    "text_value": s.text_value,
                    "expected_result": s.expected_result,
                    "why": s.why,
                    "requires_user_input": s.requires_user_input,
                    "risk": s.risk,
                    "rules": [r.to_dict() for r in s.rules],
                }
                for s in self.steps
            ],
            "missing_information": self.missing_information,
            "notes": self.notes,
        }


# ---------------------------------------------------------------------------
# Local planner
# ---------------------------------------------------------------------------


def _inside(element: Element, rect: Rect | None) -> bool:
    if rect is None:
        return True
    return rect.inflate(4).intersects(element.rect)


def _action_buttons(elements: Sequence[Element]) -> list[Element]:
    buttons = [e for e in elements if e.role in {"push button", "toggle button"} or "button" in e.role]
    primary = [b for b in buttons if any(w in b.searchable_text() for w in ACTION_WORDS)]
    return primary or buttons


def _ordered(elements: Iterable[Element]) -> list[Element]:
    return sorted(elements, key=lambda e: (e.rect.y, e.rect.x))


def _label_for(element: Element, elements: Sequence[Element]) -> str:
    if element.name and util.normalize_text(element.name):
        return element.name.strip()
    # Look for a static label immediately left of the field.
    best: tuple[int, Element] | None = None
    for candidate in elements:
        if candidate is element or candidate.role not in {"static", "label", "heading", "text"}:
            continue
        if not candidate.name:
            continue
        vertical_overlap = min(candidate.rect.bottom, element.rect.bottom) - max(candidate.rect.y, element.rect.y)
        if vertical_overlap <= 0:
            continue
        gap = element.rect.x - candidate.rect.right
        if 0 <= gap < 60 and (best is None or gap < best[0]):
            best = (gap, candidate)
    return best[1].name.strip() if best else element.role


def detect_form(elements: Sequence[Element], window: Rect | None) -> dict[str, list[Element]]:
    scoped = [e for e in elements if _inside(e, window) and not e.rect.is_degenerate()]
    text_fields = [e for e in scoped if e.role in TEXT_ROLES]
    selects = [e for e in scoped if e.role in SELECT_ROLES]
    checks = [e for e in scoped if e.role in CHECK_ROLES]
    buttons = _action_buttons(scoped)
    return {
        "text": _ordered(text_fields),
        "select": _ordered(selects),
        "check": _ordered(checks),
        "buttons": _ordered(buttons),
        "all": scoped,
    }


def _field_step(
    element: Element,
    label: str,
    action: ActionType,
    context_values: dict[str, Any],
    project_dir: str | None,
) -> PlanStep:
    value = resolve_value(label, context_values, project_dir=project_dir)
    sensitive = is_sensitive_label(label)
    if sensitive:
        return PlanStep(
            title=f"Enter {label}",
            instruction=(
                f"Click the '{label}' field and enter your value privately. "
                "I will not display, store or send this secret anywhere."
            ),
            action_type=action,
            target_label=label,
            target_role=element.role,
            text_value={"value": "", "source": "unknown", "sensitive": True, "explanation": "Sensitive field"},
            expected_result=f"The '{label}' field is filled in.",
            why="This credential is required by the form but must not leave your machine.",
            rules=[VerificationRule("user_confirmation", {"label": label})],
            requires_user_input=True,
        )
    if value is None:
        return PlanStep(
            title=f"Enter {label}",
            instruction=(
                f"Click the '{label}' field and type the value you want to use. "
                f"Tell me the value if you would like me to check it afterwards."
            ),
            action_type=action,
            target_label=label,
            target_role=element.role,
            text_value=None,
            expected_result=f"The '{label}' field is filled in.",
            why="I do not invent values for this field: it has to come from you.",
            rules=[VerificationRule("user_confirmation", {"label": label})],
            requires_user_input=True,
        )
    explanation = value.get("explanation") or ""
    source = value.get("source", "user_provided")
    return PlanStep(
        title=f"Enter {label}",
        instruction=f"Click the '{label}' field and type the exact value shown.",
        action_type=action,
        target_label=label,
        target_role=element.role,
        text_value={
            "value": value["value"],
            "source": source,
            "sensitive": bool(value.get("sensitive")),
            "explanation": explanation,
        },
        expected_result=f"'{label}' contains '{value['value']}'.",
        why=explanation or "The form requires this value before it can be saved.",
        rules=[
            VerificationRule("element_value_equals", {"label": label, "value": value["value"], "role": element.role}),
        ],
        requires_user_input=False,
    )


def local_plan(
    objective: str,
    observation: Observation | None,
    context_values: dict[str, Any] | None = None,
    project_dir: str | None = None,
    app_hint: str = "",
) -> Plan:
    context_values = context_values or {}
    elements = list(observation.elements) if observation else []
    window = observation.active_window.rect if observation and observation.active_window else None
    form = detect_form(elements, window)

    if form["text"] or form["select"] or form["check"]:
        steps: list[PlanStep] = []
        labels_needed: list[str] = []
        for element in form["text"]:
            label = _label_for(element, form["all"])
            labels_needed.append(label)
            steps.append(_field_step(element, label, ActionType.TYPE, context_values, project_dir))
        for element in form["select"]:
            label = _label_for(element, form["all"])
            value = resolve_value(label, context_values, project_dir=project_dir)
            if value is None:
                steps.append(
                    PlanStep(
                        title=f"Choose {label}",
                        instruction=f"Open the '{label}' dropdown and choose the option you need.",
                        action_type=ActionType.SELECT,
                        target_label=label,
                        target_role=element.role,
                        expected_result=f"'{label}' shows the selected option.",
                        why="This option affects how the application behaves.",
                        rules=[VerificationRule("user_confirmation", {"label": label})],
                        requires_user_input=True,
                    )
                )
            else:
                steps.append(
                    PlanStep(
                        title=f"Choose {label}",
                        instruction=f"Open the '{label}' dropdown and choose '{value['value']}'.",
                        action_type=ActionType.SELECT,
                        target_label=label,
                        target_role=element.role,
                        text_value=value,
                        expected_result=f"'{label}' is set to '{value['value']}'.",
                        why=value.get("explanation") or "This is the value you provided for this setting.",
                        rules=[
                            VerificationRule("element_value_equals", {"label": label, "value": value["value"]}),
                            VerificationRule("screen_changed", {"min_ratio": 0.0005}, required=False),
                        ],
                    )
                )
        for element in form["check"]:
            label = _label_for(element, form["all"])
            value = resolve_value(label, context_values, project_dir=project_dir)
            decision = "tick" if not value or value["value"].lower() in {"true", "yes", "1", "on", "enable", "enabled"} else "clear"
            if value is None:
                steps.append(
                    PlanStep(
                        title=f"Review {label}",
                        instruction=f"Set the '{label}' checkbox according to what you need, then confirm.",
                        action_type=ActionType.CHECK,
                        target_label=label,
                        target_role=element.role,
                        expected_result=f"'{label}' is set as you intended.",
                        why="This option changes what the application does.",
                        rules=[VerificationRule("user_confirmation", {"label": label})],
                        requires_user_input=True,
                    )
                )
            else:
                steps.append(
                    PlanStep(
                        title=f"{decision.capitalize()} {label}",
                        instruction=f"{decision.capitalize()} the '{label}' checkbox.",
                        action_type=ActionType.CHECK,
                        target_label=label,
                        target_role=element.role,
                        text_value=value,
                        expected_result=f"'{label}' is {'ticked' if decision == 'tick' else 'unticked'}.",
                        why=value.get("explanation") or "This matches the preference you provided.",
                        rules=[
                            VerificationRule(
                                "element_checked",
                                {"label": label, "role": element.role, "checked": decision == "tick"},
                            )
                        ],
                    )
                )
        for button in form["buttons"]:
            label = button.name.strip() or "the primary action button"
            steps.append(
                PlanStep(
                    title=f"Click {label}",
                    instruction=f"Click the '{label}' button when you are ready.",
                    action_type=ActionType.CLICK,
                    target_label=label,
                    target_role=button.role,
                    expected_result="The application confirms the configuration was saved.",
                    why="Nothing is stored until the form is submitted.",
                    rules=[
                        VerificationRule(
                            "any_of",
                            {
                                "rules": [
                                    {"kind": "text_present", "params": {"any_of": SUCCESS_WORDS}},
                                    {"kind": "element_disappears", "params": {"label": label}},
                                ]
                            },
                            description="A success message appears or the form closes",
                        )
                    ],
                )
            )
            break
        return Plan(
            title=_plan_title(objective, app_hint),
            summary=(
                f"I found {len(form['text'])} text field(s), {len(form['select'])} dropdown(s), "
                f"{len(form['check'])} checkbox(es) and {len(form['buttons'])} action button(s) on the current screen."
            ),
            steps=steps,
            source="local",
            missing_information=missing_information(labels_needed, context_values, project_dir),
            notes=[
                "Plan built from the accessibility tree of the current window.",
                "Values come only from what you provided or from your project configuration.",
            ],
        )

    if _terminal_intent(objective, observation):
        return _command_plan(objective, observation, context_values, app_hint)
    return _clarification_plan(objective, observation, app_hint)


TERMINAL_INTENT_WORDS = (
    "install",
    "uninstall",
    "package",
    "apt",
    "dnf",
    "pacman",
    "terminal",
    "command",
    "shell",
    "bash",
    "service",
    "systemctl",
    "daemon",
    "port",
    "log",
    "permission",
    "permission",
    "sudo",
    "compile",
    "build",
    "script",
)
TERMINAL_APPS = ("terminal", "konsole", "xterm", "alacritty", "kitty", "tilix", "terminator", "gnome-terminal")


def _terminal_intent(objective: str, observation: Observation | None) -> bool:
    """Whether this objective is terminal work at all.

    Without this gate a request like "book me a flight" would be answered with a
    shell command, which is exactly the kind of invented guidance the product
    promises never to give.
    """
    lowered = (objective or "").casefold()
    if any(word in lowered for word in TERMINAL_INTENT_WORDS):
        return True
    if any(word in lowered for word in KNOWN_PACKAGES):
        return True
    window = observation.active_window if observation else None
    if window is not None:
        klass = (window.app_class or "").casefold()
        title = (window.title or "").casefold()
        return any(app in klass or app in title for app in TERMINAL_APPS)
    return False


def _clarification_plan(objective: str, observation: Observation | None, app_hint: str) -> Plan:
    """No form and no terminal intent: ask, instead of guessing."""
    window = observation.active_window if observation else None
    where = f"the window '{util.truncated(window.title, 80)}'" if window else "the current screen"
    return Plan(
        title=_plan_title(objective, app_hint),
        summary=(
            f"I can see {where}, but it exposes no form I can guide you through, "
            "so I would have to guess. Tell me which screen or application to use."
        ),
        steps=[
            PlanStep(
                title="Tell me where to look",
                instruction=(
                    "Open the window with the form or control you want to change, then choose "
                    "\"Look again\". I only guide controls I can actually see and verify."
                ),
                action_type=ActionType.OBSERVE,
                expected_result="The window with the relevant controls is in front.",
                why="I will not invent steps for a screen I cannot inspect.",
                rules=[VerificationRule("user_confirmation", {})],
                requires_user_input=True,
            )
        ],
        source="local",
        missing_information=[{"field": "target", "question": "Which window or application should I guide you through?"}],
        notes=["No fillable form was visible on the current screen, so this plan asks instead of inventing one."],
    )


def _plan_title(objective: str, app_hint: str) -> str:
    target = app_hint.strip() or "this application"
    return util.truncated(f"Guide: {util.truncated(objective.strip(), 90) or 'complete this form'}", 140)


def _command_plan(
    objective: str,
    observation: Observation | None,
    context_values: dict[str, Any],
    app_hint: str,
) -> Plan:
    lowered = (objective or "").casefold()
    package = next((p for p in KNOWN_PACKAGES if p in lowered), "")
    notes = [
        "No fillable form was detected on the current screen, so this plan guides terminal work instead.",
        "GuidePilot never runs these commands: you review and run them yourself.",
    ]
    if not package:
        steps = [
            PlanStep(
                title="Inspect the environment",
                instruction="Run this to see what desktop and kernel you are on before changing anything.",
                action_type=ActionType.COMMAND,
                text_value={"value": "uname -a && lsb_release -a", "source": "derived", "sensitive": False},
                expected_result="Your distribution and kernel version are visible.",
                why="Knowing the platform avoids installing the wrong package for your system.",
                rules=[VerificationRule("user_confirmation", {})],
                risk="inspect",
            ),
            PlanStep(
                title="Tell me the exact application or command",
                instruction=(
                    "There is no form on screen and I could not identify a package from your request. "
                    "Tell me the application, package or command you want to work with and I will build a precise plan."
                ),
                action_type=ActionType.CONFIRM,
                expected_result="The objective names a concrete package or application.",
                why="I will not invent installation commands for an unidentified target.",
                rules=[VerificationRule("user_confirmation", {})],
                requires_user_input=True,
            ),
        ]
        return Plan(
            title=_plan_title(objective, app_hint),
            summary="I need a bit more information before I can give exact commands.",
            steps=steps,
            source="local",
            missing_information=[{"field": "target", "question": "Which application or package should I guide you through?"}],
            notes=notes,
        )

    inspect_cmd = f"apt-cache policy {package}"
    install_cmd = f"sudo apt update && sudo apt install -y {package}"
    verify_cmd = f"systemctl status {package} --no-pager"
    install_risk = classify_command(install_cmd)
    steps = [
        PlanStep(
            title=f"Check whether {package} is already installed",
            instruction=f"Open a terminal and run this read-only check for {package}.",
            action_type=ActionType.COMMAND,
            text_value={"value": inspect_cmd, "source": "derived", "sensitive": False},
            expected_result=f"The candidate and installed versions for {package} are shown.",
            why="Skipping the install step is safer if the package is already present and current.",
            rules=[VerificationRule("user_confirmation", {})],
            risk="inspect",
        ),
        PlanStep(
            title=f"Install {package}",
            instruction=(
                f"This command installs packages and needs your approval. {install_risk.explanation} "
                "Review it, then run it in a terminal yourself."
            ),
            action_type=ActionType.COMMAND,
            text_value={"value": install_cmd, "source": "derived", "sensitive": False},
            expected_result=f"The package manager reports {package} installed without errors.",
            why="Installing is the system-changing action, so it is separated from the read-only check.",
            rules=[VerificationRule("user_confirmation", {})],
            risk="modify",
        ),
        PlanStep(
            title=f"Verify {package}",
            instruction=f"Run this read-only status check to confirm {package} is healthy.",
            action_type=ActionType.COMMAND,
            text_value={"value": verify_cmd, "source": "derived", "sensitive": False},
            expected_result=f"The service for {package} reports active (running), or the package is listed.",
            why="A successful install is not proof that the service works.",
            rules=[VerificationRule("user_confirmation", {})],
            risk="inspect",
        ),
    ]
    return Plan(
        title=_plan_title(objective, app_hint),
        summary=f"Terminal plan for {package}, with read-only checks separated from the system change.",
        steps=steps,
        source="local",
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Model planner
# ---------------------------------------------------------------------------


PLANNER_SYSTEM_PROMPT = (
    "You are GuidePilot's planning module. You receive the user's objective, the current application context and a "
    "list of real accessibility elements observed on screen (label, role, rect in physical screen pixels). "
    "Produce a short, ordered plan whose steps refer ONLY to controls that exist in that element list. "
    "Rules: never invent credentials, tokens, callback URLs, account ids, file paths or identifiers; put missing "
    "information in missing_information instead. Never instruct the user to disable security features. "
    "Reply with a single JSON object matching the provided schema and nothing else.\n\n"
    + UNTRUSTED_DATA_RULE
)


def build_planner_messages(
    objective: str,
    observation: Observation | None,
    context_values: dict[str, Any],
    app_hint: str,
    extra_context: str = "",
) -> list[ChatMessage]:
    elements = observation.elements if observation else []
    window = observation.active_window if observation else None
    lines = [
        f"OBJECTIVE: {util.truncated(objective, 600)}",
        f"APP_HINT: {app_hint or 'unknown'}",
    ]
    if window:
        lines.append(f"ACTIVE_WINDOW: {util.truncated(window.title, 120)} ({window.app_class}) rect={window.rect.to_list()}")
    if observation:
        lines.append(f"SESSION: {observation.session_type}; monitors={[m.to_dict() for m in observation.monitors][:2]}")
    inventory = redact_elements(elements)[:70]
    lines.append("ELEMENTS:")
    for index, element in enumerate(inventory):
        lines.append(
            f"  {index}: {element['role']} | {element['label']!r} | value={element['value']!r} | rect={element['rect']}"
        )
    if context_values:
        lines.append("USER_PROVIDED_VALUES: " + str({k: ("***" if is_sensitive_label(str(k)) else v) for k, v in context_values.items()}))
    if extra_context:
        lines.append(wrap_untrusted("documentation_or_notes", extra_context, limit=4000))
    lines.append(
        "Schema reminder: {\"title\":str,\"summary\":str,\"missing_information\":[{\"field\":str,\"question\":str}],"
        "\"steps\":[{\"title\":str,\"instruction\":str,\"action_type\":click|type|select|check|scroll|command|confirm|observe|manual,"
        "\"target_label\":str,\"target_role\":str,\"text_value\":{\"value\":str,\"source\":user_provided|project_config|derived|documentation|model_assumption|unknown}|null,"
        "\"expected_result\":str,\"why\":str,\"verification\":[{\"kind\":str,\"params\":{},\"required\":bool}],"
        "\"requires_user_input\":bool,\"risk\":none|inspect|modify|destructive}]}"
    )
    return [
        ChatMessage("system", PLANNER_SYSTEM_PROMPT),
        ChatMessage("user", "\n".join(lines)),
    ]


def plan_from_model_payload(payload: dict[str, Any]) -> Plan:
    steps: list[PlanStep] = []
    for raw in payload.get("steps", []):
        action = raw.get("action_type", "click")
        try:
            action_type = ActionType(action)
        except ValueError as exc:
            raise PlanningUnavailable(f"Model proposed an unsupported action type '{action}'") from exc
        rules = []
        for item in raw.get("verification", []) or []:
            rule = VerificationRule.from_dict(item)
            if rule is not None:
                rules.append(rule)
        if not rules:
            rules = [VerificationRule("user_confirmation", {})]
        text_value = raw.get("text_value")
        if isinstance(text_value, dict) and text_value.get("source") == "model_assumption":
            # Model assumptions are never presented as values to enter.
            text_value = None
        steps.append(
            PlanStep(
                title=str(raw.get("title", "Step")),
                instruction=str(raw.get("instruction", "")),
                action_type=action_type,
                target_label=str(raw.get("target_label") or ""),
                target_role=str(raw.get("target_role") or ""),
                text_value=text_value if isinstance(text_value, dict) else None,
                expected_result=str(raw.get("expected_result", "")),
                why=str(raw.get("why", "")),
                rules=rules,
                requires_user_input=bool(raw.get("requires_user_input")),
                risk=str(raw.get("risk", "none")),
            )
        )
    if not steps:
        raise PlanningUnavailable("Model returned a plan with no steps")
    return Plan(
        title=util.truncated(str(payload.get("title", "Plan")), 160),
        summary=str(payload.get("summary", "")),
        steps=steps,
        source="model",
        missing_information=[m for m in payload.get("missing_information", []) if isinstance(m, dict)],
        notes=["Plan produced by the configured AI model and validated against the plan schema."],
    )


# ---------------------------------------------------------------------------
# Facade
# ---------------------------------------------------------------------------


class TaskPlanner:
    """Chooses between model planning and local planning, with strict validation."""

    def __init__(self, settings, gateway: ModelGateway | None = None) -> None:
        self.settings = settings
        self.gateway = gateway

    def model_available(self) -> bool:
        if self.gateway is None:
            return False
        ok, _ = self.gateway.available()
        return ok and self.settings.get("ai.provider") != "none"

    def plan(
        self,
        objective: str,
        observation: Observation | None,
        context_values: dict[str, Any] | None = None,
        project_dir: str | None = None,
        app_hint: str = "",
        prefer_model: bool = True,
    ) -> tuple[Plan, list[str]]:
        """Return (plan, warnings). Falls back to local planning on any failure."""
        warnings: list[str] = []
        if prefer_model and self.model_available() and self.gateway is not None:
            messages = build_planner_messages(objective, observation, context_values or {}, app_hint)
            try:
                payload, response = self.gateway.structured(ROLE_REASONING, messages, "plan")
                plan = plan_from_model_payload(payload)
                plan.notes.append(f"Model: {response.model} ({response.latency_ms} ms)")
                return plan, warnings
            except ProviderError as exc:
                warnings.append(f"AI planning was rejected or unavailable: {exc.user_message()}")
            except PlanningUnavailable as exc:
                warnings.append(f"AI plan rejected: {exc}")
        elif prefer_model and self.gateway is not None:
            ok, reason = self.gateway.available()
            if not ok and self.settings.get("ai.provider") != "none":
                warnings.append(f"AI provider unavailable: {reason}")
        plan = local_plan(objective, observation, context_values, project_dir, app_hint)
        return plan, warnings
