"""Match a described control against perceived screen elements.

Grounding is deliberative: candidates are scored, near-ties are declared
*ambiguous* and require confirmation, and anything below the configured
confidence floor is refused rather than highlighted on a guess.

Scores are heuristic (name similarity + role affinity + geometry sanity). They
are not model output and are never presented as certainty: the UI shows the
confidence and the source of every highlight.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .. import util
from ..models import Element, ElementSource, Monitor, Rect
from .geometry import validate_rect_on_screens

# User-facing role words mapped to AT-SPI role names.
ROLE_ALIASES: dict[str, set[str]] = {
    "button": {"push button", "toggle button", "button"},
    "push button": {"push button", "toggle button"},
    "link": {"link"},
    "input": {"entry", "text", "spin button", "password text", "combo box"},
    "field": {"entry", "text", "spin button", "password text"},
    "entry": {"entry", "text", "password text"},
    "textbox": {"entry", "text"},
    "textarea": {"text", "entry"},
    "dropdown": {"combo box", "menu", "list box", "menu item"},
    "select": {"combo box", "list box", "radio button", "check box"},
    "combobox": {"combo box"},
    "checkbox": {"check box"},
    "check box": {"check box"},
    "radio": {"radio button"},
    "tab": {"page tab"},
    "menu": {"menu", "menu item", "menu bar"},
    "menu item": {"menu item", "check menu item", "radio menu item"},
    "dialog": {"dialog", "alert"},
    "icon": {"icon"},
    "slider": {"slider"},
    "scrollbar": {"scroll bar"},
}

ROLE_WORDS = {
    "button",
    "link",
    "field",
    "input",
    "textbox",
    "dropdown",
    "select",
    "checkbox",
    "radio",
    "tab",
    "menu",
    "item",
    "dialog",
    "icon",
    "box",
    "entry",
    "the",
    "a",
    "an",
    "click",
    "select",
    "choose",
    "on",
    "in",
    "of",
    "this",
    "that",
}

_NEGATIVE_ROLES_FOR_CLICK = {"frame", "window", "panel", "section", "document web"}


@dataclass
class Candidate:
    element: Element
    score: float
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "element_id": self.element.id,
            "label": self.element.name,
            "role": self.element.role,
            "rect": self.element.rect.to_list(),
            "source": self.element.source.value,
            "score": round(self.score, 3),
            "reasons": self.reasons,
        }


@dataclass
class GroundingResult:
    element: Element | None
    confidence: float
    source: str
    reasons: list[str] = field(default_factory=list)
    alternatives: list[dict[str, Any]] = field(default_factory=list)
    ambiguous: bool = False
    rejected: bool = False
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.element is not None and not self.rejected

    def to_dict(self) -> dict[str, Any]:
        return {
            "found": self.ok,
            "element_id": self.element.id if self.element else None,
            "label": self.element.name if self.element else None,
            "role": self.element.role if self.element else None,
            "rect": self.element.rect.to_list() if self.element else None,
            "confidence": round(self.confidence, 3),
            "source": self.source,
            "reasons": self.reasons,
            "alternatives": self.alternatives,
            "ambiguous": self.ambiguous,
            "rejected": self.rejected,
            "message": self.message,
        }


def _tokens(text: str) -> set[str]:
    return {t for t in util.normalize_text(text).split() if t and t not in ROLE_WORDS}


def _role_set(role_hint: str | None) -> set[str]:
    if not role_hint:
        return set()
    hint = util.normalize_text(role_hint)
    aliases: set[str] = set()
    for word in hint.split():
        if word in ROLE_ALIASES:
            aliases |= ROLE_ALIASES[word]
    if hint in ROLE_ALIASES:
        aliases |= ROLE_ALIASES[hint]
    if not aliases:
        aliases.add(hint)
    return aliases


def score_element(
    element: Element,
    description: str,
    role_hint: str | None = None,
    aliases: Iterable[str] | None = None,
    screens_area: int = 0,
) -> Candidate:
    reasons: list[str] = []
    target = util.normalize_text(description)
    haystack = element.searchable_text()
    if not haystack:
        return Candidate(element, 0.0, ["element has no accessible name"])

    candidates = [target] + [util.normalize_text(a) for a in (aliases or []) if a]
    best_label_score = 0.0
    for candidate in candidates:
        if not candidate:
            continue
        if candidate == haystack:
            best_label_score = max(best_label_score, 1.0)
            continue
        if candidate in haystack or haystack in candidate:
            ratio = min(len(candidate), len(haystack)) / max(len(candidate), len(haystack))
            best_label_score = max(best_label_score, 0.72 + 0.2 * ratio)
            continue
        target_tokens = _tokens(candidate)
        element_tokens = _tokens(haystack)
        if target_tokens and element_tokens:
            overlap = len(target_tokens & element_tokens) / len(target_tokens | element_tokens)
            if overlap >= 0.49:
                best_label_score = max(best_label_score, 0.55 + 0.35 * overlap)
        sequence = difflib.SequenceMatcher(None, candidate, haystack).ratio()
        if sequence > 0.62:
            best_label_score = max(best_label_score, 0.45 + 0.35 * sequence)

    if best_label_score <= 0:
        return Candidate(element, 0.0, ["label does not match"])

    score = best_label_score
    reasons.append(f"label similarity {best_label_score:.2f}")

    wanted_roles = _role_set(role_hint)
    if wanted_roles:
        if element.role in wanted_roles:
            score = min(1.0, score + 0.08)
            reasons.append(f"role matches '{element.role}'")
        else:
            score = max(0.0, score - 0.18)
            reasons.append(f"role '{element.role}' differs from requested '{role_hint}'")

    if element.source == ElementSource.ATSPI:
        score = min(1.0, score + 0.03)
        reasons.append("accessibility-provided bounds")
    elif element.source == ElementSource.OCR:
        score = max(0.0, score - 0.05)
        reasons.append("OCR-derived bounds (approximate)")
    else:
        score = max(0.0, score - 0.02)

    if element.rect.area == 0:
        return Candidate(element, 0.0, ["element has no geometry"])
    if element.rect.is_degenerate():
        score = max(0.0, score - 0.25)
        reasons.append("element geometry is degenerate")
    if screens_area and element.rect.area > screens_area * 0.35:
        score = max(0.0, score - 0.2)
        reasons.append("element covers a large part of the screen")
    if element.role in _NEGATIVE_ROLES_FOR_CLICK and (role_hint or "").strip().lower() not in {"dialog", "window"}:
        score = max(0.0, score - 0.1)
        reasons.append("element is a container, not a control")
    if not element.enabled:
        score = max(0.0, score - 0.1)
        reasons.append("element is disabled")

    return Candidate(element, min(1.0, score), reasons)


def rank_candidates(
    elements: Sequence[Element],
    description: str,
    role_hint: str | None = None,
    aliases: Iterable[str] | None = None,
    monitors: Sequence[Monitor] = (),
    limit: int = 5,
) -> list[Candidate]:
    screens_area = sum(m.rect.area for m in monitors) if monitors else 0
    ranked: list[Candidate] = []
    for element in elements:
        candidate = score_element(element, description, role_hint, aliases, screens_area)
        if candidate.score <= 0:
            continue
        if monitors:
            ok, reason = validate_rect_on_screens(element.rect, monitors)
            if not ok:
                candidate.score *= 0.6
                candidate.reasons.append(f"geometry rejected: {reason}")
        ranked.append(candidate)
    ranked.sort(key=lambda c: (c.score, c.element.source == ElementSource.ATSPI), reverse=True)
    return ranked[:limit]


def best_match(
    elements: Sequence[Element],
    description: str,
    role_hint: str | None = None,
    aliases: Iterable[str] | None = None,
    monitors: Sequence[Monitor] = (),
    min_confidence: float = 0.6,
    ambiguity_margin: float = 0.06,
) -> GroundingResult:
    ranked = rank_candidates(elements, description, role_hint, aliases, monitors)
    if not ranked:
        return GroundingResult(
            None,
            0.0,
            "none",
            rejected=True,
            message=(
                f"I could not find anything on screen matching '{description}'. "
                "Re-inspecting may help, or you can point me at the control and I will re-check."
            ),
        )
    top = ranked[0]
    alternatives = [c.to_dict() for c in ranked[1:4]]
    ambiguous = bool(
        len(ranked) > 1 and ranked[0].score >= min_confidence and abs(ranked[0].score - ranked[1].score) < ambiguity_margin
    )
    if top.score < min_confidence or top.element.rect.is_degenerate():
        return GroundingResult(
            top.element,
            top.score,
            top.element.source.value,
            reasons=top.reasons,
            alternatives=alternatives,
            rejected=True,
            message=(
                f"Confidence for '{description}' is only {top.score:.0%}, below the {min_confidence:.0%} threshold. "
                "I will not draw a highlight on a guess \u2014 please confirm the target."
            ),
        )
    ok, reason = validate_rect_on_screens(top.element.rect, monitors) if monitors else (True, "")
    if not ok:
        return GroundingResult(
            top.element,
            top.score,
            top.element.source.value,
            reasons=top.reasons + [reason],
            alternatives=alternatives,
            rejected=True,
            message=f"The best candidate's position is implausible ({reason}), so I will not highlight it.",
        )
    return GroundingResult(
        top.element,
        top.score,
        top.element.source.value,
        reasons=top.reasons,
        alternatives=alternatives,
        ambiguous=ambiguous,
        message=(
            "Two controls match closely; confirm which one you mean."
            if ambiguous
            else ""
        ),
    )


def merge_vision_with_accessibility(
    vision_rect: Rect,
    elements: Sequence[Element],
    min_overlap: float = 0.35,
) -> tuple[Element | None, float]:
    """Cross-check a vision box against accessibility elements.

    A vision box that overlaps a real accessible element is much more trustworthy
    than a bare prediction, so the accessible element's exact bounds replace the
    estimated ones. No overlap means no boost.
    """
    best: tuple[float, Element] | None = None
    for element in elements:
        if element.rect.area <= 0:
            continue
        inter_w = min(vision_rect.right, element.rect.right) - max(vision_rect.x, element.rect.x)
        inter_h = min(vision_rect.bottom, element.rect.bottom) - max(vision_rect.y, element.rect.y)
        overlap = max(0, inter_w) * max(0, inter_h)
        ratio = overlap / max(1, min(vision_rect.area, element.rect.area))
        if ratio >= min_overlap and (best is None or ratio > best[0]):
            best = (ratio, element)
    if best is None:
        return None, 0.0
    return best[1], best[0]


def find_text(elements: Sequence[Element], needle: str) -> Element | None:
    """Find an element whose text contains ``needle`` (normalized)."""
    target = util.normalize_text(needle)
    if not target:
        return None
    for element in elements:
        haystack = element.searchable_text()
        if target and target in haystack:
            return element
    return None


def element_inventory(elements: Sequence[Element], limit: int = 60) -> list[dict[str, Any]]:
    """Compact element list for prompts and UI inspection."""
    out = []
    for element in elements[:limit]:
        out.append(
            {
                "label": util.truncated(element.name, 80),
                "role": element.role,
                "rect": element.rect.to_list(),
                "value": util.truncated(element.value, 40) if element.value else None,
                "source": element.source.value,
            }
        )
    return out
