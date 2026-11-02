"""Scripted provider used exclusively by automated tests.

It is never reachable from user settings: :func:`build_provider` refuses the
``mock`` name, so production code cannot silently answer with canned text. Tests
import :class:`ScriptedProvider` directly.

Every response it returns is marked with ``provider="mock"`` and the text prefix
``[MOCK]`` so that mock-driven test output can never be mistaken for a live model
integration.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .base import ChatMessage, ModelCapabilities, ProviderError, ProviderResponse


@dataclass
class ScriptedProvider:
    responses: list[str] = field(default_factory=list)
    vision: bool = False
    error: ProviderError | None = None
    calls: list[list[ChatMessage]] = field(default_factory=list)

    name = "mock"

    def capabilities(self, model: str | None = None) -> ModelCapabilities:
        return ModelCapabilities(vision=self.vision, json_output=True, context_chars=8000, notes=["MOCK provider (tests only)"])

    def chat(self, messages: list[ChatMessage], *, model: str | None = None, json_mode: bool = False) -> ProviderResponse:
        self.calls.append(list(messages))
        if self.error is not None:
            raise self.error
        if not self.responses:
            raise ProviderError("unavailable", "ScriptedProvider ran out of scripted responses")
        text = self.responses.pop(0)
        if not text.startswith("[MOCK]"):
            text = f"[MOCK] {text}"
        return ProviderResponse(text=text, model=model or "mock-model", provider="mock", usage={"total_tokens": 10})

    @staticmethod
    def json_response(payload: dict[str, Any]) -> str:
        return "[MOCK] " + json.dumps(payload)
