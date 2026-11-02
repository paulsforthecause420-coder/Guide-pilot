"""Provider-independent model interface.

Every adapter must:

* declare its capabilities (vision support, structured output, context size)
* return text plus, when available, token usage
* raise :class:`ProviderError` with a stable ``kind`` instead of leaking HTTP
  details, so the UI can explain what happened and the orchestrator can decide
  whether to fall back to local logic
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


class ProviderError(RuntimeError):
    """Model call failed. ``kind`` drives user-facing recovery advice."""

    KINDS = {
        "unconfigured",
        "auth",
        "rate_limit",
        "unavailable",
        "timeout",
        "network",
        "malformed",
        "unsupported",
        "budget",
        "cancelled",
    }

    def __init__(self, kind: str, message: str, *, retryable: bool = False, status: int | None = None) -> None:
        self.kind = kind if kind in self.KINDS else "unavailable"
        self.message = message
        self.retryable = retryable
        self.status = status
        super().__init__(message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "message": self.message,
            "retryable": self.retryable,
            "status": self.status,
        }

    def user_message(self) -> str:
        return {
            "unconfigured": "No AI provider is configured. GuidePilot will keep guiding you using local accessibility and OCR data.",
            "auth": "The AI provider rejected the API key. Check the key in Settings; local guidance still works.",
            "rate_limit": "The AI provider is rate limiting requests. I will slow down and retry.",
            "unavailable": "The AI provider is unreachable. Local guidance continues to work.",
            "timeout": "The AI provider timed out. I can retry or continue with local guidance.",
            "network": "A network problem blocked the AI request.",
            "malformed": "The model returned output that did not match the required structure, so I rejected it.",
            "unsupported": "The selected model does not support the requested capability (for example image input).",
            "budget": "The model call budget for this session is exhausted. Raise it in Settings to continue with the AI provider.",
            "cancelled": "The request was cancelled.",
        }.get(self.kind, self.message)


@dataclass
class ChatMessage:
    role: str  # system | user | assistant
    content: str
    images: list[bytes] = field(default_factory=list)
    image_media_type: str = "image/png"

    def to_dict(self) -> dict[str, Any]:
        return {"role": self.role, "content": self.content, "images": len(self.images)}


@dataclass
class ProviderResponse:
    text: str
    model: str
    provider: str
    usage: dict[str, int] = field(default_factory=dict)
    latency_ms: int = 0
    finish_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "model": self.model,
            "provider": self.provider,
            "usage": self.usage,
            "latency_ms": self.latency_ms,
            "finish_reason": self.finish_reason,
        }


@dataclass
class ModelCapabilities:
    vision: bool = False
    json_output: bool = True
    streaming: bool = False
    context_chars: int = 32000
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "vision": self.vision,
            "json_output": self.json_output,
            "streaming": self.streaming,
            "context_chars": self.context_chars,
            "notes": self.notes,
        }


class Provider(Protocol):
    name: str

    def capabilities(self, model: str | None = None) -> ModelCapabilities: ...

    def chat(self, messages: list[ChatMessage], *, model: str | None = None, json_mode: bool = False) -> ProviderResponse: ...
