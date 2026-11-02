"""Optional speech: local text-to-speech output and speech-input capability reporting."""

from .tts import SpeechOutput, tts_capability  # noqa: F401
from .stt import SpeechInput, stt_capability  # noqa: F401

__all__ = ["SpeechOutput", "tts_capability", "SpeechInput", "stt_capability"]
