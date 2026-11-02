"""Typed data structures shared by perception, grounding, tasks and the API."""

from __future__ import annotations

import math
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Iterable, Sequence

from . import util


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class TaskState(str, Enum):
    CREATED = "CREATED"
    OBSERVING = "OBSERVING"
    PLANNING = "PLANNING"
    READY_FOR_USER = "READY_FOR_USER"
    WAITING_FOR_ACTION = "WAITING_FOR_ACTION"
    VERIFYING = "VERIFYING"
    BLOCKED = "BLOCKED"
    RECOVERING = "RECOVERING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class StepStatus(str, Enum):
    PENDING = "pending"
    ACTIVE = "active"
    VERIFIED = "verified"
    FAILED = "failed"
    INCONCLUSIVE = "inconclusive"
    SKIPPED = "skipped"
    USER_CONFIRMED = "user_confirmed"


class ActionType(str, Enum):
    CLICK = "click"
    TYPE = "type"
    SELECT = "select"
    CHECK = "check"
    SCROLL = "scroll"
    COMMAND = "command"
    CONFIRM = "confirm"
    OBSERVE = "observe"
    MANUAL = "manual"


class VerificationOutcome(str, Enum):
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    INCONCLUSIVE = "INCONCLUSIVE"


class ElementSource(str, Enum):
    ATSPI = "atspi"
    OCR = "ocr"
    VISION = "vision"
    WINDOW = "window"
    MANUAL = "manual"


class CoordinateSpace(str, Enum):
    """Only these coordinate spaces are accepted from model output."""

    PHYSICAL_SCREEN = "physical_screen"
    CAPTURE_PIXELS = "capture_pixels"
    NORMALIZED_IMAGE = "normalized_image"


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------


@dataclass
class Rect:
    x: int
    y: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height

    @property
    def center(self) -> tuple[int, int]:
        return (self.x + self.width // 2, self.y + self.height // 2)

    @property
    def area(self) -> int:
        return max(0, self.width) * max(0, self.height)

    def is_degenerate(self) -> bool:
        return self.width <= 1 or self.height <= 1

    def to_list(self) -> list[int]:
        return [int(self.x), int(self.y), int(self.width), int(self.height)]

    def inflate(self, padding: int) -> "Rect":
        return Rect(self.x - padding, self.y - padding, self.width + 2 * padding, self.height + 2 * padding)

    def intersects(self, other: "Rect") -> bool:
        return not (
            self.right <= other.x or other.right <= self.x or self.bottom <= other.y or other.bottom <= self.y
        )

    def iou(self, other: "Rect") -> float:
        if not self.intersects(other):
            return 0.0
        inter_w = min(self.right, other.right) - max(self.x, other.x)
        inter_h = min(self.bottom, other.bottom) - max(self.y, other.y)
        inter = max(0, inter_w) * max(0, inter_h)
        union = self.area + other.area - inter
        return util.safe_div(inter, union)

    def contains_point(self, px: int, py: int) -> bool:
        return self.x <= px < self.right and self.y <= py < self.bottom

    @staticmethod
    def from_any(value: Any) -> "Rect | None":
        """Accept [x,y,w,h] or {"x":..,} and reject anything malformed."""
        if isinstance(value, Rect):
            return value
        if isinstance(value, dict):
            try:
                return Rect(int(value["x"]), int(value["y"]), int(value["width"]), int(value["height"]))
            except (KeyError, TypeError, ValueError):
                return None
        if isinstance(value, (list, tuple)) and len(value) == 4:
            try:
                return Rect(*(int(round(float(v))) for v in value))
            except (TypeError, ValueError):
                return None
        return None


@dataclass
class Monitor:
    id: str
    name: str
    x: int
    y: int
    width: int
    height: int
    scale: float = 1.0
    primary: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def rect(self) -> Rect:
        return Rect(self.x, self.y, self.width, self.height)


@dataclass
class WindowInfo:
    id: str
    title: str
    app_class: str
    pid: int | None
    rect: Rect
    active: bool = False
    monitor_id: str | None = None
    sensitive: bool = False

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["rect"] = self.rect.to_list()
        return data


# ---------------------------------------------------------------------------
# Elements and observation
# ---------------------------------------------------------------------------


@dataclass
class Element:
    id: str
    name: str
    role: str
    rect: Rect
    source: ElementSource = ElementSource.ATSPI
    value: str | None = None
    enabled: bool = True
    confidence: float = 1.0
    app: str | None = None
    window_id: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "role": self.role,
            "rect": self.rect.to_list(),
            "source": self.source.value if isinstance(self.source, ElementSource) else str(self.source),
            "value": self.value,
            "enabled": self.enabled,
            "confidence": round(float(self.confidence), 4),
            "app": self.app,
            "window_id": self.window_id,
            "attributes": self.attributes,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "Element":
        rect = Rect.from_any(data.get("rect")) or Rect(0, 0, 0, 0)
        try:
            source = ElementSource(data.get("source", "atspi"))
        except ValueError:
            source = ElementSource.MANUAL
        return Element(
            id=str(data.get("id") or f"el-{uuid.uuid4().hex[:8]}"),
            name=str(data.get("name") or ""),
            role=str(data.get("role") or ""),
            rect=rect,
            source=source,
            value=data.get("value"),
            enabled=bool(data.get("enabled", True)),
            confidence=float(data.get("confidence", 1.0) or 0.0),
            app=data.get("app"),
            window_id=data.get("window_id"),
            attributes=data.get("attributes") or {},
        )

    def searchable_text(self) -> str:
        parts = [self.name or "", self.value or ""]
        label = self.attributes.get("description") if isinstance(self.attributes, dict) else None
        if label:
            parts.append(str(label))
        return util.normalize_text(" ".join(p for p in parts if p))


@dataclass
class CaptureFrame:
    """A single in-memory capture. Pixels are never persisted by default."""

    id: str
    scope: str  # "window" | "monitor" | "screen"
    target_id: str | None
    rect: Rect
    width: int
    height: int
    scale: float
    captured_at: int
    backend: str
    signature: str = ""
    change_ratio: float = 1.0
    path: str | None = None  # transient scratch file, deleted after use
    error: str | None = None

    def to_dict(self, include_path: bool = False) -> dict[str, Any]:
        data = {
            "id": self.id,
            "scope": self.scope,
            "target_id": self.target_id,
            "rect": self.rect.to_list(),
            "width": self.width,
            "height": self.height,
            "scale": self.scale,
            "captured_at": self.captured_at,
            "backend": self.backend,
            "signature": self.signature,
            "change_ratio": round(self.change_ratio, 5),
            "error": self.error,
        }
        if include_path:
            data["path"] = self.path
        return data


@dataclass
class Observation:
    id: str
    created_at: int
    session_type: str
    monitors: list[Monitor]
    active_window: WindowInfo | None
    windows: list[WindowInfo]
    elements: list[Element]
    frame: CaptureFrame | None
    sources: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    sensitive: bool = False

    def to_dict(self, include_elements: bool = True) -> dict[str, Any]:
        return {
            "id": self.id,
            "created_at": self.created_at,
            "session_type": self.session_type,
            "monitors": [m.to_dict() for m in self.monitors],
            "active_window": self.active_window.to_dict() if self.active_window else None,
            "windows": [w.to_dict() for w in self.windows],
            "elements": [e.to_dict() for e in self.elements] if include_elements else [],
            "element_count": len(self.elements),
            "frame": self.frame.to_dict() if self.frame else None,
            "sources": self.sources,
            "notes": self.notes,
            "sensitive": self.sensitive,
        }

    def text_blob(self, limit: int = 20000) -> str:
        chunks = []
        for element in self.elements:
            if element.name:
                chunks.append(element.name)
            if element.value:
                chunks.append(element.value)
        return util.truncated(" | ".join(chunks), limit)

    def find_elements(self, name: str, role: str | None = None) -> list[Element]:
        target = util.normalize_text(name)
        out = []
        for element in self.elements:
            if role and element.role != role:
                continue
            haystack = element.searchable_text()
            if target and target in haystack:
                out.append(element)
        return out


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


@dataclass
class VerificationRule:
    kind: str
    params: dict[str, Any] = field(default_factory=dict)
    required: bool = True
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "params": self.params, "required": self.required, "description": self.description}

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "VerificationRule | None":
        if not isinstance(data, dict) or not isinstance(data.get("kind"), str):
            return None
        params = data.get("params") if isinstance(data.get("params"), dict) else {}
        return VerificationRule(
            kind=data["kind"],
            params=params,
            required=bool(data.get("required", True)),
            description=str(data.get("description", "")),
        )


@dataclass
class Evidence:
    rule: str
    passed: bool | None
    detail: str
    observed: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {"rule": self.rule, "passed": self.passed, "detail": self.detail, "observed": self.observed}


@dataclass
class VerificationResult:
    outcome: VerificationOutcome
    evidence: list[Evidence]
    summary: str
    checked_at: int = field(default_factory=util.now_ms)

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "evidence": [e.to_dict() for e in self.evidence],
            "summary": self.summary,
            "checked_at": self.checked_at,
        }

    @property
    def verified(self) -> bool:
        return self.outcome == VerificationOutcome.VERIFIED


# ---------------------------------------------------------------------------
# Task model
# ---------------------------------------------------------------------------


@dataclass
class Step:
    id: str
    index: int
    title: str
    instruction: str
    action_type: ActionType = ActionType.CLICK
    target_description: str = ""
    target_selector: dict[str, Any] = field(default_factory=dict)
    text_value: dict[str, Any] | None = None
    expected_result: str = ""
    why: str = ""
    rules: list[VerificationRule] = field(default_factory=list)
    status: StepStatus = StepStatus.PENDING
    attempts: int = 0
    verified_at: int | None = None
    evidence: list[Evidence] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    requires_user_input: bool = False
    risk: str = "none"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "index": self.index,
            "title": self.title,
            "instruction": self.instruction,
            "action_type": self.action_type.value,
            "target_description": self.target_description,
            "target_selector": self.target_selector,
            "text_value": self.text_value,
            "expected_result": self.expected_result,
            "why": self.why,
            "rules": [r.to_dict() for r in self.rules],
            "status": self.status.value,
            "attempts": self.attempts,
            "verified_at": self.verified_at,
            "evidence": [e.to_dict() for e in self.evidence],
            "notes": self.notes,
            "requires_user_input": self.requires_user_input,
            "risk": self.risk,
        }


@dataclass
class Highlight:
    element_id: str | None
    rect: Rect
    label: str
    instruction: str
    confidence: float
    source: str
    pulsing: bool = True
    number: int | None = None
    arrow: str = "up"

    def to_dict(self) -> dict[str, Any]:
        return {
            "element_id": self.element_id,
            "rect": self.rect.to_list(),
            "label": self.label,
            "instruction": self.instruction,
            "confidence": round(float(self.confidence), 3),
            "source": self.source,
            "pulsing": self.pulsing,
            "number": self.number,
            "arrow": self.arrow,
        }


@dataclass
class Guidance:
    step_id: str
    instruction: str
    action_type: str
    highlight: Highlight | None
    text_to_enter: dict[str, Any] | None
    expected_result: str
    verification_method: str
    requires_confirmation: bool
    confidence: float
    why: str = ""
    alternatives: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "instruction": self.instruction,
            "action_type": self.action_type,
            "highlight": self.highlight.to_dict() if self.highlight else None,
            "text_to_enter": self.text_to_enter,
            "expected_result": self.expected_result,
            "verification_method": self.verification_method,
            "requires_confirmation": self.requires_confirmation,
            "confidence": round(float(self.confidence), 3),
            "why": self.why,
            "alternatives": self.alternatives,
            "warnings": self.warnings,
            "notes": self.notes,
        }


@dataclass
class Task:
    id: str
    objective: str
    state: TaskState
    steps: list[Step]
    current_index: int = 0
    created_at: int = field(default_factory=util.now_ms)
    updated_at: int = field(default_factory=util.now_ms)
    app_context: str = ""
    # The window this task was started on. Grounding stays anchored to it so a
    # focus change (the user reading the assistant, another app on a second
    # monitor) cannot silently move the guide to a different application.
    target_window_id: str = ""
    summary: str = ""
    facts: list[dict[str, Any]] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)
    observation: Observation | None = None
    guidance: Guidance | None = None
    verification: VerificationResult | None = None
    last_verified_step: int = -1
    paused_reason: str = ""
    error_budget: int = 0
    model_calls: int = 0
    workflow_id: str | None = None
    context_values: dict[str, Any] = field(default_factory=dict)

    @property
    def current_step(self) -> Step | None:
        if 0 <= self.current_index < len(self.steps):
            return self.steps[self.current_index]
        return None

    @property
    def progress(self) -> dict[str, int]:
        total = len(self.steps)
        done = sum(1 for s in self.steps if s.status in (StepStatus.VERIFIED, StepStatus.USER_CONFIRMED, StepStatus.SKIPPED))
        return {"total": total, "completed": done}

    def to_dict(self, include_observation: bool = False) -> dict[str, Any]:
        data = {
            "id": self.id,
            "objective": self.objective,
            "state": self.state.value,
            "steps": [s.to_dict() for s in self.steps],
            "current_index": self.current_index,
            "current_step": self.current_step.to_dict() if self.current_step else None,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "app_context": self.app_context,
            "target_window_id": self.target_window_id,
            "summary": self.summary,
            "facts": self.facts,
            "history": self.history[-80:],
            "guidance": self.guidance.to_dict() if self.guidance else None,
            "verification": self.verification.to_dict() if self.verification else None,
            "last_verified_step": self.last_verified_step,
            "paused_reason": self.paused_reason,
            "progress": self.progress,
            "model_calls": self.model_calls,
            "workflow_id": self.workflow_id,
            "context_values": {k: ("[sensitive]" if isinstance(v, dict) and v.get("sensitive") else v.get("value") if isinstance(v, dict) else v) for k, v in self.context_values.items()},
        }
        if include_observation:
            data["observation"] = self.observation.to_dict() if self.observation else None
        return data


ALLOWED_TRANSITIONS: dict[TaskState, set[TaskState]] = {
    TaskState.CREATED: {TaskState.OBSERVING, TaskState.PLANNING, TaskState.CANCELLED, TaskState.PAUSED, TaskState.BLOCKED},
    TaskState.OBSERVING: {TaskState.PLANNING, TaskState.READY_FOR_USER, TaskState.BLOCKED, TaskState.PAUSED, TaskState.CANCELLED, TaskState.OBSERVING},
    TaskState.PLANNING: {TaskState.READY_FOR_USER, TaskState.BLOCKED, TaskState.CANCELLED, TaskState.PAUSED, TaskState.OBSERVING},
    TaskState.READY_FOR_USER: {
        TaskState.WAITING_FOR_ACTION,
        TaskState.VERIFYING,
        TaskState.RECOVERING,
        TaskState.PAUSED,
        TaskState.CANCELLED,
        TaskState.COMPLETED,
        TaskState.BLOCKED,
        TaskState.READY_FOR_USER,
        TaskState.OBSERVING,
    },
    TaskState.WAITING_FOR_ACTION: {
        TaskState.VERIFYING,
        TaskState.READY_FOR_USER,
        TaskState.RECOVERING,
        TaskState.PAUSED,
        TaskState.CANCELLED,
        TaskState.BLOCKED,
        TaskState.COMPLETED,
        TaskState.OBSERVING,
    },
    TaskState.VERIFYING: {
        TaskState.READY_FOR_USER,
        TaskState.RECOVERING,
        TaskState.COMPLETED,
        TaskState.BLOCKED,
        TaskState.PAUSED,
        TaskState.CANCELLED,
        TaskState.WAITING_FOR_ACTION,
    },
    TaskState.RECOVERING: {TaskState.READY_FOR_USER, TaskState.BLOCKED, TaskState.PAUSED, TaskState.CANCELLED, TaskState.OBSERVING},
    TaskState.BLOCKED: {TaskState.RECOVERING, TaskState.PLANNING, TaskState.CANCELLED, TaskState.PAUSED, TaskState.READY_FOR_USER, TaskState.OBSERVING},
    TaskState.PAUSED: {TaskState.OBSERVING, TaskState.READY_FOR_USER, TaskState.CANCELLED, TaskState.VERIFYING, TaskState.PLANNING},
    TaskState.COMPLETED: {TaskState.OBSERVING},
    TaskState.CANCELLED: {TaskState.OBSERVING},
}


def can_transition(current: TaskState, target: TaskState) -> bool:
    return target in ALLOWED_TRANSITIONS.get(current, set())


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def step_id(index: int) -> str:
    return f"step-{index + 1:03d}"


def iter_rects(rects: Iterable[Sequence[int]]) -> list[Rect]:
    out = []
    for value in rects:
        rect = Rect.from_any(value)
        if rect is not None:
            out.append(rect)
    return out


def bounding_box(rects: Sequence[Rect]) -> Rect | None:
    if not rects:
        return None
    x0 = min(r.x for r in rects)
    y0 = min(r.y for r in rects)
    x1 = max(r.right for r in rects)
    y1 = max(r.bottom for r in rects)
    return Rect(x0, y0, x1 - x0, y1 - y0)


def jsonable(value: Any) -> Any:
    """Recursively convert dataclasses/enums into JSON-safe structures."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Rect):
        return value.to_list()
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return value
