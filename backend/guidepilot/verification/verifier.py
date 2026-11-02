"""Evidence-based step verification.

Three outcomes, and the difference matters:

``VERIFIED``
    At least one required rule produced positive evidence and none failed.
``FAILED``
    A required rule produced negative evidence (a control that should have
    appeared is measurably absent, a field holds the wrong value, ...).
``INCONCLUSIVE``
    The evidence needed to decide was unavailable (accessibility off, OCR
    missing, no verification rule) or the user has not confirmed. This is never
    upgraded to ``VERIFIED``.

Rules run against a *fresh* observation, never against the observation that
generated the instruction, so "clicked somewhere" can never satisfy a step.
"""

from __future__ import annotations

import re
from typing import Any, Sequence

from .. import util
from ..security.redaction import is_sensitive_label
from ..models import (
    Evidence,
    Observation,
    VerificationOutcome,
    VerificationResult,
    VerificationRule,
)

_RULE_TEXT = {
    "element_appears": "the expected control appears",
    "element_disappears": "the expected control disappears",
    "text_present": "the expected text is visible",
    "element_value_equals": "the field contains the expected value",
    "element_checked": "the checkbox is in the expected state",
    "window_title_matches": "the window title changes as expected",
    "screen_changed": "the screen visibly changes",
    "user_confirmation": "you confirm the result",
}


def describe_rules(rules: Sequence[VerificationRule]) -> list[str]:
    out: list[str] = []
    for rule in rules:
        base = _RULE_TEXT.get(rule.kind, rule.kind)
        if rule.description:
            out.append(rule.description)
        elif rule.kind in {"element_appears", "element_disappears"}:
            out.append(f"{base}: '{rule.params.get('label', '?')}'")
        elif rule.kind == "text_present":
            out.append(f"{base}: '{rule.params.get('text', '?')}'")
        elif rule.kind == "element_value_equals":
            out.append(f"{base}: '{rule.params.get('label', '?')}' = '{rule.params.get('value', '?')}'")
        else:
            out.append(base)
    return out


class Verifier:
    def __init__(self, min_change_ratio: float = 0.002) -> None:
        self.min_change_ratio = min_change_ratio

    # -- rule evaluation ---------------------------------------------------
    INTERACTIVE_HINTS = ("entry", "combo", "check", "radio", "button", "slider", "spin", "tab", "text")

    def _find(self, observation: Observation, label: str, role: str | None) -> Any:
        """Best element for a rule; exact name matches and real controls win.

        Returning the first substring match is wrong: a form's “Region” label
        would shadow the “Region” combo box. This ranks candidates so the
        control that actually holds the value is used.
        """
        target = util.normalize_text(label)
        if not target:
            return None
        best: tuple[float, Any] | None = None
        for element in observation.elements:
            if role and element.role != role and role not in element.role:
                continue
            name = util.normalize_text(element.name)
            haystack = element.searchable_text()
            if not haystack:
                continue
            score = 0.0
            if name == target:
                score += 10.0
            elif target in haystack:
                score += 5.0
            elif haystack in target:
                score += 3.0
            else:
                continue
            if any(hint in element.role for hint in self.INTERACTIVE_HINTS):
                score += 3.0
            if element.value is not None:
                score += 0.5
            if best is None or score > best[0]:
                best = (score, element)
        return best[1] if best else None

    def _text_sources_available(self, observation: Observation) -> bool:
        return bool(observation.elements) or bool(
            observation.sources.get("accessibility", {}).get("used")
            or observation.sources.get("ocr", {}).get("used")
        )

    def _evaluate_rule(
        self,
        rule: VerificationRule,
        previous: Observation | None,
        current: Observation,
        user_confirmed: bool,
    ) -> Evidence:
        params = rule.params or {}
        kind = rule.kind

        if kind == "element_appears":
            label = str(params.get("label") or params.get("text") or "")
            role = params.get("role")
            element = self._find(current, label, role)
            if element is not None:
                return Evidence(kind, True, f"Found '{label}' ({element.role}) at {element.rect.to_list()}", element.rect.to_list())
            if not self._text_sources_available(current):
                return Evidence(kind, None, f"No accessibility or OCR data was available to look for '{label}'")
            return Evidence(kind, False, f"'{label}' is not present on screen")

        if kind == "element_disappears":
            label = str(params.get("label") or "")
            role = params.get("role")
            element = self._find(current, label, role)
            if element is None:
                if not self._text_sources_available(current):
                    return Evidence(kind, None, f"No accessibility or OCR data was available to confirm '{label}' is gone")
                return Evidence(kind, True, f"'{label}' is no longer visible")
            return Evidence(kind, False, f"'{label}' is still visible at {element.rect.to_list()}", element.rect.to_list())

        if kind == "text_present":
            text = str(params.get("text") or "")
            any_of = params.get("any_of")
            needles = [text] if text else []
            if isinstance(any_of, list):
                needles.extend(str(v) for v in any_of if isinstance(v, (str, int, float)))
            needles = [n for n in needles if n]
            haystack = " ".join(
                filter(
                    None,
                    [e.name for e in current.elements]
                    + [e.value or "" for e in current.elements]
                    + [current.active_window.title if current.active_window else ""],
                )
            )
            normalized_haystack = util.normalize_text(haystack)
            hit = next((n for n in needles if util.normalize_text(n) in normalized_haystack), None)
            if hit:
                return Evidence(kind, True, f"Found expected text '{hit}'")
            if not self._text_sources_available(current):
                return Evidence(kind, None, "No accessibility or OCR data was available to search for the expected text")
            return Evidence(kind, False, "None of the expected texts are visible")

        if kind == "element_value_equals":
            label = str(params.get("label") or "")
            expected = str(params.get("value") or "")
            element = self._find(current, label, params.get("role"))
            if element is None:
                if not self._text_sources_available(current):
                    return Evidence(kind, None, f"No accessibility data was available to read '{label}'")
                return Evidence(kind, False, f"Field '{label}' was not found")
            if current.sources.get("accessibility", {}).get("values_unavailable") and not (element.value or "").strip():
                # The application publishes no text at all, so an empty read says
                # nothing about what the field actually contains. Reporting a
                # mismatch here would accuse the user of a failure that did not
                # happen.
                return Evidence(
                    kind,
                    None,
                    f"This application does not publish the contents of '{label}', so I cannot read it back",
                )
            actual = util.normalize_text(element.value or "")
            if expected and is_sensitive_label(label):
                return Evidence(kind, True if actual else None, "Sensitive field: presence checked, value not compared")
            if expected and actual == util.normalize_text(expected):
                return Evidence(kind, True, f"Field '{label}' contains the expected value")
            return Evidence(
                kind,
                False,
                f"Field '{label}' contains '{util.truncated(element.value or '', 40)}' instead of the expected value",
                element.value,
            )

        if kind == "element_checked":
            label = str(params.get("label") or "")
            expected = bool(params.get("checked", True))
            element = self._find(current, label, params.get("role"))
            if element is None:
                if not self._text_sources_available(current):
                    return Evidence(kind, None, f"No accessibility data was available to read '{label}'")
                return Evidence(kind, False, f"Control '{label}' was not found")
            actual = element.attributes.get("checked") if isinstance(element.attributes, dict) else None
            if actual is None:
                return Evidence(
                    kind,
                    None,
                    f"'{label}' does not expose a checkable state through accessibility, so I cannot confirm it",
                )
            return Evidence(
                kind,
                bool(actual) == expected,
                f"'{label}' is {'checked' if actual else 'not checked'}",
                bool(actual),
            )

        if kind == "window_title_matches":
            pattern = str(params.get("pattern") or "")
            title = current.active_window.title if current.active_window else ""
            if not title:
                return Evidence(kind, None, "No window title was available")
            try:
                matched = bool(re.search(pattern, title, re.IGNORECASE))
            except re.error:
                matched = pattern.lower() in title.lower()
            return Evidence(kind, matched, f"Window title is '{util.truncated(title, 80)}'")

        if kind == "screen_changed":
            threshold = float(params.get("min_ratio", self.min_change_ratio))
            if current.frame is None:
                return Evidence(kind, None, "No frame was captured, so screen change cannot be measured")
            ratio = current.frame.change_ratio
            return Evidence(
                kind,
                ratio >= threshold,
                f"Screen changed by {ratio:.1%} (threshold {threshold:.1%})",
                round(ratio, 4),
            )

        if kind == "user_confirmation":
            return Evidence(kind, True if user_confirmed else None, "Waiting for your confirmation")

        if kind == "any_of":
            sub_rules = params.get("rules")
            if not isinstance(sub_rules, list) or not sub_rules:
                return Evidence(kind, None, "any_of rule had no sub-rules")
            results: list[Evidence] = []
            for raw in sub_rules:
                sub_rule = VerificationRule.from_dict(raw) if isinstance(raw, dict) else None
                if sub_rule is None:
                    continue
                results.append(self._evaluate_rule(sub_rule, previous, current, user_confirmed))
            if not results:
                return Evidence(kind, None, "any_of rule had no usable sub-rules")
            if any(r.passed is True for r in results):
                winner = next(r for r in results if r.passed is True)
                return Evidence(kind, True, f"One expected outcome was observed: {winner.detail}", winner.observed)
            if all(r.passed is False for r in results):
                return Evidence(kind, False, "None of the expected outcomes were observed: " + "; ".join(r.detail for r in results))
            return Evidence(kind, None, "Not enough evidence to confirm any expected outcome yet")

        return Evidence(kind, None, f"Unknown verification rule '{kind}'")

    # -- public API --------------------------------------------------------
    def evaluate(
        self,
        rules: Sequence[VerificationRule],
        previous: Observation | None,
        current: Observation,
        user_confirmed: bool = False,
    ) -> VerificationResult:
        if not rules:
            return VerificationResult(
                VerificationOutcome.INCONCLUSIVE,
                [Evidence("none", None, "No verification rule was defined for this step")],
                "No verification criteria were available, so I cannot confirm this step.",
            )
        evidence: list[Evidence] = []
        for rule in rules:
            try:
                evidence.append(self._evaluate_rule(rule, previous, current, user_confirmed))
            except Exception as exc:  # pragma: no cover - defensive
                evidence.append(Evidence(rule.kind, None, f"Rule failed to evaluate: {exc}"))

        required = [e for e, rule in zip(evidence, rules) if rule.required]
        positives = [e for e in required if e.passed is True]
        negatives = [e for e in required if e.passed is False]
        unknowns = [e for e in required if e.passed is None]

        if negatives:
            outcome = VerificationOutcome.FAILED
            summary = "I could not confirm this step: " + negatives[0].detail
        elif positives and not unknowns:
            outcome = VerificationOutcome.VERIFIED
            summary = "Verified: " + positives[0].detail
        elif positives:
            outcome = VerificationOutcome.INCONCLUSIVE
            summary = "Partial evidence: " + positives[0].detail + "; " + unknowns[0].detail
        else:
            outcome = VerificationOutcome.INCONCLUSIVE
            summary = unknowns[0].detail if unknowns else "No evidence was available"
        return VerificationResult(outcome, evidence, summary)
