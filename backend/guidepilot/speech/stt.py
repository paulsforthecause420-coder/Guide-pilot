"""Speech-to-text capability reporting and push-to-talk plumbing.

No speech-recognition model ships with GuidePilot: local transcription requires
a Whisper build the user installs themselves. This module reports that honestly
and gives the UI something real to display instead of a fake microphone button.
When a supported local engine is present, push-to-talk records and transcribes
through it.
"""

from __future__ import annotations

import contextlib
import tempfile
from pathlib import Path
from typing import Any

from .. import util

_STT_BINARIES = ("whisper", "whisper-cpp", "whisper-cli", "main")
_RECORDERS = ("arecord", "pw-record", "ffmpeg")


def stt_capability(preferred: str = "auto") -> dict[str, Any]:
    binary = util.which(preferred) if preferred not in ("", "auto") else None
    if binary is None:
        for name in _STT_BINARIES:
            binary = util.which(name)
            if binary:
                break
    recorder = next((util.which(name) for name in _RECORDERS if util.which(name)), None)
    if not binary:
        return {
            "available": False,
            "engine": None,
            "recorder": recorder.split("/")[-1] if recorder else None,
            "reason": (
                "No local speech-to-text engine found. Install whisper.cpp (or openai-whisper) to enable voice input; "
                "GuidePilot has no cloud speech dependency."
            ),
        }
    if not recorder:
        return {
            "available": False,
            "engine": binary.split("/")[-1],
            "recorder": None,
            "reason": "A transcription engine was found but no audio recorder (arecord/pw-record/ffmpeg).",
        }
    return {"available": True, "engine": binary, "recorder": recorder, "reason": ""}


class SpeechInput:
    """Push-to-talk: record a short clip and transcribe it locally."""

    def __init__(self, enabled: bool = False, engine: str = "auto") -> None:
        self.enabled = enabled
        self.capability_info = stt_capability(engine)

    def capability(self) -> dict[str, Any]:
        return self.capability_info

    def transcribe(self, audio_path: str) -> dict[str, Any]:
        """Transcribe an existing audio file. Refuses when voice input is disabled."""
        if not self.enabled:
            return {"text": "", "error": "Voice input is disabled in settings"}
        if not self.capability_info.get("available"):
            return {"text": "", "error": self.capability_info.get("reason", "Speech-to-text unavailable")}
        engine = self.capability_info["engine"]
        name = Path(engine).name
        if name in {"whisper", "whisper-cli", "main", "whisper-cpp"}:
            result = util.run_cmd([engine, "-f", audio_path, "-nt", "-otxt"], timeout=180)
            if not result.ok:
                return {"text": "", "error": util.truncated(result.stderr or "transcription failed", 300)}
            candidate = Path(audio_path).with_suffix(".txt")
            if candidate.exists():
                text = candidate.read_text(encoding="utf-8", errors="replace").strip()
                with contextlib.suppress(OSError):
                    candidate.unlink()
                return {"text": text, "engine": name}
            return {"text": result.stdout.strip(), "engine": name}
        return {"text": "", "error": f"Unsupported transcription engine: {name}"}

    def record_and_transcribe(self, seconds: float = 5.0) -> dict[str, Any]:
        """Record from the default microphone and transcribe (local only)."""
        if not self.enabled:
            return {"text": "", "error": "Voice input is disabled in settings"}
        info = self.capability_info
        if not info.get("available"):
            return {"text": "", "error": info.get("reason", "Speech-to-text unavailable")}
        recorder = info["recorder"]
        handle = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        path = Path(handle.name)
        handle.close()
        try:
            name = Path(recorder).name
            if name == "arecord":
                argv = [recorder, "-q", "-f", "cd", "-d", str(int(seconds)), str(path)]
            elif name == "pw-record":
                argv = [recorder, "--rate", "16000", "--channels", "1", str(path)]
            else:
                argv = [recorder, "-y", "-f", "alsa", "-i", "default", "-t", "wav", "-ar", "16000", str(path)]
            result = util.run_cmd(argv, timeout=seconds + 20)
            if not result.ok:
                return {"text": "", "error": util.truncated(result.stderr or "recording failed", 300)}
            return self.transcribe(str(path))
        finally:
            with contextlib.suppress(OSError):
                path.unlink()
