"""API key storage.

Order of preference:

1. ``GUIDEPILOT_API_KEY`` environment variable (never written by GuidePilot).
2. ``secret-tool`` (libsecret / GNOME Keyring) — the normal path on XFCE/GNOME.
3. ``python3-keyring`` when importable.
4. A ``0600`` file in the user's config directory, clearly reported as weaker.

The key is read on demand and never logged, never returned by the API, and never
included in settings snapshots.
"""

from __future__ import annotations

import json
import os
import stat
from typing import Any

from .. import util

SERVICE = "guidepilot"
ACCOUNT = "api_key"
ENV_VAR = "GUIDEPILOT_API_KEY"


class SecretStore:
    def __init__(self, config_path=None) -> None:
        self._fallback_path = config_path or (util.config_dir() / "secrets.json")
        self._backend = self._detect_backend()
        self._cache: str | None = None

    # -- backend detection -------------------------------------------------
    def _detect_backend(self) -> str:
        if util.which("secret-tool"):
            return "secret-tool"
        try:
            import keyring  # type: ignore

            keyring.get_keyring()
            return "keyring"
        except Exception:
            pass
        return "file"

    @property
    def backend(self) -> str:
        return "env" if os.environ.get(ENV_VAR) else self._backend

    def describe(self) -> dict[str, Any]:
        descriptions = {
            "env": "Environment variable GUIDEPILOT_API_KEY",
            "secret-tool": "libsecret / GNOME Keyring (secret-tool)",
            "keyring": "Python keyring backend",
            "file": "0600 file in ~/.config/guidepilot (less secure: not an OS keyring)",
        }
        return {
            "backend": self.backend,
            "description": descriptions.get(self.backend, self.backend),
            "has_key": self.has_key(),
            "env_override": bool(os.environ.get(ENV_VAR)),
        }

    # -- operations --------------------------------------------------------
    def has_key(self) -> bool:
        return bool(self.get())

    def get(self) -> str:
        env_value = os.environ.get(ENV_VAR)
        if env_value:
            return env_value.strip()
        if self._cache is not None:
            return self._cache
        value = ""
        if self._backend == "secret-tool":
            result = util.run_cmd(
                ["secret-tool", "lookup", "service", SERVICE, "account", ACCOUNT], timeout=8
            )
            if result.ok:
                value = result.stdout.strip()
        elif self._backend == "keyring":
            try:
                import keyring  # type: ignore

                value = (keyring.get_password(SERVICE, ACCOUNT) or "").strip()
            except Exception:
                value = ""
        if not value:
            value = self._read_file()
        self._cache = value
        return value

    def set(self, value: str) -> str:
        """Store a key; returns the backend used. Empty value deletes it."""
        value = (value or "").strip()
        self._cache = None
        if not value:
            self.delete()
            return self.backend
        if self._backend == "secret-tool":
            result = util.run_cmd(
                ["secret-tool", "store", "--label", "GuidePilot API key", "service", SERVICE, "account", ACCOUNT],
                timeout=10,
                input_text=value,
            )
            if result.ok:
                self._cache = value
                return "secret-tool"
        elif self._backend == "keyring":
            try:
                import keyring  # type: ignore

                keyring.set_password(SERVICE, ACCOUNT, value)
                self._cache = value
                return "keyring"
            except Exception:
                pass
        payload = {"api_key": value, "note": "Written by GuidePilot. Not an OS keyring entry."}
        util.atomic_write_json(self._fallback_path, payload, mode=0o600)
        try:
            os.chmod(self._fallback_path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        self._cache = value
        return "file"

    def delete(self) -> None:
        self._cache = None
        if self._backend == "secret-tool":
            util.run_cmd(["secret-tool", "clear", "service", SERVICE, "account", ACCOUNT], timeout=8)
        elif self._backend == "keyring":
            try:
                import keyring  # type: ignore

                keyring.delete_password(SERVICE, ACCOUNT)
            except Exception:
                pass
        try:
            self._fallback_path.unlink()
        except OSError:
            pass

    def _read_file(self) -> str:
        data = util.read_json(self._fallback_path, {})
        if isinstance(data, dict) and isinstance(data.get("api_key"), str):
            return data["api_key"].strip()
        return ""

    # -- provider hook -----------------------------------------------------
    def as_provider(self):
        return lambda: self.get()
