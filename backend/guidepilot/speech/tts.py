"""Local text-to-speech using espeak-ng / espeak / speech-dispatcher.

Voice output is *opt-in*: nothing is spoken unless the user enables it in
settings, and it is never used to deliver secrets (values are redacted and
sensitive fields are skipped entirely).
"""

from __future__ import annotations

import threading
from typing import Any

from .. import util

_ENGINES = ("espeak-ng", "espeak", "spd-say")


def detect_engine(preferred: str = "auto") -> str | None:
    if preferred and preferred != "auto":
        return util.which(preferred)
    for engine in _ENGINES:
        path = util.which(engine)
        if path:
            return path
    return None


def tts_capability(preferred: str = "auto") -> dict[str, Any]:
    binary = detect_engine(preferred)
    return {
        "available": bool(binary),
        "engine": binary.split("/")[-1] if binary else None,
        "reason": "" if binary else "No local TTS engine found (install espeak-ng or speech-dispatcher)",
    }


class SpeechOutput:
    """Speaks short instructions through a local engine (no network)."""

    def __init__(self, enabled: bool = False, engine: str = "auto") -> None:
        self.enabled = enabled
        self.engine = detect_engine(engine)
        self._lock = threading.Lock()
        self._last_spoken = ""

    def capability(self) -> dict[str, Any]:
        return tts_capability(self.engine or "auto")

    def speak(self, text: str, force: bool = False) -> dict[str, Any]:
        if not text:
            return {"spoken": False, "reason": "empty text"}
        if not self.enabled and not force:
            return {"spoken": False, "reason": "voice output is disabled"}
        if not self.engine:
            return {"spoken": False, "reason": "no TTS engine installed"}
        cleaned = util.scrub_secrets(text)[:400]
        with self._lock:
            name = self.engine.rsplit("/", 1)[-1]
            if name == "spd-say":
                result = util.run_cmd([self.engine, "-w", cleaned], timeout=15)
            else:
                result = util.run_cmd([self.engine, "-s", "165", cleaned], timeout=15)
            self._last_spoken = cleaned
        return {"spoken": result.ok, "engine": name, "error": "" if result.ok else util.truncated(result.stderr, 160)}
