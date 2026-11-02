"""Verification must be evidence-based and must never invent success."""

from __future__ import annotations

from conftest import element, make_observation

from guidepilot.models import VerificationOutcome, VerificationRule
from guidepilot.verification.verifier import Verifier


def evaluate(rules, current, previous=None, user_confirmed=False):
    return Verifier().evaluate(rules, previous, current, user_confirmed=user_confirmed)


def filled_form(**values) -> list:
    """The demo form with the named fields carrying the given values."""
    fields = {
        "App Name": "GuidePilot Acceptance",
        "Callback URL": "https://acceptance.example.com/api/oauth/callback",
    }
    fields.update(values)
    elements = [
        element("App Name", "entry", (330, 176, 300, 30), value=fields.get("App Name")),
        element("Callback URL", "entry", (330, 226, 300, 30), value=fields.get("Callback URL")),
        element("Region", "combo box", (330, 276, 300, 30), value="EU"),
        element("Enable telemetry", "check box", (330, 326, 20, 20)),
        element("Save Configuration", "push button", (330, 420, 180, 36)),
        element("Configuration saved successfully", "label", (330, 470, 240, 20)),
    ]
    return elements


def test_element_appears_verifies_when_present():
    observation = make_observation(filled_form())
    result = evaluate(
        [VerificationRule("element_appears", {"label": "Configuration saved successfully"})],
        observation,
    )
    assert result.outcome == VerificationOutcome.VERIFIED
    assert result.evidence[0].passed is True


def test_element_appears_fails_when_screen_was_readable():
    observation = make_observation(filled_form())
    result = evaluate(
        [VerificationRule("element_appears", {"label": "Invoice generated"})],
        observation,
    )
    assert result.outcome == VerificationOutcome.FAILED


def test_missing_text_sources_are_inconclusive_not_failed():
    # No elements at all and no accessibility/OCR source: the verifier cannot see
    # anything, so claiming a failure would be a lie.
    blind = make_observation([])
    blind.sources = {}
    result = evaluate([VerificationRule("element_appears", {"label": "Saved"})], blind)
    assert result.outcome == VerificationOutcome.INCONCLUSIVE
    assert result.evidence[0].passed is None


def test_field_value_must_match_exactly():
    observation = make_observation(filled_form())
    ok = evaluate(
        [VerificationRule("element_value_equals", {"label": "App Name", "value": "GuidePilot Acceptance"})],
        observation,
    )
    assert ok.outcome == VerificationOutcome.VERIFIED

    wrong = evaluate(
        [VerificationRule("element_value_equals", {"label": "App Name", "value": "Something else"})],
        observation,
    )
    assert wrong.outcome == VerificationOutcome.FAILED
    assert "instead of the expected value" in wrong.summary


def test_unreadable_field_contents_are_inconclusive():
    # Chromium in basic accessibility mode publishes the control but no text.
    observation = make_observation(
        [element("App Name", "entry", (330, 176, 300, 30), value="")], values_unavailable=True
    )
    result = evaluate(
        [VerificationRule("element_value_equals", {"label": "App Name", "value": "Anything"})],
        observation,
    )
    assert result.outcome == VerificationOutcome.INCONCLUSIVE
    assert "cannot read it back" in result.summary


def test_sensitive_value_is_never_compared():
    observation = make_observation([element("API key", "password text", (330, 176, 300, 30), value="s3cr3t")])
    result = evaluate(
        [VerificationRule("element_value_equals", {"label": "API key", "value": "s3cr3t"})],
        observation,
    )
    assert result.outcome == VerificationOutcome.VERIFIED
    assert "not compared" in result.evidence[0].detail


def test_checkbox_state_is_read_from_accessibility():
    unchecked = element("Enable telemetry", "check box", (330, 326, 20, 20))
    unchecked.attributes = {"checked": False}
    observation = make_observation([unchecked])
    result = evaluate(
        [VerificationRule("element_checked", {"label": "Enable telemetry", "checked": True})],
        observation,
    )
    assert result.outcome == VerificationOutcome.FAILED

    checked = element("Enable telemetry", "check box", (330, 326, 20, 20))
    checked.attributes = {"checked": True}
    result = evaluate(
        [VerificationRule("element_checked", {"label": "Enable telemetry", "checked": True})],
        make_observation([checked]),
    )
    assert result.outcome == VerificationOutcome.VERIFIED


def test_checkbox_without_state_is_inconclusive():
    observation = make_observation([element("Enable telemetry", "check box", (330, 326, 20, 20))])
    result = evaluate(
        [VerificationRule("element_checked", {"label": "Enable telemetry", "checked": True})],
        observation,
    )
    assert result.outcome == VerificationOutcome.INCONCLUSIVE


def test_user_confirmation_alone_never_passes_silently():
    observation = make_observation(filled_form())
    rule = [VerificationRule("user_confirmation", {"label": "Step"})]
    assert evaluate(rule, observation).outcome == VerificationOutcome.INCONCLUSIVE
    assert evaluate(rule, observation, user_confirmed=True).outcome == VerificationOutcome.VERIFIED


def test_no_rules_is_inconclusive():
    result = evaluate([], make_observation(filled_form()))
    assert result.outcome == VerificationOutcome.INCONCLUSIVE
    assert "No verification criteria" in result.summary


def test_screen_changed_uses_measured_ratio():
    changed = make_observation(filled_form(), change_ratio=0.05)
    assert evaluate([VerificationRule("screen_changed", {"min_ratio": 0.01})], changed).outcome == VerificationOutcome.VERIFIED

    still = make_observation(filled_form(), change_ratio=0.0)
    assert evaluate([VerificationRule("screen_changed", {"min_ratio": 0.01})], still).outcome == VerificationOutcome.FAILED


def test_screen_changed_without_frame_is_inconclusive():
    observation = make_observation(filled_form())
    observation.frame = None
    assert evaluate([VerificationRule("screen_changed", {})], observation).outcome == VerificationOutcome.INCONCLUSIVE


def test_any_of_passes_on_one_expected_outcome():
    observation = make_observation(filled_form())
    rule = VerificationRule(
        "any_of",
        {"rules": [{"kind": "text_present", "params": {"text": "nothing like this"}}, {"kind": "text_present", "params": {"text": "saved"}}]},
    )
    assert evaluate([rule], observation).outcome == VerificationOutcome.VERIFIED


def test_any_of_all_negative_is_failed():
    observation = make_observation(filled_form())
    rule = VerificationRule(
        "any_of",
        {"rules": [{"kind": "text_present", "params": {"text": "nope"}}, {"kind": "text_present", "params": {"text": "never"}}]},
    )
    assert evaluate([rule], observation).outcome == VerificationOutcome.FAILED


def test_optional_rule_does_not_block_required_pass():
    observation = make_observation(filled_form(), change_ratio=0.0)
    rules = [
        VerificationRule("element_value_equals", {"label": "Region", "value": "EU"}),
        VerificationRule("screen_changed", {"min_ratio": 0.5}, required=False),
    ]
    assert evaluate(rules, observation).outcome == VerificationOutcome.VERIFIED


def test_unknown_rule_is_inconclusive():
    result = evaluate([VerificationRule("teleport_completed", {})], make_observation(filled_form()))
    assert result.outcome == VerificationOutcome.INCONCLUSIVE
    assert "Unknown verification rule" in result.summary


def test_describe_rules_produces_human_sentences():
    from guidepilot.verification.verifier import describe_rules

    descriptions = describe_rules(
        [
            VerificationRule("element_value_equals", {"label": "Region", "value": "EU"}),
            VerificationRule("user_confirmation", {"label": "Strict mode"}),
        ]
    )
    assert len(descriptions) == 2
    assert all(descriptions)
