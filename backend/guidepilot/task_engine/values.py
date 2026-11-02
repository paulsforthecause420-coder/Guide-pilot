"""Determining the exact text to enter into a field.

GuidePilot is strict here: a value is only offered when it comes from a source
that can be trusted for that field.

``user_provided``
    The user typed it (task context, chat answer, settings).
``project_config``
    Found in the user's own project files (``.env``, ``package.json``).
``derived``
    Computed from a user-provided base value with a stated rule — always labelled
    as an assumption that the user should check.
``unknown``
    Nothing reliable is known. GuidePilot asks instead of inventing.

Credentials, tokens, callback URLs and identifiers are never fabricated.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable

from .. import util
from ..security.redaction import is_sensitive_label

# Field label keywords mapped to likely project-config keys.
_ENV_KEY_HINTS: dict[str, tuple[str, ...]] = {
    "callback": ("OAUTH_CALLBACK_URL", "CALLBACK_URL", "REDIRECT_URI", "OAUTH_REDIRECT_URI", "AUTH_CALLBACK_URL"),
    "redirect": ("OAUTH_REDIRECT_URI", "REDIRECT_URI", "CALLBACK_URL"),
    "api base": ("API_BASE_URL", "BASE_URL", "APP_URL", "PUBLIC_URL"),
    "base url": ("BASE_URL", "API_BASE_URL", "APP_URL", "PUBLIC_URL"),
    "server url": ("SERVER_URL", "BASE_URL", "API_BASE_URL"),
    "client id": ("OAUTH_CLIENT_ID", "CLIENT_ID", "GITHUB_CLIENT_ID"),
    "app name": ("APP_NAME", "APPLICATION_NAME", "NAME"),
    "application name": ("APP_NAME", "APPLICATION_NAME"),
    "region": ("AWS_REGION", "REGION"),
    "database url": ("DATABASE_URL", "DB_URL"),
    "port": ("PORT", "APP_PORT"),
}

_SENSITIVE_ENV_TOKENS = ("SECRET", "TOKEN", "PASSWORD", "PRIVATE", "CREDENTIAL", "API_KEY")


def normalize_key(label: str) -> str:
    return util.normalize_text(label).replace(" ", "_")


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return values
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, raw = stripped.partition("=")
        key = key.strip()
        value = raw.strip().strip('"').strip("'")
        if key and value:
            values[key] = value
    return values


def read_project_config(project_dir: str | Path | None) -> dict[str, str]:
    """Read non-secret configuration keys from a project directory."""
    if not project_dir:
        return {}
    root = Path(project_dir).expanduser()
    if not root.is_dir():
        return {}
    config: dict[str, str] = {}
    for name in (".env", ".env.local", ".env.example", ".env.sample"):
        config.update(_read_env_file(root / name))
    package_json = root / "package.json"
    if package_json.exists():
        data = util.read_json(package_json, {})
        if isinstance(data, dict) and isinstance(data.get("name"), str):
            config.setdefault("APP_NAME", data["name"])
    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        text = util.read_text(pyproject)
        match = re.search(r'^\s*name\s*=\s*"([^"]+)"', text, re.MULTILINE)
        if match:
            config.setdefault("APP_NAME", match.group(1))
    # Never surface secret values from project files.
    return {k: v for k, v in config.items() if not any(token in k.upper() for token in _SENSITIVE_ENV_TOKENS)}


def _lookup_in_config(label: str, config: dict[str, str]) -> tuple[str, str] | None:
    normalized = util.normalize_text(label)
    for hint, keys in _ENV_KEY_HINTS.items():
        if hint in normalized:
            for key in keys:
                if key in config and config[key].strip():
                    return config[key].strip(), key
    # Fall back to a direct key match on the normalized label.
    candidate = normalize_key(label).upper()
    if candidate in config and config[candidate].strip():
        return config[candidate].strip(), candidate
    return None


def _coerce_context_entry(entry: Any) -> dict[str, Any] | None:
    if entry is None:
        return None
    if isinstance(entry, dict):
        value = entry.get("value")
        if value is None:
            return None
        return {
            "value": str(value),
            "source": str(entry.get("source") or "user_provided"),
            "sensitive": bool(entry.get("sensitive", False)),
            "confidence": float(entry.get("confidence", 0.95)),
            "explanation": str(entry.get("explanation") or ""),
        }
    if isinstance(entry, (int, float)):
        return {"value": str(entry), "source": "user_provided", "sensitive": False, "confidence": 0.95, "explanation": ""}
    if isinstance(entry, str) and entry.strip():
        return {"value": entry.strip(), "source": "user_provided", "sensitive": False, "confidence": 0.95, "explanation": ""}
    return None


def resolve_value(
    label: str,
    context_values: dict[str, Any] | None = None,
    project_dir: str | Path | None = None,
    project_config: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """Find the value for ``label`` from trusted sources only.

    ``context_values`` keys may be raw labels or normalized labels; both are
    matched. Field-specific entries win over project configuration.
    """
    if is_sensitive_label(label):
        # Sensitive values are never read from config or echoed; the user must
        # supply them through the secure input path.
        entry = _lookup_context(label, context_values or {})
        if entry is None:
            return {
                "value": "",
                "source": "unknown",
                "sensitive": True,
                "confidence": 0.0,
                "explanation": "Sensitive field: GuidePilot will not display, store or transmit this value.",
            }
        entry["sensitive"] = True
        return entry

    entry = _lookup_context(label, context_values or {})
    if entry is not None:
        return entry
    config = project_config if project_config is not None else read_project_config(project_dir)
    found = _lookup_in_config(label, config)
    if found is not None:
        value, key = found
        return {
            "value": value,
            "source": "project_config",
            "sensitive": False,
            "confidence": 0.8,
            "explanation": f"Read from your project configuration ({key}).",
        }
    return None


def _lookup_context(label: str, context_values: dict[str, Any]) -> dict[str, Any] | None:
    if not context_values:
        return None
    normalized = util.normalize_text(label)
    compact = normalize_key(label).lower()
    for key, entry in context_values.items():
        if not isinstance(key, str):
            continue
        key_normalized = util.normalize_text(key)
        if key_normalized in {normalized, compact} or normalized == key_normalized.replace("_", " "):
            coerced = _coerce_context_entry(entry)
            if coerced:
                return coerced
    # Substring match in either direction, longest key first for determinism.
    for key in sorted(context_values, key=lambda k: len(str(k)), reverse=True):
        if not isinstance(key, str):
            continue
        key_normalized = util.normalize_text(key).replace(" ", "")
        if len(key_normalized) >= 3 and (key_normalized in normalized.replace(" ", "") or normalized.replace(" ", "") in key_normalized):
            coerced = _coerce_context_entry(context_values[key])
            if coerced:
                return coerced
    return None


def derive_callback_url(base_url: str | None, path: str = "/api/oauth/callback") -> dict[str, Any] | None:
    """Derive a callback URL from an explicit base URL — as a labelled assumption."""
    if not base_url or not base_url.startswith(("http://", "https://")):
        return None
    return {
        "value": base_url.rstrip("/") + path,
        "source": "derived",
        "sensitive": False,
        "confidence": 0.5,
        "explanation": (
            f"Derived by appending {path} to the base URL you provided. "
            "This is an assumption \u2014 check it against your provider's documentation before saving."
        ),
    }


def missing_information(
    labels: Iterable[str],
    context_values: dict[str, Any] | None = None,
    project_dir: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Field labels that still need a value from the user."""
    config = read_project_config(project_dir)
    out = []
    for label in labels:
        if resolve_value(label, context_values, project_dir=None, project_config=config) is None:
            out.append(
                {
                    "field": label,
                    "question": f"What value should I use for '{label}'?",
                    "sensitive": is_sensitive_label(label),
                }
            )
    return out


def summarize_context_values(context_values: dict[str, Any]) -> dict[str, Any]:
    """Redacted view of task context values for UI/prompts."""
    out: dict[str, Any] = {}
    for key, entry in (context_values or {}).items():
        coerced = _coerce_context_entry(entry)
        if coerced is None:
            continue
        if coerced["sensitive"]:
            out[key] = {"value": "[sensitive]", "source": coerced["source"]}
        else:
            out[key] = {"value": coerced["value"], "source": coerced["source"]}
    return out


def load_json_file(path: str | Path) -> dict[str, Any]:
    data = util.read_json(Path(path), {})
    return data if isinstance(data, dict) else {}
