"""User settings: defaults, validation, persistence.

Settings are plain JSON so users can inspect and edit them by hand. API keys are
never stored here: only a reference name pointing at the OS keyring entry (see
``guidepilot.security.secrets``).
"""

from __future__ import annotations

import copy
import os
import secrets as _secrets
from pathlib import Path
from typing import Any, Callable

from . import util

DEFAULTS: dict[str, Any] = {
    "server": {
        "host": "127.0.0.1",
        "port": 8765,
    },
    "observation": {
        # "active_window" | "monitor" | "all_monitors"
        "scope": "active_window",
        "monitor_id": None,
        # Automatic re-observation cadence; 0 disables polling entirely.
        "cadence_ms": 2500,
        "idle_backoff": True,
        "min_change_ratio": 0.004,
        "cache_size": 6,
        "downscale_width": 1280,
        "poll_only_when_task_active": True,
    },
    "privacy": {
        "share_enabled": False,
        "paused": False,
        "stop_capture": False,
        "retain_screenshots": False,
        "redact_secrets": True,
        "local_only": False,
        "exclusion_patterns": [
            "keepass",
            "1password",
            "bitwarden",
            "gnome-keyring",
            "seahorse",
            "lastpass",
            "authenticator",
            "password",
        ],
        "acknowledge_cloud_vision": False,
    },
    "ai": {
        # "none" | "openai_compat" | "ollama"
        "provider": "none",
        "base_url": "http://127.0.0.1:11434",
        "model_vision": "",
        "model_reasoning": "",
        "model_instruction": "",
        "model_verification": "",
        "timeout_ms": 45000,
        "max_retries": 2,
        "max_context_chars": 24000,
        "call_budget": 200,
        "api_key_ref": "guidepilot/api_key",
        "vision_grounding": True,
    },
    "ui": {
        "theme": "dark",
        "opacity": 0.97,
        "compact": False,
        "always_on_top": True,
        "overlay_enabled": True,
        "dim_background": False,
        "pulse_highlight": True,
        "edge_docking": "none",
        "window_bounds": {"x": 80, "y": 80, "width": 430, "height": 680},
        "teach_me_mode": False,
        "shortcuts": {
            "toggle": "CommandOrControl+Shift+G",
            "stop": "CommandOrControl+Shift+Escape",
            "repeat": "CommandOrControl+Shift+R",
        },
    },
    "voice": {
        "enabled": False,
        "tts_engine": "auto",
        "stt_engine": "auto",
        "push_to_talk": "CommandOrControl+Alt+Space",
        "speak_steps": True,
        "speak_errors": True,
    },
    "automation": {
        "assisted_actions_enabled": False,
        "allow_paste": False,
        "allow_focus": False,
        "allow_scroll": False,
        "require_confirmation": True,
    },
    "perception": {
        "use_accessibility": True,
        "use_ocr": True,
        "min_grounding_confidence": 0.6,
        "app_hint": "",
    },
}

# Keys that may be written by the renderer through PUT /settings.
_WRITABLE = set(DEFAULTS.keys())


class SettingsError(ValueError):
    """Raised when a settings patch is invalid."""


def _merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in patch.items():
        if key not in _WRITABLE:
            raise SettingsError(f"Unknown settings section: {key}")
        if not isinstance(value, dict):
            raise SettingsError(f"Settings section '{key}' must be an object")
        for sub_key, sub_value in value.items():
            if sub_key not in DEFAULTS[key]:
                raise SettingsError(f"Unknown setting: {key}.{sub_key}")
            out[key][sub_key] = _validate_value(key, sub_key, sub_value)
    return out


def _validate_value(section: str, key: str, value: Any) -> Any:
    default = DEFAULTS[section][key]
    path = f"{section}.{key}"
    if isinstance(default, bool):
        if not isinstance(value, bool):
            raise SettingsError(f"{path} must be a boolean")
        return value
    if isinstance(default, int) and not isinstance(default, bool):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SettingsError(f"{path} must be a number")
        number = int(value)
        ranges = {
            "observation.cadence_ms": (0, 600000),
            "observation.cache_size": (1, 40),
            "observation.downscale_width": (320, 7680),
            "ui.opacity": (0.3, 1.0),
            "ai.timeout_ms": (1000, 300000),
            "ai.max_retries": (0, 8),
            "ai.max_context_chars": (1000, 400000),
            "ai.call_budget": (0, 100000),
            "server.port": (0, 65535),
        }
        low, high = ranges.get(path, (-10**9, 10**9))
        if not (low <= number <= high):
            raise SettingsError(f"{path} must be between {low} and {high}")
        return number
    if isinstance(default, float):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SettingsError(f"{path} must be a number")
        number = float(value)
        if not (0.3 <= number <= 1.0):
            raise SettingsError(f"{path} must be between 0.3 and 1.0")
        return number
    if isinstance(default, str):
        if not isinstance(value, str):
            raise SettingsError(f"{path} must be a string")
        if path == "observation.scope" and value not in {"active_window", "monitor", "all_monitors"}:
            raise SettingsError("observation.scope must be active_window, monitor or all_monitors")
        if path == "ui.theme" and value not in {"dark", "light"}:
            raise SettingsError("ui.theme must be dark or light")
        if path == "ai.provider" and value not in {"none", "openai_compat", "ollama"}:
            raise SettingsError("ai.provider must be none, openai_compat or ollama")
        if path in {"ai.base_url"} and value and not value.startswith(("http://", "https://", "file://")):
            raise SettingsError(f"{path} must be an http(s) URL")
        if path == "ai.base_url" and value.startswith("https://"):
            return value.rstrip("/")
        if path in {"ai.model_vision", "ai.model_reasoning", "ai.model_instruction", "ai.model_verification"}:
            return value.strip()
        return value
    if default is None:
        if value is not None and not isinstance(value, str):
            raise SettingsError(f"{path} must be a string or null")
        return value
    if isinstance(default, list):
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise SettingsError(f"{path} must be a list of strings")
        return [v.strip() for v in value if v.strip()]
    if isinstance(default, dict):
        if not isinstance(value, dict):
            raise SettingsError(f"{path} must be an object")
        merged = copy.deepcopy(default)
        for sub_key, sub_value in value.items():
            if sub_key not in default:
                raise SettingsError(f"Unknown setting: {path}.{sub_key}")
            expected = default[sub_key]
            if isinstance(expected, bool) and not isinstance(sub_value, bool):
                raise SettingsError(f"{path}.{sub_key} must be a boolean")
            if isinstance(expected, str) and not isinstance(sub_value, str):
                raise SettingsError(f"{path}.{sub_key} must be a string")
            if isinstance(expected, (int, float)) and not isinstance(expected, bool):
                if isinstance(sub_value, bool) or not isinstance(sub_value, (int, float)):
                    raise SettingsError(f"{path}.{sub_key} must be a number")
            merged[sub_key] = sub_value
        return merged
    return value


def _coerce_loaded(raw: Any) -> dict[str, Any]:
    """Merge a settings file without failing hard on stale/unknown keys."""
    out = copy.deepcopy(DEFAULTS)
    if not isinstance(raw, dict):
        return out
    for section, values in raw.items():
        if section not in DEFAULTS or not isinstance(values, dict):
            continue
        for key, value in values.items():
            if key not in DEFAULTS[section]:
                continue
            try:
                out[section][key] = _validate_value(section, key, value)
            except SettingsError:
                continue
    return out


class Settings:
    """Thread-safe settings container with atomic persistence."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or util.settings_path()
        self._lock = __import__("threading").RLock()
        self._data = copy.deepcopy(DEFAULTS)

    # -- lifecycle ---------------------------------------------------------
    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        instance = cls(path)
        instance.reload()
        return instance

    def reload(self) -> dict[str, Any]:
        with self._lock:
            self._data = _coerce_loaded(util.read_json(self.path, {}))
            return copy.deepcopy(self._data)

    def save(self) -> None:
        with self._lock:
            util.atomic_write_json(self.path, self._data, mode=0o600)

    # -- access -----------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._data)

    def get(self, dotted: str, default: Any = None) -> Any:
        section, _, key = dotted.partition(".")
        with self._lock:
            if key:
                return self._data.get(section, {}).get(key, default)
            return self._data.get(section, default)

    def section(self, name: str) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._data.get(name, {}))

    def update(self, patch: dict[str, Any]) -> dict[str, Any]:
        """Apply a validated patch and persist it."""
        with self._lock:
            merged = _merge(self._data, patch)
            self._data = merged
            self.save()
            return copy.deepcopy(self._data)

    def set(self, dotted: str, value: Any) -> None:
        section, _, key = dotted.partition(".")
        if not key:
            raise SettingsError("Settings key must be section.key")
        self.update({section: {key: value}})

    # -- derived ----------------------------------------------------------
    @property
    def public_view(self) -> dict[str, Any]:
        """Settings safe to send to the renderer (no secret values by design)."""
        data = self.snapshot()
        data["ai"]["has_api_key"] = bool(os.environ.get("GUIDEPILOT_API_KEY"))
        return data


def generate_runtime_token() -> str:
    return _secrets.token_urlsafe(32)


def write_runtime_token(token: str) -> Path:
    path = util.runtime_dir() / "token"
    util.atomic_write_text(path, token, mode=0o600)
    return path
