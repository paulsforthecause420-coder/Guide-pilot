"""Coordinate mapping, grounding, screen-change measurement, settings, schemas."""

from __future__ import annotations

import json
import struct
import zlib

import pytest

from conftest import element, make_observation

from guidepilot.config import Settings, SettingsError
from guidepilot.grounding.geometry import (
    CaptureGeometry,
    capture_to_screen,
    clamp_to_monitors,
    normalized_to_screen,
    screen_to_capture,
    validate_rect_on_screens,
)
from guidepilot.grounding.matcher import best_match, element_inventory, find_text
from guidepilot.models import Rect
from guidepilot.perception.change import compare, image_size, signature_from_png
from guidepilot.perception.display import parse_xrandr_monitors
from guidepilot.validation import ValidationError, validate_against


# -- geometry ---------------------------------------------------------------


def test_capture_to_screen_scales_downscaled_frames():
    geometry = CaptureGeometry(rect=Rect(100, 50, 800, 600), width=400, height=300, scale=0.5)
    assert geometry.is_consistent()
    assert capture_to_screen([0, 0, 40, 20], geometry) == Rect(100, 50, 80, 40)
    assert screen_to_capture(Rect(100, 50, 80, 40), geometry) == Rect(0, 0, 40, 20)


def test_normalized_boxes_map_into_the_capture_region():
    geometry = CaptureGeometry(rect=Rect(0, 0, 1000, 500), width=1000, height=500, scale=1.0)
    assert normalized_to_screen([0.5, 0.5, 1.0, 1.0], geometry) == Rect(500, 250, 500, 250)


def test_rects_outside_the_known_monitors_are_rejected():
    monitors = parse_xrandr_monitors(
        "Monitors: 1\n 0: +*DP-1 1920/527x1080/296+0+0  DP-1\n"
    )
    assert monitors and monitors[0].primary is True
    ok, _reason = validate_rect_on_screens(Rect(10, 10, 100, 50), monitors)
    assert ok is True
    ok, reason = validate_rect_on_screens(Rect(5000, 5000, 100, 50), monitors)
    assert ok is False and reason


def test_clamping_keeps_a_rect_reachable():
    monitors = parse_xrandr_monitors("Monitors: 1\n 0: +*DP-1 1920/527x1080/296+0+0  DP-1\n")
    clamped = clamp_to_monitors(Rect(1900, 1060, 200, 100), monitors)
    assert clamped.x + clamped.width <= 1920
    assert clamped.y + clamped.height <= 1080


def test_two_monitor_layout_parses_offsets():
    monitors = parse_xrandr_monitors(
        "Monitors: 2\n 0: +*DP-1 1920/527x1080/296+0+0  DP-1\n 1: +HDMI-1 2560/600x1440/340+1920+0  HDMI-1\n"
    )
    assert len(monitors) == 2
    assert monitors[1].x == 1920
    assert monitors[1].width == 2560


# -- grounding --------------------------------------------------------------


def test_grounding_picks_the_control_not_its_label():
    observation = make_observation()
    result = best_match(observation.elements, "App Name", role_hint="entry")
    assert result.ok is True
    assert result.element is not None
    assert result.element.role == "entry"
    assert result.confidence >= 0.6


def test_grounding_refuses_when_nothing_matches():
    observation = make_observation()
    result = best_match(observation.elements, "Invoice total")
    assert result.ok is False
    assert result.element is None
    assert result.message


def test_grounding_is_rejected_for_ambiguous_targets():
    ambiguous = [
        element("Save", "push button", (300, 400, 80, 30)),
        element("Save", "push button", (500, 400, 80, 30)),
    ]
    result = best_match(ambiguous, "Save", role_hint="push button")
    assert result.ambiguous is True or result.ok is False


def test_element_inventory_is_bounded_and_labelled():
    observation = make_observation()
    inventory = element_inventory(observation.elements, limit=3)
    assert len(inventory) == 3
    assert all("label" in item and "role" in item for item in inventory)


def test_find_text_matches_what_is_on_screen():
    observation = make_observation()
    assert find_text(observation.elements, "Save Configuration") is not None
    assert find_text(observation.elements, "Nothing like this") is None


# -- screen change ----------------------------------------------------------


def _png(width: int, height: int, color: tuple[int, int, int]) -> bytes:
    raw = b"".join(b"\x00" + bytes(color) * width for _ in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def test_identical_frames_report_no_change():
    png = _png(32, 32, (255, 255, 255))
    ratio, hamming = compare(signature_from_png(png), signature_from_png(png))
    assert ratio == 0.0
    assert hamming == 0.0


def test_different_frames_report_change():
    previous = signature_from_png(_png(32, 32, (255, 255, 255)))
    current = signature_from_png(_png(32, 32, (0, 0, 0)))
    ratio, _hamming = compare(previous, current)
    assert ratio > 0.5, "a white frame and a black frame must be reported as a change"


def test_image_size_reads_the_png_header():
    assert image_size(_png(40, 24, (1, 2, 3))) == (40, 24)


# -- settings ---------------------------------------------------------------


def test_settings_validate_types(tmp_path):
    settings = Settings(tmp_path / "settings.json")
    with pytest.raises(SettingsError):
        settings.set("ui.opacity", "very see through")
    with pytest.raises(SettingsError):
        settings.set("privacy.share_enabled", "yes please")
    with pytest.raises(SettingsError):
        settings.set("nonexistent.section", 1)


def test_settings_persist_and_reload(tmp_path):
    path = tmp_path / "settings.json"
    settings = Settings(path)
    settings.set("ui.opacity", 0.5)
    settings.set("privacy.exclusion_patterns", ["keepass", "bitwarden"])
    settings.save()
    reloaded = Settings.load(path)
    assert reloaded.get("ui.opacity") == 0.5
    assert reloaded.get("privacy.exclusion_patterns") == ["keepass", "bitwarden"]


def test_share_enabled_defaults_to_off(tmp_path):
    settings = Settings(tmp_path / "settings.json")
    assert settings.get("privacy.share_enabled") is False, "capturing must be opt-in"


def test_public_view_contains_no_secret_material(tmp_path):
    settings = Settings(tmp_path / "settings.json")
    view = json.dumps(settings.public_view)
    assert "sk-" not in view
    assert settings.public_view["ai"]["has_api_key"] is False


def test_update_rejects_a_whole_section_of_wrong_type(tmp_path):
    settings = Settings(tmp_path / "settings.json")
    with pytest.raises(SettingsError):
        settings.update({"privacy": "off"})


# -- shared schemas ---------------------------------------------------------


def test_a_plan_payload_matching_the_schema_validates():
    payload = {
        "title": "Configure integration",
        "summary": "Fill the form",
        "steps": [
            {
                "title": "Enter App Name",
                "instruction": "Type the name",
                "action_type": "type",
                "target_label": "App Name",
                "target_role": "entry",
                "expected_result": "The field is filled",
                "why": "Required",
            }
        ],
    }
    validate_against("plan", payload)


def test_a_plan_without_steps_is_rejected():
    with pytest.raises(ValidationError) as excinfo:
        validate_against("plan", {"title": "x", "summary": "y", "steps": []})
    assert excinfo.value.errors


def test_guidance_schema_rejects_a_missing_instruction():
    with pytest.raises(ValidationError):
        validate_against("guidance", {"action_type": "click"})


def test_workflow_schema_rejects_an_unknown_action():
    with pytest.raises(ValidationError):
        validate_against(
            "workflow",
            {
                "name": "w",
                "version": 1,
                "steps": [{"title": "t", "instruction": "i", "action_type": "teleport"}],
            },
        )
