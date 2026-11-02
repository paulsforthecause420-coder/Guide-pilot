"""Model gateway: role-based routing, budgets, structured output, fallback.

The gateway is the only path to a model. It enforces:

* **Budget** — a configurable ceiling of model calls per session, so an agentic
  loop can never run away with the user's API credit.
* **Local-only mode** — when enabled, non-loopback endpoints are refused.
* **Structured output** — JSON is extracted, parsed and validated against a
  schema from ``shared/schemas``; invalid output is *rejected*, never repaired
  into something actionable.
* **Graceful degradation** — when no provider is reachable the caller gets a
  typed error and continues with local accessibility/OCR guidance.
"""

from __future__ import annotations

import json
import re
import threading
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

from .. import util, validation
from ..config import Settings
from .base import ChatMessage, ModelCapabilities, ProviderError, ProviderResponse
from .openai_compat import build_provider

ROLE_VISION = "vision"
ROLE_REASONING = "reasoning"
ROLE_INSTRUCTION = "instruction"
ROLE_VERIFICATION = "verification"


def _is_loopback(url: str) -> bool:
    try:
        host = urllib.parse.urlparse(url).hostname or ""
    except ValueError:
        return False
    return host in {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


@dataclass
class GatewayStatus:
    configured: bool
    provider: str
    models: dict[str, str]
    capabilities: dict[str, Any]
    budget: dict[str, int]
    last_error: dict[str, Any] | None = None
    local_only: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "provider": self.provider,
            "models": self.models,
            "capabilities": self.capabilities,
            "budget": self.budget,
            "last_error": self.last_error,
            "local_only": self.local_only,
        }


@dataclass
class GatewayStats:
    calls: int = 0
    failures: int = 0
    rejected_outputs: int = 0
    tokens: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


class ModelGateway:
    """Routes model requests by role and validates their output."""

    def __init__(self, settings: Settings, api_key_provider=None) -> None:
        self.settings = settings
        self._api_key_provider = api_key_provider or (lambda: "")
        self._lock = threading.RLock()
        self.stats = GatewayStats()
        self.last_error: ProviderError | None = None
        self._provider = None
        self._provider_signature: tuple[Any, ...] | None = None
        budget = int(settings.get("ai.call_budget", 200) or 0)
        self.budget = util.BudgetCounter(limit=budget)

    # -- configuration -----------------------------------------------------
    def _signature(self) -> tuple[Any, ...]:
        ai = self.settings.section("ai")
        return (
            ai.get("provider"),
            ai.get("base_url"),
            ai.get("model_vision"),
            ai.get("model_reasoning"),
            ai.get("model_instruction"),
            ai.get("model_verification"),
            ai.get("timeout_ms"),
            ai.get("max_retries"),
            ai.get("max_context_chars"),
        )

    def provider(self):
        with self._lock:
            signature = self._signature()
            if self._provider is not None and self._provider_signature == signature:
                return self._provider
            ai = self.settings.section("ai")
            if ai.get("provider") == "none":
                self._provider = None
                self._provider_signature = signature
                raise ProviderError("unconfigured", "No AI provider configured")
            if self.settings.get("privacy.local_only") and ai.get("provider") == "openai_compat":
                if not _is_loopback(str(ai.get("base_url") or "")):
                    raise ProviderError(
                        "unsupported",
                        "Local-only mode is enabled, so a remote AI endpoint is refused. Point the base URL at a local server or disable local-only mode.",
                    )
            api_key = ""
            try:
                api_key = self._api_key_provider() or ""
            except Exception:
                api_key = ""
            self._provider = build_provider(ai, api_key=api_key)
            self._provider_signature = signature
            return self._provider

    def model_for(self, role: str) -> str:
        ai = self.settings.section("ai")
        mapping = {
            ROLE_VISION: ai.get("model_vision"),
            ROLE_REASONING: ai.get("model_reasoning"),
            ROLE_INSTRUCTION: ai.get("model_instruction"),
            ROLE_VERIFICATION: ai.get("model_verification"),
        }
        chosen = (mapping.get(role) or "").strip()
        if chosen:
            return chosen
        for fallback in (ROLE_REASONING, ROLE_INSTRUCTION, ROLE_VISION):
            candidate = (mapping.get(fallback) or "").strip()
            if candidate:
                return candidate
        return ""

    def capabilities(self, role: str = ROLE_REASONING) -> ModelCapabilities:
        try:
            return self.provider().capabilities(self.model_for(role))
        except ProviderError:
            return ModelCapabilities(vision=False, json_output=False, context_chars=0, notes=["No provider configured"])

    def available(self) -> tuple[bool, str]:
        try:
            provider = self.provider()
        except ProviderError as exc:
            return False, exc.user_message()
        model = self.model_for(ROLE_REASONING)
        if not model:
            return False, "No model name is configured."
        caps = provider.capabilities(model)
        del caps
        return True, ""

    def status(self) -> GatewayStatus:
        ai = self.settings.section("ai")
        configured = ai.get("provider") not in (None, "", "none")
        caps: dict[str, Any] = {}
        ok, reason = self.available()
        if ok:
            caps = self.capabilities(ROLE_VISION).to_dict()
        else:
            caps = {"available": False, "reason": reason}
        return GatewayStatus(
            configured=configured,
            provider=str(ai.get("provider")),
            models={
                ROLE_VISION: self.model_for(ROLE_VISION),
                ROLE_REASONING: self.model_for(ROLE_REASONING),
                ROLE_INSTRUCTION: self.model_for(ROLE_INSTRUCTION),
                ROLE_VERIFICATION: self.model_for(ROLE_VERIFICATION),
            },
            capabilities=caps,
            budget=self.budget.snapshot(),
            last_error=self.last_error.to_dict() if self.last_error else None,
            local_only=bool(self.settings.get("privacy.local_only")),
        )

    def reset_budget(self, limit: int | None = None) -> None:
        if limit is not None:
            self.budget.limit = max(0, int(limit))
        self.budget.used = 0

    # -- calls -------------------------------------------------------------
    def complete(
        self,
        role: str,
        messages: list[ChatMessage],
        *,
        json_mode: bool = False,
        consume_budget: bool = True,
    ) -> ProviderResponse:
        provider = self.provider()
        model = self.model_for(role)
        if not model:
            raise ProviderError("unconfigured", f"No model configured for the {role} role")
        if consume_budget and not self.budget.consume(1):
            error = ProviderError(
                "budget",
                f"Model call budget of {self.budget.limit} calls is exhausted",
            )
            self.last_error = error
            raise error
        caps = provider.capabilities(model)
        if any(message.images for message in messages) and not caps.vision:
            error = ProviderError(
                "unsupported",
                f"Model '{model}' does not accept image input; screenshots cannot be sent to it",
            )
            self.last_error = error
            self.stats.failures += 1
            raise error
        self.stats.calls += 1
        try:
            response = provider.chat(messages, model=model, json_mode=json_mode)
        except ProviderError as exc:
            self.stats.failures += 1
            self.last_error = exc
            raise
        tokens = int(response.usage.get("total_tokens") or response.usage.get("completion_tokens") or 0)
        self.stats.tokens += tokens
        self.last_error = None
        return response

    def structured(
        self,
        role: str,
        messages: list[ChatMessage],
        schema_name: str,
        *,
        consume_budget: bool = True,
    ) -> tuple[dict[str, Any], ProviderResponse]:
        """Request JSON, then validate it strictly against a shared schema."""
        response = self.complete(role, messages, json_mode=True, consume_budget=consume_budget)
        payload = extract_json_object(response.text)
        if payload is None:
            self.stats.rejected_outputs += 1
            raise ProviderError(
                "malformed",
                "The model did not return a JSON object; its output was rejected",
            )
        errors = validation.validate_against_bool(schema_name, payload)
        if errors:
            self.stats.rejected_outputs += 1
            raise ProviderError(
                "malformed",
                "The model returned structurally invalid output and it was rejected: " + "; ".join(errors[:4]),
            )
        return payload, response


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json_object(text: str) -> dict[str, Any] | None:
    """Extract the first JSON object from model text (tolerates code fences)."""
    if not text:
        return None
    candidates: list[str] = []
    fenced = _JSON_FENCE_RE.search(text)
    if fenced:
        candidates.append(fenced.group(1).strip())
    candidates.append(text.strip())
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None
