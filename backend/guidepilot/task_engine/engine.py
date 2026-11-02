"""Task orchestration: observe → understand → highlight → instruct → verify → adapt.

The engine owns the task state machine. Two rules are enforced centrally:

1. **A model (or planner) instruction is never a completed action.** Progress
   requires evidence from a fresh observation or an explicit user confirmation.
2. **An unverified step is never silently skipped.** Advancing without
   verification is recorded as ``skipped`` and needs an explicit request.

Adaptation is bounded: after repeated failed verifications the task becomes
``BLOCKED`` with an explanation instead of looping the same instruction.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from dataclasses import replace
from typing import Any, Iterable, Sequence

from .. import util
from ..config import Settings
from ..grounding.matcher import GroundingResult, ROLE_ALIASES, best_match, element_inventory
from ..models import (
    ActionType,
    Element,
    Evidence,
    Guidance,
    Highlight,
    Observation,
    Rect,
    Step,
    StepStatus,
    Task,
    TaskState,
    VerificationOutcome,
    VerificationResult,
    VerificationRule,
    can_transition,
    new_id,
    step_id,
)
from ..perception import change
from ..perception.observer import ScreenObserver
from ..providers.base import ChatMessage, ProviderError
from ..providers.gateway import ROLE_INSTRUCTION, ROLE_REASONING, ModelGateway
from ..security.injection import UNTRUSTED_DATA_RULE, scan_untrusted, wrap_untrusted
from ..security.redaction import is_sensitive_label, redact
from ..security.risk import classify_command
from ..storage.db import Database
from ..verification.verifier import Verifier, describe_rules
from .planner import Plan, PlanStep, TaskPlanner
from .values import resolve_value, summarize_context_values

MAX_STEP_ATTEMPTS = 3


class EngineError(RuntimeError):
    """An operation is not valid for the current task state."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message}


_VERIFICATION_METHOD_BY_RULE = {
    "element_appears": "element_appearance",
    "element_disappears": "element_disappearance",
    "text_present": "text_presence",
    "element_value_equals": "field_value",
    "window_title_matches": "window_title",
    "screen_changed": "screen_state_comparison",
    "user_confirmation": "user_confirmation",
    "any_of": "screen_state_comparison",
}


def _verification_method(rules: Sequence[VerificationRule]) -> str:
    if not rules:
        return "none"
    return _VERIFICATION_METHOD_BY_RULE.get(rules[0].kind, "none")


def _step_from_plan(index: int, plan_step: PlanStep) -> Step:
    return Step(
        id=step_id(index),
        index=index,
        title=plan_step.title,
        instruction=plan_step.instruction,
        action_type=plan_step.action_type,
        target_description=plan_step.target_label,
        target_selector=plan_step.to_selector(),
        text_value=plan_step.text_value,
        expected_result=plan_step.expected_result,
        why=plan_step.why,
        rules=plan_step.rules,
        requires_user_input=plan_step.requires_user_input,
        risk=plan_step.risk,
    )


def task_from_dict(data: dict[str, Any], observation: Observation | None = None) -> Task:
    """Rebuild a Task (for resume) from stored JSON."""
    steps: list[Step] = []
    for raw in data.get("steps", []):
        try:
            action = ActionType(raw.get("action_type", "click"))
        except ValueError:
            action = ActionType.MANUAL
        try:
            status = StepStatus(raw.get("status", "pending"))
        except ValueError:
            status = StepStatus.PENDING
        rules = []
        for rule_raw in raw.get("rules", []) or []:
            rule = VerificationRule.from_dict(rule_raw)
            if rule is not None:
                rules.append(rule)
        evidence = []
        for ev in raw.get("evidence", []) or []:
            if isinstance(ev, dict):
                evidence.append(Evidence(str(ev.get("rule", "")), ev.get("passed"), str(ev.get("detail", "")), ev.get("observed")))
        steps.append(
            Step(
                id=str(raw.get("id") or step_id(len(steps))),
                index=int(raw.get("index", len(steps))),
                title=str(raw.get("title", "")),
                instruction=str(raw.get("instruction", "")),
                action_type=action,
                target_description=str(raw.get("target_description", "")),
                target_selector=raw.get("target_selector") or {},
                text_value=raw.get("text_value"),
                expected_result=str(raw.get("expected_result", "")),
                why=str(raw.get("why", "")),
                rules=rules,
                status=status,
                attempts=int(raw.get("attempts", 0)),
                verified_at=raw.get("verified_at"),
                evidence=evidence,
                notes=list(raw.get("notes", []) or []),
                requires_user_input=bool(raw.get("requires_user_input")),
                risk=str(raw.get("risk", "none")),
            )
        )
    try:
        state = TaskState(data.get("state", "CREATED"))
    except ValueError:
        state = TaskState.CREATED
    return Task(
        id=str(data["id"]),
        objective=str(data.get("objective", "")),
        state=state,
        steps=steps,
        current_index=int(data.get("current_index", 0)),
        created_at=int(data.get("created_at", util.now_ms())),
        updated_at=int(data.get("updated_at", util.now_ms())),
        app_context=str(data.get("app_context", "")),
        target_window_id=str(data.get("target_window_id", "")),
        summary=str(data.get("summary", "")),
        facts=list(data.get("facts", []) or []),
        history=list(data.get("history", []) or []),
        observation=observation,
        last_verified_step=int(data.get("last_verified_step", -1)),
        paused_reason=str(data.get("paused_reason", "")),
        workflow_id=data.get("workflow_id"),
        context_values=data.get("context_values", {}) or {},
    )


class TaskEngine:
    def __init__(
        self,
        settings: Settings,
        observer: ScreenObserver,
        planner: TaskPlanner,
        gateway: ModelGateway,
        verifier: Verifier,
        db: Database,
    ) -> None:
        self.settings = settings
        self.observer = observer
        self.planner = planner
        self.gateway = gateway
        # step id -> (rect, png) of a text field before the user types into it
        self._field_baselines: dict[str, tuple[list[int], bytes]] = {}
        self.verifier = verifier
        self.db = db
        self._lock = threading.RLock()
        self._tasks: dict[str, Task] = {}
        self.current_task_id: str | None = None
        self.stopped = False
        self.last_error: dict[str, Any] | None = None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @property
    def current_task(self) -> Task | None:
        if not self.current_task_id:
            return None
        return self._tasks.get(self.current_task_id)

    def _audit(self, kind: str, detail: Any, task_id: str | None = None) -> None:
        try:
            self.db.add_audit(kind, task_id or self.current_task_id, detail)
        except Exception as exc:  # pragma: no cover - logging must never break guidance
            util.get_logger().warning(f"audit write failed: {exc}")

    def _transition(self, task: Task, target: TaskState, reason: str = "") -> None:
        if task.state == target:
            return
        if not can_transition(task.state, target):
            raise EngineError("invalid_transition", f"Cannot move task from {task.state.value} to {target.value}")
        previous = task.state
        task.state = target
        task.updated_at = util.now_ms()
        task.history.append(
            {
                "at": task.updated_at,
                "from": previous.value,
                "to": target.value,
                "reason": util.truncated(reason, 300),
            }
        )

    def _persist(self, task: Task) -> None:
        payload = task.to_dict(include_observation=True)
        if payload.get("observation"):
            payload["observation"]["elements"] = payload["observation"]["elements"][:150]
        self.db.save_task(payload)
        self.db.set_last_task(task.id)
        if task.observation is not None:
            frame = task.observation.frame
            self.db.add_observation_meta(
                task.observation.id,
                task.id,
                task.observation.session_type,
                frame.scope if frame else "none",
                len(task.observation.elements),
                frame.change_ratio if frame else 0.0,
                task.observation.sensitive,
                task.observation.sources,
            )

    def _capture_field_baseline(self, task: Task, step: Step, guidance: Guidance) -> None:
        """Remember how a text field looks before the user types into it.

        Accessibility text is not dependable: some applications publish no text at
        all (Chromium in basic accessibility mode), and others publish a value and
        then read back empty for a field that is visibly filled. In both cases the
        field's own pixels are the only honest evidence available, so a baseline is
        taken for every field that will be verified by value and compared after the
        interaction.
        """
        if guidance.highlight is None or not guidance.highlight.rect:
            return
        if not any(rule.kind == "element_value_equals" for rule in step.rules):
            return
        rect = guidance.highlight.rect
        pixels = self._field_pixels(rect)
        if pixels is None:
            return
        self._field_baselines[step.id] = (rect.to_list(), pixels)

    def _field_pixels(self, rect: Rect) -> bytes | None:
        """One field's pixels, taken from the target window rather than the screen.

        A screen grab of the region contains whatever is drawn on top of it, and
        while a step is active that is GuidePilot's own highlight: baseline and
        check would both be pictures of the overlay and the field would look
        untouched no matter what the user typed. The window's own contents are the
        field's real appearance.
        """
        task = self.current_task
        observation = task.observation if task is not None else None
        frame = observation.frame if observation is not None else None
        if observation is not None and frame is not None:
            png = self.observer.frame_png(observation.id)
            if png:
                scale = frame.scale or 1.0
                box = (
                    int((rect.x - frame.rect.x) * scale),
                    int((rect.y - frame.rect.y) * scale),
                    int((rect.x - frame.rect.x + rect.width) * scale),
                    int((rect.y - frame.rect.y + rect.height) * scale),
                )
                cropped = change.crop_png(png, box)
                if cropped:
                    return cropped
        try:
            capture = self.observer.backend.capture_rect(rect)
        except Exception:  # pragma: no cover - capture is best effort
            return None
        return capture.png if capture is not None and capture.png else None

    def _field_changed_since_baseline(self, step: Step, rect: list[int]) -> bool | None:
        """True when the highlighted region visibly changed since the baseline.

        Two measurements, because a text field can be filled without changing its
        overall appearance: a coarse perceptual ratio catches a recoloured or
        emptied region, while the glyph count catches the small, sharp change that
        thousands of characters of typed text actually make.
        """
        baseline = self._field_baselines.get(step.id)
        if baseline is None:
            return None
        baseline_rect, baseline_png = baseline
        if [int(v) for v in baseline_rect] != [int(v) for v in rect]:
            return None
        pixels = self._field_pixels(Rect(*[int(v) for v in rect]))
        if pixels is None:
            return None
        try:
            change_ratio, _ = change.compare(
                change.signature_from_png(baseline_png), change.signature_from_png(pixels)
            )
        except Exception:  # pragma: no cover - undecodable frames are not evidence
            change_ratio = 0.0
        if change_ratio >= 0.02:
            return True
        before_ink = change.ink_count(baseline_png)
        after_ink = change.ink_count(pixels)
        if os.environ.get("GUIDEPILOT_DEBUG_VERIFY"):
            print(
                f"[verify] field ratio={change_ratio:.4f} ink={before_ink}->{after_ink} "
                f"png_sizes={len(baseline_png)}/{len(pixels)}",
                file=sys.stderr,
                flush=True,
            )
        if before_ink is None or after_ink is None:
            return change_ratio >= 0.02
        # A caret is a corner of a character's worth of ink; typed text is not.
        return (after_ink - before_ink) >= 40

    @staticmethod
    def _failed_on_blank_value(result: VerificationResult) -> bool:
        """True when a value rule failed only because the field read back empty."""
        for evidence in result.evidence:
            if evidence.rule != "element_value_equals" or evidence.passed is not False:
                continue
            if not str(evidence.observed or "").strip():
                return True
        return False

    def _refine_with_pixels(self, step: Step, result: VerificationResult) -> VerificationResult:
        """Make an unreadable text field's outcome more useful, never more optimistic.

        The outcome stays ``INCONCLUSIVE`` either way: pixels cannot prove what a
        field *contains*. They can tell "nothing typed yet" from "something was
        typed but I cannot read it", which is what the user needs to hear.

        A *failed* value check also qualifies when the only thing that failed was an
        empty read on a field whose pixels visibly changed: the field has content,
        so the empty read is a limitation of the application, not a mistake the
        user made, and accusing them would be a false failure.
        """
        baseline = self._field_baselines.get(step.id)
        changed = None if baseline is None else self._field_changed_since_baseline(step, baseline[0])
        if os.environ.get("GUIDEPILOT_DEBUG_VERIFY"):
            print(
                f"[verify] refine step={step.id} outcome={result.outcome.value} "
                f"baseline={'yes' if baseline else 'no'} changed={changed}",
                file=sys.stderr,
                flush=True,
            )
        if baseline is None or changed is None:
            return result
        label = step.target_description or step.title or "the field"
        failed_read = result.outcome == VerificationOutcome.FAILED
        if failed_read and (not changed or not self._failed_on_blank_value(result)):
            # A non-blank wrong value, or a field that looks untouched, is real
            # evidence of a failed step and must keep its verdict.
            return result
        if changed:
            evidence = Evidence("pixels", None, "The field's appearance changed after the interaction")
            summary = (
                f"'{label}' visibly changed, but this application does not publish field contents, "
                "so I cannot read the exact value back. Tell me to confirm if it is correct."
            )
        else:
            evidence = Evidence("pixels", False, "The field looks unchanged since the instruction was shown")
            summary = f"'{label}' looks exactly as it did before you typed, so nothing has been entered yet."
        return VerificationResult(VerificationOutcome.INCONCLUSIVE, list(result.evidence) + [evidence], summary)

    def _observe_task(self, task: Task, force: bool = True) -> Observation:
        """Observe the window this task is anchored to.

        Focus moves around while a task runs — the user clicks the assistant to
        read an instruction, another application raises itself on a second
        monitor — and grounding must not follow it. The pinned window is used
        until it disappears, then the pin moves to whatever is active.
        """
        observation = self.observer.observe(
            force=force,
            app_hint=task.app_context,
            window_id=task.target_window_id or None,
        )
        active = observation.active_window
        if active is None:
            return observation
        if not task.target_window_id or active.id != task.target_window_id:
            if task.target_window_id:
                # The pinned window is gone; re-anchor instead of failing.
                task.history.append(
                    {
                        "at": util.now_ms(),
                        "kind": "observation",
                        "detail": f"Target window {task.target_window_id} is gone; now observing {active.id} '{active.title}'",
                    }
                )
            task.target_window_id = active.id
            task.app_context = task.app_context or active.app_class
        return observation

    # ------------------------------------------------------------------
    # Creation and planning
    # ------------------------------------------------------------------
    def create_task(
        self,
        objective: str,
        context_values: dict[str, Any] | None = None,
        app_hint: str = "",
        project_dir: str | None = None,
        prefer_model: bool = True,
        plan_override: Plan | None = None,
        extra_warnings: Sequence[str] | None = None,
        workflow_id: str | None = None,
    ) -> dict[str, Any]:
        objective = (objective or "").strip()
        if not objective:
            raise EngineError("invalid_objective", "Describe what you would like help doing.")
        if self.stopped:
            raise EngineError("emergency_stopped", "Emergency stop is active. Re-enable screen sharing to start a new task.")
        observation = self.observer.observe(force=True, app_hint=app_hint)
        task = Task(
            id=new_id("task"),
            objective=objective,
            state=TaskState.CREATED,
            steps=[],
            app_context=app_hint or (observation.active_window.app_class if observation.active_window else ""),
            target_window_id=observation.active_window.id if observation.active_window else "",
            context_values=context_values or {},
            workflow_id=workflow_id,
        )
        with self._lock:
            self._tasks[task.id] = task
            self.current_task_id = task.id
        self._transition(task, TaskState.OBSERVING, "Initial screen observation")
        task.observation = observation
        task.history.append({"at": util.now_ms(), "kind": "observation", "detail": self._observation_summary(observation)})
        warnings = self._scan_screen_for_injection(observation, task)
        self._transition(task, TaskState.PLANNING, "Building a plan from the observed screen")
        if plan_override is not None:
            plan = plan_override
            plan_warnings = list(extra_warnings or [])
        else:
            plan, plan_warnings = self.planner.plan(
                objective=objective,
                observation=observation,
                context_values=context_values or {},
                project_dir=project_dir,
                app_hint=app_hint or task.app_context,
                prefer_model=prefer_model,
            )
            plan_warnings = list(plan_warnings) + list(extra_warnings or [])
        warnings.extend(plan_warnings)
        if not plan.steps:
            self._transition(task, TaskState.BLOCKED, "The planner produced no steps")
            task.summary = "I could not build a plan for this objective."
            self._persist(task)
            return self.state_payload(task, warnings=warnings)
        task.steps = [_step_from_plan(index, item) for index, item in enumerate(plan.steps)]
        task.summary = plan.summary
        task.facts = [
            {"kind": "plan_source", "value": plan.source, "verified": plan.source == "local"},
            {
                "kind": "observed_elements",
                "value": len(observation.elements),
                "verified": bool(observation.sources.get("accessibility", {}).get("used")),
            },
        ]
        for missing in plan.missing_information:
            warnings.append(f"Information needed: {missing.get('question') or missing.get('field')}")
        if plan.source == "model":
            task.model_calls += 1
        self._transition(task, TaskState.READY_FOR_USER, "Plan ready")
        task.steps[0].status = StepStatus.ACTIVE
        self._ground_and_instruct(task, warnings=warnings)
        self._persist(task)
        return self.state_payload(task, warnings=warnings)

    def _observation_summary(self, observation: Observation) -> dict[str, Any]:
        return {
            "window": observation.active_window.title if observation.active_window else None,
            "app": observation.active_window.app_class if observation.active_window else None,
            "elements": len(observation.elements),
            "sensitive": observation.sensitive,
            "sources": {k: v for k, v in observation.sources.items() if k in {"accessibility", "ocr", "capture"}},
        }

    def _scan_screen_for_injection(self, observation: Observation, task: Task) -> list[str]:
        texts = [e.name for e in observation.elements if e.name] + [e.value or "" for e in observation.elements]
        if observation.active_window:
            texts.append(observation.active_window.title)
        findings = scan_untrusted(texts)
        warnings: list[str] = []
        if findings:
            warnings.append(
                "Screen content contains text that looks like instructions aimed at an AI assistant. "
                "I ignored it: only your own messages can change what I do."
            )
            task.history.append(
                {"at": util.now_ms(), "kind": "injection_warning", "detail": [f.to_dict() for f in findings[:3]]}
            )
            task.facts.append(
                {"kind": "prompt_injection_attempt", "value": len(findings), "verified": True, "detail": "ignored"}
            )
            self._audit("prompt_injection_ignored", {"findings": [f.to_dict() for f in findings[:3]]}, task.id)
        return warnings

    # ------------------------------------------------------------------
    # Grounding and guidance
    # ------------------------------------------------------------------
    def _elements_for_matching(self, observation: Observation) -> list[Element]:
        elements = list(observation.elements)
        window = observation.active_window
        if window:
            scoped = [e for e in elements if window.rect.inflate(6).intersects(e.rect)]
            if scoped:
                return scoped
        return elements

    def _ground(self, task: Task, step: Step, observation: Observation) -> tuple[Guidance, list[str]]:
        warnings: list[str] = []
        selector = step.target_selector or {}
        label = str(selector.get("label") or step.target_description or "")
        role = str(selector.get("role") or "")
        aliases = selector.get("role_aliases") or []
        min_confidence = float(self.settings.get("perception.min_grounding_confidence", 0.6))
        candidates = self._elements_for_matching(observation)
        result: GroundingResult = best_match(
            candidates,
            description=label,
            role_hint=role,
            aliases=aliases,
            monitors=observation.monitors,
            min_confidence=min_confidence,
        )
        highlight: Highlight | None = None
        confidence = result.confidence
        source = result.source
        alternatives = [alt for alt in result.alternatives]

        if result.ok and result.element is not None:
            highlight = Highlight(
                element_id=result.element.id,
                rect=result.element.rect,
                label=result.element.name or label,
                instruction=step.instruction,
                confidence=result.confidence,
                source=source,
                pulsing=bool(self.settings.get("ui.pulse_highlight", True)),
                number=step.index + 1,
            )
            if result.ambiguous:
                warnings.append(result.message)
        else:
            vision = self._vision_ground(task, step, observation, candidates)
            if vision is not None:
                element, vision_confidence, note = vision
                rect = element.rect if element is not None else step.target_selector.get("rect")
                if rect is not None:
                    highlight = Highlight(
                        element_id=element.id if element else None,
                        rect=Rect.from_any(rect) or Rect(0, 0, 0, 0),
                        label=(element.name if element else label) or label,
                        instruction=step.instruction,
                        confidence=vision_confidence,
                        source="vision" if element is None else "vision+a11y",
                        pulsing=True,
                        number=step.index + 1,
                    )
                    confidence = vision_confidence
                    source = highlight.source
                    warnings.append(note)
            if highlight is None:
                warnings.append(result.message or "The target control could not be located on screen.")
                confidence = result.confidence

        if step.risk in {"modify", "destructive"}:
            warnings.append(
                f"This step changes your system ({step.risk}). Review it before acting; GuidePilot will not run it for you."
            )
        requires_confirmation = bool(step.risk in {"modify", "destructive"} or not result.ok)

        # A value supplied after planning (chat answer or "use this value" in the
        # UI) must reach the instruction card, otherwise the user is asked for
        # information the assistant already has.
        if step.text_value is None and step.action_type == ActionType.TYPE:
            late_value = resolve_value(label, task.context_values)
            if late_value is not None:
                step.text_value = late_value
                if step.requires_user_input:
                    step.requires_user_input = False
                    step.instruction = f"Click the '{label}' field and enter the value shown below."

        text_to_enter: dict[str, Any] | None = None
        if step.text_value:
            sensitive = bool(step.text_value.get("sensitive")) or is_sensitive_label(label)
            value = "" if sensitive else str(step.text_value.get("value", ""))
            text_to_enter = {
                "value": value,
                "field_label": label,
                "source": step.text_value.get("source", "unknown"),
                "sensitive": sensitive,
                "copyable": bool(value) and not sensitive,
                "explanation": step.text_value.get("explanation", ""),
            }
            if sensitive:
                warnings.append(
                    "This is a sensitive field. GuidePilot will not display, copy, store or transmit the value: enter it yourself."
                )

        guidance = Guidance(
            step_id=step.id,
            instruction=step.instruction,
            action_type=step.action_type.value,
            highlight=highlight,
            text_to_enter=text_to_enter,
            expected_result=step.expected_result,
            verification_method=_verification_method(step.rules),
            requires_confirmation=requires_confirmation,
            confidence=confidence,
            why=step.why,
            alternatives=alternatives,
            warnings=warnings,
            notes=[
                f"verification: {', '.join(describe_rules(step.rules)) or 'none'}",
                f"target source: {source}",
            ],
        )
        if highlight is None and step.action_type not in {ActionType.COMMAND, ActionType.MANUAL, ActionType.CONFIRM}:
            warnings.append("I could not place a highlight, so follow the written instruction instead.")
        return guidance, warnings

    def _vision_ground(
        self,
        task: Task,
        step: Step,
        observation: Observation,
        candidates: Sequence[Element],
    ) -> tuple[Element | None, float, str] | None:
        if not self.settings.get("ai.vision_grounding", True):
            return None
        ok, _ = self.gateway.available()
        if not ok:
            return None
        caps = self.gateway.capabilities("vision")
        if not caps.vision:
            return None
        png = self.observer.frame_png(observation.id)
        if not png:
            return None
        if self.settings.get("privacy.local_only") and self.settings.get("ai.provider") == "openai_compat":
            return None
        target = step.target_description or step.target_selector.get("label") or step.title
        inventory = element_inventory(candidates, limit=50)
        prompt = [
            ChatMessage(
                "system",
                "You locate a single UI control in a screenshot. Reply with JSON only: "
                '{"target_found":bool,"confidence":0..1,"label":str,"element_role":str,'
                '"bounding_box":[x0,y0,x1,y1],"coordinate_space":"normalized_image","reasoning":str}. '
                "Use normalized coordinates between 0 and 1 relative to the image. "
                "If you cannot see the control, set target_found=false and confidence<=0.3. Never guess.\n\n"
                + UNTRUSTED_DATA_RULE,
            ),
            ChatMessage(
                "user",
                f"Find this control: {target}\nRole hint: {step.target_selector.get('role') or 'unknown'}\n"
                f"Accessibility labels already on screen (may be incomplete): {inventory}",
                images=[png],
            ),
        ]
        try:
            payload, _response = self.gateway.structured("vision", prompt, "grounding")
            task.model_calls += 1
            self.db.log_model_call("vision", "gateway", self.gateway.model_for("vision"), True)
        except ProviderError as exc:
            self.db.log_model_call("vision", "gateway", self.gateway.model_for("vision"), False, error=str(exc))
            return None
        if not payload.get("target_found"):
            return None
        confidence = float(payload.get("confidence") or 0)
        if confidence < float(self.settings.get("perception.min_grounding_confidence", 0.6)):
            return None
        box = payload.get("bounding_box")
        space = payload.get("coordinate_space") or "normalized_image"
        if not isinstance(box, list) or len(box) != 4:
            return None
        from ..grounding.geometry import CaptureGeometry, map_model_box, validate_rect_on_screens

        frame = observation.frame
        if frame is None:
            return None
        geometry = CaptureGeometry(rect=frame.rect, width=frame.width, height=frame.height, scale=frame.scale)
        try:
            rect = map_model_box(box, space, geometry)
        except Exception:
            return None
        ok_geom, _reason = validate_rect_on_screens(rect, observation.monitors, min_visible_ratio=0.9)
        if not ok_geom:
            return None
        # Cross-check against accessibility elements: exact bounds win.
        from ..grounding.matcher import merge_vision_with_accessibility

        element, overlap = merge_vision_with_accessibility(rect, candidates)
        if element is not None:
            return element, min(0.97, confidence + 0.1), "Vision model location confirmed by accessibility bounds."
        return None, 0.0, ""

    def _ground_and_instruct(self, task: Task, warnings: list[str] | None = None) -> None:
        step = task.current_step
        if step is None:
            return
        observation = task.observation
        if observation is None:
            observation = self.observer.observe()
            task.observation = observation
        guidance, ground_warnings = self._ground(task, step, observation)
        all_warnings = list(warnings or []) + ground_warnings
        if guidance.highlight is None and step.action_type not in {
            ActionType.COMMAND,
            ActionType.MANUAL,
            ActionType.CONFIRM,
        }:
            if task.state in {TaskState.READY_FOR_USER, TaskState.RECOVERING, TaskState.WAITING_FOR_ACTION}:
                self._transition(task, TaskState.BLOCKED, "Target control could not be located")
                guidance.warnings.append(
                    "I could not locate the target with enough confidence. Use 'Look again' after the screen settles, "
                    "or tell me the exact control name."
                )
        task.guidance = guidance
        self._capture_field_baseline(task, step, guidance)
        if all_warnings:
            merged: list[str] = list(guidance.warnings)
            for warning in all_warnings:
                if warning not in merged:
                    merged.append(warning)
            guidance.warnings = merged
        step.status = StepStatus.ACTIVE if step.status == StepStatus.PENDING else step.status

    def ground_current(self, force_observe: bool = True) -> dict[str, Any]:
        task = self._require_task()
        observation = task.observation
        if force_observe or observation is None:
            observation = self._observe_task(task, force=True)
            task.observation = observation
        if task.state in {TaskState.BLOCKED, TaskState.PAUSED}:
            self._transition(task, TaskState.READY_FOR_USER, "Re-grounding after a fresh observation")
        self._ground_and_instruct(task)
        self._persist(task)
        return self.state_payload(task)

    # ------------------------------------------------------------------
    # Verification and navigation
    # ------------------------------------------------------------------
    def verify_current(self, user_confirmed: bool = False) -> dict[str, Any]:
        task = self._require_task()
        step = task.current_step
        if step is None:
            raise EngineError("no_step", "There is no active step to verify")
        if task.state == TaskState.COMPLETED:
            raise EngineError("task_completed", "This task is already complete")
        previous = task.observation
        if task.state != TaskState.VERIFYING:
            self._transition(task, TaskState.VERIFYING, "Checking the result on screen")
        observation = self._observe_task(task, force=True)
        task.observation = observation
        result = self.verifier.evaluate(step.rules, previous, observation, user_confirmed=user_confirmed)
        # Chromium-based applications publish accessibility state *after* the
        # input has landed: a field's text, a combo box's selected option and a
        # checkbox's state can take a second or more to appear, and a popup that
        # just closed can leave a stale tree behind. Re-read a few times over a
        # couple of seconds so a slow toolkit does not look like a failed step.
        # Only a VERIFIED re-read replaces the first result: a genuine failure
        # still fails, an inconclusive reading is never upgraded to success, and
        # the poll stops as soon as the evidence arrives.
        for delay in (0.4, 0.9, 1.4) if result.outcome != VerificationOutcome.VERIFIED else ():
            time.sleep(delay)
            observation = self._observe_task(task, force=True)
            task.observation = observation
            retry = self.verifier.evaluate(step.rules, previous, observation, user_confirmed=user_confirmed)
            if retry.outcome == VerificationOutcome.VERIFIED:
                result = retry
                break
            if result.outcome == VerificationOutcome.INCONCLUSIVE and retry.outcome == VerificationOutcome.FAILED:
                # A failed read after an unreadable one is stronger evidence, but
                # it must not replace a genuine pass or a later pass.
                result = retry
        if result.outcome in (VerificationOutcome.INCONCLUSIVE, VerificationOutcome.FAILED):
            result = self._refine_with_pixels(step, result)
        self._apply_verification(task, step, result)
        self._persist(task)
        return self.state_payload(task)

    def _apply_verification(self, task: Task, step: Step, result: VerificationResult) -> None:
        """Shared outcome handling for explicit and automatic verification."""
        task.verification = result
        step.evidence = result.evidence
        if result.outcome == VerificationOutcome.VERIFIED:
            step.status = StepStatus.VERIFIED
            step.verified_at = util.now_ms()
            task.last_verified_step = step.index
            task.history.append(
                {"at": util.now_ms(), "kind": "verification", "step": step.id, "outcome": "VERIFIED", "detail": result.summary}
            )
            self._advance_after_success(task)
        elif result.outcome == VerificationOutcome.FAILED:
            step.status = StepStatus.FAILED
            step.attempts += 1
            task.history.append(
                {"at": util.now_ms(), "kind": "verification", "step": step.id, "outcome": "FAILED", "detail": result.summary}
            )
            self._recover(task, step, result)
        else:
            step.status = StepStatus.INCONCLUSIVE
            task.history.append(
                {"at": util.now_ms(), "kind": "verification", "step": step.id, "outcome": "INCONCLUSIVE", "detail": result.summary}
            )
            self._transition(task, TaskState.WAITING_FOR_ACTION, "Verification was inconclusive")
            step.notes.append(result.summary)

    def watch_tick(self, auto_verify: bool = True) -> dict[str, Any]:
        """One adaptive observation cycle while waiting for the user.

        Captures the screen, measures change and — only when something visibly
        changed — re-evaluates the current step's verification rules. A step can
        therefore be confirmed the moment the user's action actually lands,
        without advancing on an unchanged screen.
        """
        task = self._require_task()
        if task.state in {TaskState.COMPLETED, TaskState.CANCELLED, TaskState.PAUSED, TaskState.BLOCKED}:
            return self.state_payload(task)
        step = task.current_step
        previous = task.observation
        observation = self._observe_task(task, force=True)
        task.observation = observation
        warnings: list[str] = []
        threshold = float(self.settings.get("observation.min_change_ratio", 0.004))
        change = observation.frame.change_ratio if observation.frame else 1.0
        if step is not None and previous is not None and step.rules and change >= threshold:
            result = self.verifier.evaluate(step.rules, previous, observation, user_confirmed=False)
            if result.outcome == VerificationOutcome.VERIFIED:
                self._apply_verification(task, step, result)
                self._persist(task)
                return self.state_payload(task, warnings=["Verified automatically after a screen change."])
            if result.outcome == VerificationOutcome.FAILED:
                warnings.append("The screen changed but the expected result is not there yet.")
            task.verification = result
            step.evidence = result.evidence
        if task.state in {TaskState.BLOCKED, TaskState.RECOVERING}:
            self._transition(task, TaskState.READY_FOR_USER, "Re-grounded after a screen change")
        self._ground_and_instruct(task)
        self._persist(task)
        return self.state_payload(
            task,
            warnings=warnings + (["Screen changed; highlight refreshed."] if change >= threshold else []),
        )

    def _advance_after_success(self, task: Task) -> None:
        step = task.current_step
        if step is None:
            return
        next_index = task.current_index + 1
        if next_index >= len(task.steps):
            task.current_index = len(task.steps)
            self._transition(task, TaskState.COMPLETED, "All steps verified")
            task.summary = "Task completed and verified."
            task.guidance = None
            self._audit("task_completed", {"objective": task.objective, "steps": len(task.steps)}, task.id)
            return
        task.current_index = next_index
        task.steps[next_index].status = StepStatus.ACTIVE
        self._transition(task, TaskState.READY_FOR_USER, "Next step ready")
        # Ground the next step against a fresh observation.
        task.observation = self._observe_task(task, force=False)
        self._ground_and_instruct(task)

    def _recover(self, task: Task, step: Step, result: VerificationResult) -> None:
        self._transition(task, TaskState.RECOVERING, "Adapting after a failed verification")
        task.error_budget += 1
        task.observation = self._observe_task(task, force=True)
        if step.attempts >= MAX_STEP_ATTEMPTS:
            self._transition(task, TaskState.BLOCKED, f"{step.attempts} failed verification attempts")
            task.guidance = Guidance(
                step_id=step.id,
                instruction=(
                    f"I still can't verify '{step.title}'. Something on screen is different from what this step expects. "
                    "Tell me what you see, or cancel this task and we can start again from the current screen."
                ),
                action_type=step.action_type.value,
                highlight=None,
                text_to_enter=None,
                expected_result=step.expected_result,
                verification_method=_verification_method(step.rules),
                requires_confirmation=True,
                confidence=0.0,
                why=step.why,
                warnings=[result.summary],
                notes=["Stopped repeating the same instruction after repeated failures."],
            )
            return
        self._transition(task, TaskState.READY_FOR_USER, "Re-grounded after failure")
        step.status = StepStatus.ACTIVE
        self._ground_and_instruct(task)
        if task.guidance:
            task.guidance.warnings.append(
                f"That attempt did not produce the expected result ({result.summary}). I re-read the screen; the highlight below is current."
            )

    def confirm_current(self) -> dict[str, Any]:
        """User explicitly says the step is done (recorded as user_confirmed)."""
        task = self._require_task()
        step = task.current_step
        if step is None:
            raise EngineError("no_step", "There is no active step")
        step.status = StepStatus.USER_CONFIRMED
        step.verified_at = util.now_ms()
        task.last_verified_step = step.index
        # A manual confirmation is recorded as such; it must not reuse the
        # previous step's verification result in the API payload.
        task.verification = VerificationResult(
            VerificationOutcome.INCONCLUSIVE,
            [Evidence("user_confirmation", True, "You confirmed this step manually; it was not machine-verified")],
            f"Step '{step.title}' recorded as user-confirmed (not verified on screen).",
        )
        task.history.append(
            {"at": util.now_ms(), "kind": "user_confirmation", "step": step.id, "detail": "User confirmed the step manually"}
        )
        self._audit("step_user_confirmed", {"step": step.id, "title": step.title}, task.id)
        task.observation = self._observe_task(task, force=False)
        if task.current_index + 1 >= len(task.steps):
            task.current_index = len(task.steps)
            self._transition(task, TaskState.COMPLETED, "All steps completed (last one user-confirmed)")
            task.guidance = None
        else:
            task.current_index += 1
            task.steps[task.current_index].status = StepStatus.ACTIVE
            self._transition(task, TaskState.READY_FOR_USER, "Next step ready")
            self._ground_and_instruct(task)
        self._persist(task)
        return self.state_payload(task)

    def skip_current(self, reason: str = "user skipped") -> dict[str, Any]:
        task = self._require_task()
        step = task.current_step
        if step is None:
            raise EngineError("no_step", "There is no active step")
        step.status = StepStatus.SKIPPED
        step.notes.append(util.truncated(reason, 200))
        task.verification = VerificationResult(
            VerificationOutcome.INCONCLUSIVE,
            [Evidence("skipped", None, util.truncated(reason, 200))],
            f"Step '{step.title}' was skipped without verification.",
        )
        task.history.append(
            {"at": util.now_ms(), "kind": "step_skipped", "step": step.id, "detail": util.truncated(reason, 200)}
        )
        self._audit("step_skipped", {"step": step.id, "reason": reason}, task.id)
        if task.current_index + 1 >= len(task.steps):
            task.current_index = len(task.steps)
            self._transition(task, TaskState.COMPLETED, "Last step skipped by user")
            task.guidance = None
        else:
            task.current_index += 1
            task.steps[task.current_index].status = StepStatus.ACTIVE
            if task.state in {TaskState.BLOCKED, TaskState.WAITING_FOR_ACTION, TaskState.VERIFYING}:
                self._transition(task, TaskState.READY_FOR_USER, "Skipped to the next step")
            elif task.state != TaskState.READY_FOR_USER:
                self._transition(task, TaskState.READY_FOR_USER, "Skipped to the next step")
            task.observation = self._observe_task(task, force=False)
            self._ground_and_instruct(task)
        self._persist(task)
        return self.state_payload(task)

    def next_step(self) -> dict[str, Any]:
        task = self._require_task()
        step = task.current_step
        if step is None:
            raise EngineError("no_step", "There is no active step")
        if step.status not in {StepStatus.VERIFIED, StepStatus.USER_CONFIRMED, StepStatus.SKIPPED}:
            raise EngineError(
                "unverified_step",
                "This step has not been verified. Verify it, confirm it yourself, or explicitly skip it — I won't mark it complete without evidence.",
            )
        return self.state_payload(task)

    def previous_step(self) -> dict[str, Any]:
        task = self._require_task()
        if task.current_index == 0:
            raise EngineError("no_previous_step", "This is the first step")
        task.current_index = max(0, task.current_index - 1)
        previous = task.steps[task.current_index]
        previous.status = StepStatus.ACTIVE
        if task.state in {TaskState.COMPLETED, TaskState.BLOCKED, TaskState.WAITING_FOR_ACTION}:
            if task.state == TaskState.COMPLETED:
                task.state = TaskState.READY_FOR_USER
            else:
                self._transition(task, TaskState.READY_FOR_USER, "Went back to a previous step")
        task.history.append({"at": util.now_ms(), "kind": "previous_step", "step": previous.id})
        task.observation = self._observe_task(task, force=True)
        self._ground_and_instruct(task)
        self._persist(task)
        return self.state_payload(task)

    # ------------------------------------------------------------------
    # Pause / stop / resume
    # ------------------------------------------------------------------
    def pause(self, reason: str = "Paused by user") -> dict[str, Any]:
        task = self.current_task
        if task is None:
            raise EngineError("no_task", "There is no active task")
        if task.state != TaskState.PAUSED:
            self._transition(task, TaskState.PAUSED, reason)
        task.paused_reason = reason
        self._audit("task_paused", {"reason": reason}, task.id)
        self._persist(task)
        return self.state_payload(task)

    def resume(self) -> dict[str, Any]:
        task = self._require_task()
        if task.state == TaskState.PAUSED:
            task.observation = self._observe_task(task, force=True)
            self._transition(task, TaskState.READY_FOR_USER, "Resumed with a fresh observation")
            self._ground_and_instruct(task, warnings=["Resumed. I re-read the screen before continuing, so the state has not been assumed."])
        elif task.state in {TaskState.BLOCKED, TaskState.WAITING_FOR_ACTION}:
            task.observation = self._observe_task(task, force=True)
            self._transition(task, TaskState.READY_FOR_USER, "Retried after user request")
            self._ground_and_instruct(task)
        self._persist(task)
        return self.state_payload(task)

    def cancel(self, reason: str = "Cancelled by user") -> dict[str, Any]:
        task = self._require_task()
        if task.state not in {TaskState.COMPLETED, TaskState.CANCELLED}:
            self._transition(task, TaskState.CANCELLED, reason)
        task.guidance = None
        self._audit("task_cancelled", {"reason": reason}, task.id)
        self._persist(task)
        return self.state_payload(task)

    def emergency_stop(self) -> dict[str, Any]:
        """Global STOP: halt capture, pause the task, keep everything recoverable."""
        self.stopped = True
        self.settings.update({"privacy": {"stop_capture": True, "paused": True, "share_enabled": False}})
        task = self.current_task
        stopped_task = None
        if task and task.state not in {TaskState.COMPLETED, TaskState.CANCELLED}:
            try:
                self._transition(task, TaskState.PAUSED, "Emergency stop")
            except EngineError:
                task.state = TaskState.PAUSED
            task.paused_reason = "Emergency stop"
            task.guidance = None
            self._persist(task)
            stopped_task = task.id
        self.observer.clear()
        self._audit("emergency_stop", {"task": stopped_task})
        return {
            "stopped": True,
            "task_id": stopped_task,
            "message": "All screen capture and guidance stopped. Nothing is being read from your screen.",
            "settings": self.settings.public_view,
        }

    def clear_stop(self) -> dict[str, Any]:
        self.stopped = False
        self.settings.update({"privacy": {"stop_capture": False, "paused": False, "share_enabled": True}})
        self._audit("screen_sharing_enabled", {"source": "user"})
        return {"stopped": False, "settings": self.settings.public_view}

    # ------------------------------------------------------------------
    # Resume from disk
    # ------------------------------------------------------------------
    def resume_prompt(self) -> dict[str, Any] | None:
        data = self.db.find_resumable_task()
        if not data:
            return None
        return {
            "task_id": data.get("id"),
            "objective": data.get("objective"),
            "state": data.get("state"),
            "current_step": data.get("current_step", {}).get("title") if isinstance(data.get("current_step"), dict) else None,
            "progress": data.get("progress"),
            "updated_at": data.get("updated_at"),
            "question": "Resume your previous task?",
        }

    def resume_task(self, task_id: str, context_values: dict[str, Any] | None = None) -> dict[str, Any]:
        data = self.db.load_task(task_id)
        if not data:
            raise EngineError("not_found", "That task is no longer in local storage")
        if self.stopped:
            raise EngineError("emergency_stopped", "Emergency stop is active. Re-enable screen sharing first.")
        observation = self.observer.observe(force=True)
        task = task_from_dict(data, observation)
        if context_values:
            task.context_values.update(context_values)
        if task.state in {TaskState.COMPLETED, TaskState.CANCELLED}:
            task.state = TaskState.READY_FOR_USER
            task.history.append({"at": util.now_ms(), "kind": "resumed_completed", "detail": "Re-opened a finished task"})
        if task.current_index >= len(task.steps):
            task.current_index = max(0, len(task.steps) - 1)
        task.state = TaskState.READY_FOR_USER
        with self._lock:
            self._tasks[task.id] = task
            self.current_task_id = task.id
        warnings = [
            "Resumed from local storage. The screen was re-read before continuing, so earlier state was not assumed."
        ]
        warnings.extend(self._scan_screen_for_injection(observation, task))
        self._ground_and_instruct(task, warnings=warnings)
        self._persist(task)
        return self.state_payload(task, warnings=warnings)

    # ------------------------------------------------------------------
    # Conversation
    # ------------------------------------------------------------------
    LOCAL_CHAT_HINTS = [
        "what do i click",
        "where do i click",
        "what's next",
        "whats next",
        "next step",
        "did i do",
        "did it work",
        "why isn't this working",
        "why isnt this working",
        "not working",
        "show me again",
        "repeat",
        "explain",
        "why",
        "where",
        "what does",
        "help",
    ]

    def chat(self, message: str) -> dict[str, Any]:
        message = (message or "").strip()
        if not message:
            raise EngineError("empty_message", "Type a question first")
        task = self.current_task
        observation = task.observation if task else self.observer.latest() or self.observer.observe()
        if task:
            task.observation = observation
        reply_text, source, warnings = self._chat_model(message, task, observation)
        if reply_text is None:
            reply_text = self._chat_local(message, task, observation)
            source = "local"
        if task:
            task.history.append({"at": util.now_ms(), "kind": "chat", "role": "user", "detail": util.truncated(message, 400)})
            task.history.append({"at": util.now_ms(), "kind": "chat", "role": "assistant", "detail": util.truncated(reply_text, 600)})
            self._persist(task)
        return {
            "reply": reply_text,
            "source": source,
            "warnings": warnings,
            "task": self.state_payload(task) if task else None,
            "screen": {
                "window": observation.active_window.title if observation.active_window else None,
                "elements": len(observation.elements),
                "sensitive": observation.sensitive,
            },
        }

    def _chat_model(
        self, message: str, task: Task | None, observation: Observation
    ) -> tuple[str | None, str, list[str]]:
        ok, reason = self.gateway.available()
        if not ok:
            return None, "local", [f"Answered locally ({reason})"] if reason else []
        if self.settings.get("privacy.local_only") and self.settings.get("ai.provider") == "openai_compat":
            return None, "local", ["Local-only mode: answered without contacting a remote model."]
        context_lines = [
            f"OBJECTIVE: {task.objective if task else '(no active task)'}",
            f"TASK_STATE: {task.state.value if task else 'NONE'}",
            f"CURRENT_STEP: {task.current_step.title if task and task.current_step else 'none'}",
            f"CURRENT_INSTRUCTION: {task.guidance.instruction if task and task.guidance else 'none'}",
            f"EXPECTED_RESULT: {task.current_step.expected_result if task and task.current_step else 'none'}",
        ]
        if task and task.verification:
            context_lines.append(f"LAST_VERIFICATION: {task.verification.outcome.value} - {task.verification.summary}")
        systems = (
            "You are GuidePilot, a calm, precise desktop instructor. Answer the user's question using the verified "
            "screen context. Be concise (max 120 words unless asked to explain). If you don't know something, say so and "
            "propose how to find out. Never invent values, credentials or screen contents. Never claim an action "
            "succeeded unless verification says VERIFIED.\n\n" + UNTRUSTED_DATA_RULE
        )
        screen_block = wrap_untrusted(
            "accessibility_elements_and_window",
            "\n".join(
                f"{e.role} | {e.name!r} | value={redact(e.value or '')[:60]!r} | rect={e.rect.to_list()}"
                for e in observation.elements[:80]
            ),
            limit=9000,
        )
        user_block = "\n".join(context_lines) + "\n\n" + screen_block + f"\n\nUSER_QUESTION: {message}"
        try:
            response = self.gateway.complete(
                ROLE_INSTRUCTION,
                [ChatMessage("system", systems), ChatMessage("user", user_block)],
            )
            if task:
                task.model_calls += 1
            self.db.log_model_call(ROLE_INSTRUCTION, "gateway", self.gateway.model_for(ROLE_INSTRUCTION), True)
            return response.text.strip(), "model", []
        except ProviderError as exc:
            self.db.log_model_call(ROLE_INSTRUCTION, "gateway", self.gateway.model_for(ROLE_INSTRUCTION), False, error=str(exc))
            return None, "local", [exc.user_message()]

    def _chat_local(self, message: str, task: Task | None, observation: Observation) -> str:
        lowered = message.casefold().strip()
        guidance = task.guidance if task else None
        step = task.current_step if task else None
        if any(word in lowered for word in ("did i", "did it work", "correct", "verify", "check")):
            if task is None or step is None:
                return "There is no active step to verify. Start a task first and I can check the result for you."
            result = self.verifier.evaluate(step.rules, task.observation, observation, user_confirmed=False)
            mapping = {
                VerificationOutcome.VERIFIED: "Yes — verified on screen. ",
                VerificationOutcome.FAILED: "Not yet. ",
                VerificationOutcome.INCONCLUSIVE: "I can't confirm it either way. ",
            }
            return mapping[result.outcome] + result.summary
        if any(word in lowered for word in ("what do i click", "where do i click", "where is", "where do i", "what should i")):
            if guidance and guidance.highlight:
                return (
                    f"Click the highlighted '{guidance.highlight.label}' at "
                    f"{guidance.highlight.rect.to_list()}. {guidance.instruction}"
                )
            if guidance:
                return guidance.instruction
            return "I don't have an active instruction. Tell me what you're trying to do and I'll look at the screen."
        if any(word in lowered for word in ("show me again", "repeat", "say that again")):
            if guidance:
                return f"{guidance.instruction} (Expected: {guidance.expected_result})"
            return "There is no instruction to repeat right now."
        if any(word in lowered for word in ("what's next", "whats next", "next step", "continue")):
            if task is None:
                return "No task is active. Describe your objective and I'll build a plan from what I can see."
            if task.current_step is None:
                return "You're at the end of the plan. Verify the last step or start a new task."
            return f"Next: {task.current_step.instruction}"
        if any(word in lowered for word in ("why", "explain", "what does")):
            if step is None:
                return "Ask me about a specific step and I'll explain the reasoning behind it."
            why = f"{step.why} " if step.why else ""
            rules_text = ", ".join(describe_rules(step.rules)) or "no verification rule"
            return (
                f"{why}Expected result: {step.expected_result} I will verify it by checking: {rules_text}. "
                "If I can't confirm it, the step stays open rather than being marked complete."
            )
        if "not working" in lowered or "isn't working" in lowered or "stuck" in lowered:
            notes = []
            if observation.sensitive:
                notes.append("The active window is privacy-excluded, so I can't see it.")
            if not observation.elements:
                notes.append("I can't read any labelled controls in the current window.")
            notes.append(self._observation_facts(observation))
            return "Let's diagnose it. " + " ".join(notes) + " Tell me exactly what happened, or press 'Look again'."
        if guidance:
            return (
                f"{guidance.instruction} Expected result: {guidance.expected_result}. "
                f"- {self._observation_facts(observation)}"
            )
        return (
            f"I'm watching '{observation.active_window.title if observation.active_window else 'your screen'}' "
            f"and I can see {len(observation.elements)} labelled elements. Tell me your objective and I'll plan the steps."
        )

    def _observation_facts(self, observation: Observation) -> str:
        window = observation.active_window.title if observation.active_window else "no active window"
        a11y = observation.sources.get("accessibility", {})
        parts = [f"Active window: {util.truncated(window, 80)}.", f"{len(observation.elements)} labelled elements visible."]
        if a11y.get("used"):
            parts.append(f"Accessibility data from '{a11y.get('app')}'.")
        else:
            parts.append("Accessibility data is unavailable for this window.")
        if observation.sensitive:
            parts.append("This window is privacy-excluded.")
        return " ".join(parts)

    # ------------------------------------------------------------------
    # Workflows
    # ------------------------------------------------------------------
    def save_as_workflow(self, task_id: str | None = None, name: str = "") -> dict[str, Any]:
        task = self._tasks.get(task_id or self.current_task_id or "")
        if task is None:
            raise EngineError("not_found", "No task to save")
        from .workflows import workflow_from_task

        workflow = workflow_from_task(task, name=name)
        workflow_id = self.db.save_workflow(workflow)
        task.workflow_id = workflow_id
        self._audit("workflow_saved", {"workflow_id": workflow_id, "name": workflow.get("name")}, task.id)
        self._persist(task)
        return {"workflow_id": workflow_id, "workflow": workflow}

    # ------------------------------------------------------------------
    # Payloads
    # ------------------------------------------------------------------
    def _require_task(self) -> Task:
        task = self.current_task
        if task is None:
            raise EngineError("no_task", "Start a task first")
        return task

    def state_payload(self, task: Task | None = None, warnings: Iterable[str] | None = None) -> dict[str, Any]:
        task = task or self.current_task
        if task is None:
            return {"task": None, "warnings": list(warnings or [])}
        payload = {
            "task": task.to_dict(include_observation=False),
            "guidance": task.guidance.to_dict() if task.guidance else None,
            "verification": task.verification.to_dict() if task.verification else None,
            "observation": self._observation_view(task.observation),
            "warnings": list(warnings or []),
            "can": self._capabilities(task),
        }
        return payload

    def _observation_view(self, observation: Observation | None) -> dict[str, Any] | None:
        if observation is None:
            return None
        view = observation.to_dict(include_elements=True)
        view["elements"] = [
            {
                "id": e.id,
                "name": e.name,
                "role": e.role,
                "rect": e.rect.to_list(),
                "value": None if (e.value and is_sensitive_label(e.name)) else e.value,
                "source": e.source.value,
                "confidence": e.confidence,
            }
            for e in observation.elements[:200]
        ]
        return view

    def _capabilities(self, task: Task) -> dict[str, bool]:
        step = task.current_step
        return {
            "can_verify": step is not None and task.state not in {TaskState.COMPLETED, TaskState.CANCELLED},
            "can_confirm": step is not None and task.state not in {TaskState.COMPLETED, TaskState.CANCELLED},
            "can_skip": step is not None and task.state not in {TaskState.COMPLETED, TaskState.CANCELLED},
            "can_next": step is not None and step.status in {StepStatus.VERIFIED, StepStatus.USER_CONFIRMED, StepStatus.SKIPPED},
            "can_previous": task.current_index > 0,
            "can_pause": task.state not in {TaskState.PAUSED, TaskState.COMPLETED, TaskState.CANCELLED},
            "can_resume": task.state in {TaskState.PAUSED, TaskState.BLOCKED, TaskState.WAITING_FOR_ACTION},
            "can_save_workflow": bool(task.steps),
        }

    # ------------------------------------------------------------------
    # Lookups
    # ------------------------------------------------------------------
    def get_task(self, task_id: str) -> dict[str, Any] | None:
        task = self._tasks.get(task_id)
        if task is not None:
            return self.state_payload(task)
        data = self.db.load_task(task_id)
        if data is None:
            return None
        return {"task": data, "guidance": data.get("guidance"), "verification": data.get("verification")}

    def history(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.db.list_tasks(limit=limit)
