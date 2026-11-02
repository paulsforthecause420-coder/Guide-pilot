"""OpenAI-compatible chat-completions provider.

Works with any server that implements ``POST /chat/completions`` and accepts
``messages`` with ``image_url`` content parts — OpenAI, OpenRouter, LM Studio,
vLLM, llama.cpp's server, LocalAI, Groq and friends. Nothing is hardcoded to a
specific vendor endpoint; the base URL is user configuration.

No third-party HTTP library is used, so the app installs with zero Python
dependencies.
"""

from __future__ import annotations

import base64
import json
import socket
import time
import urllib.error
import urllib.request
from typing import Any

from .. import util
from .base import ChatMessage, ModelCapabilities, ProviderError, ProviderResponse

_VISION_MODEL_HINTS = ("vl", "vision", "llava", "bakllava", "moondream", "gemma3", "minicpm-v", "pixtral", "qwen2-vl", "qwen2.5-vl", "internvl", "phi-4-multimodal")


class OpenAICompatibleProvider:
    name = "openai_compat"

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        model: str = "",
        timeout_ms: int = 45000,
        max_retries: int = 2,
        max_context_chars: int = 24000,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.default_model = model
        self.timeout_s = max(1.0, timeout_ms / 1000.0)
        self.max_retries = max(0, max_retries)
        self.max_context_chars = max(1000, max_context_chars)

    # -- helpers -----------------------------------------------------------
    def _endpoint(self) -> str:
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        if self.base_url.endswith("/v1"):
            return f"{self.base_url}/chat/completions"
        return f"{self.base_url}/v1/chat/completions"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def capabilities(self, model: str | None = None) -> ModelCapabilities:
        chosen = (model or self.default_model or "").lower()
        vision = any(hint in chosen for hint in _VISION_MODEL_HINTS)
        notes: list[str] = []
        if not vision and chosen:
            notes.append(
                f"Model '{model or self.default_model}' is not recognised as vision-capable; "
                "visual grounding will rely on accessibility/OCR data."
            )
        return ModelCapabilities(vision=vision, json_output=True, context_chars=self.max_context_chars, notes=notes)

    # -- payload -----------------------------------------------------------
    def _encode_messages(self, messages: list[ChatMessage]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        budget = self.max_context_chars
        for message in messages:
            content: Any = util.truncated(message.content, max(500, budget))
            budget -= min(len(content), budget)
            if message.images:
                parts: list[dict[str, Any]] = [{"type": "text", "text": content}]
                for image in message.images[:2]:
                    encoded = base64.b64encode(image).decode("ascii")
                    parts.append(
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{message.image_media_type};base64,{encoded}"},
                        }
                    )
                out.append({"role": message.role, "content": parts})
            else:
                out.append({"role": message.role, "content": content})
        return out

    def chat(self, messages: list[ChatMessage], *, model: str | None = None, json_mode: bool = False) -> ProviderResponse:
        chosen_model = model or self.default_model
        if not chosen_model:
            raise ProviderError("unconfigured", "No model name is configured for the OpenAI-compatible provider")
        payload: dict[str, Any] = {
            "model": chosen_model,
            "messages": self._encode_messages(messages),
            "temperature": 0.1,
            "stream": False,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        data = json.dumps(payload).encode("utf-8")
        started = util.now_ms()
        last_error: ProviderError | None = None
        for attempt in range(self.max_retries + 1):
            request = urllib.request.Request(self._endpoint(), data=data, headers=self._headers(), method="POST")
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                    raw = response.read().decode("utf-8", "replace")
                parsed = json.loads(raw)
                return self._parse(parsed, chosen_model, util.now_ms() - started)
            except urllib.error.HTTPError as exc:
                body = ""
                try:
                    body = exc.read().decode("utf-8", "replace")[:500]
                except Exception:
                    pass
                kind = {
                    401: "auth",
                    403: "auth",
                    404: "unsupported",
                    413: "malformed",
                    422: "malformed",
                    429: "rate_limit",
                }.get(exc.code, "unavailable")
                if exc.code >= 500:
                    kind = "unavailable"
                last_error = ProviderError(
                    kind,
                    f"Provider returned HTTP {exc.code}: {util.truncated(body, 300)}",
                    retryable=exc.code in {429, 500, 502, 503, 504},
                    status=exc.code,
                )
            except urllib.error.URLError as exc:
                reason = str(getattr(exc, "reason", exc))
                last_error = ProviderError("network", util.truncated(reason, 300), retryable=True)
            except (socket.timeout, TimeoutError):
                last_error = ProviderError("timeout", f"Request exceeded {self.timeout_s:.0f}s", retryable=True)
            except json.JSONDecodeError as exc:
                last_error = ProviderError("malformed", f"Provider returned invalid JSON: {exc}", retryable=False)
            except ValueError as exc:
                last_error = ProviderError("malformed", str(exc))
            if last_error and last_error.retryable and attempt < self.max_retries:
                time.sleep(min(4.0, 0.5 * (2**attempt)))
                continue
            break
        raise last_error or ProviderError("unavailable", "Provider call failed")

    def _parse(self, payload: dict[str, Any], model: str, latency_ms: int) -> ProviderResponse:
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ProviderError("malformed", "Provider response contained no choices")
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        if not isinstance(message, dict):
            raise ProviderError("malformed", "Provider response choice had no message")
        content = message.get("content")
        if isinstance(content, list):
            text = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        elif isinstance(content, str):
            text = content
        else:
            raise ProviderError("malformed", "Provider response content was not text")
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        return ProviderResponse(
            text=text,
            model=str(payload.get("model") or model),
            provider=self.name,
            usage={k: int(v) for k, v in usage.items() if isinstance(v, (int, float))},
            latency_ms=latency_ms,
            finish_reason=str(choices[0].get("finish_reason") or ""),
        )


class OllamaProvider:
    """Local Ollama server adapter (``POST /api/chat``)."""

    name = "ollama"

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:11434",
        model: str = "",
        timeout_ms: int = 120000,
        max_retries: int = 1,
        max_context_chars: int = 16000,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.default_model = model
        self.timeout_s = max(1.0, timeout_ms / 1000.0)
        self.max_retries = max(0, max_retries)
        self.max_context_chars = max(1000, max_context_chars)

    def capabilities(self, model: str | None = None) -> ModelCapabilities:
        chosen = (model or self.default_model or "").lower()
        vision = any(hint in chosen for hint in _VISION_MODEL_HINTS)
        notes = [] if vision else [
            "Local models without vision do not provide visual grounding; GuidePilot uses accessibility and OCR instead."
        ]
        return ModelCapabilities(vision=vision, json_output=True, context_chars=self.max_context_chars, notes=notes)

    def chat(self, messages: list[ChatMessage], *, model: str | None = None, json_mode: bool = False) -> ProviderResponse:
        chosen_model = model or self.default_model
        if not chosen_model:
            raise ProviderError("unconfigured", "No Ollama model is configured")
        payload_messages: list[dict[str, Any]] = []
        for message in messages:
            entry: dict[str, Any] = {"role": message.role, "content": util.truncated(message.content, self.max_context_chars)}
            if message.images:
                entry["images"] = [base64.b64encode(image).decode("ascii") for image in message.images[:2]]
            payload_messages.append(entry)
        payload: dict[str, Any] = {
            "model": chosen_model,
            "messages": payload_messages,
            "stream": False,
            "options": {"temperature": 0.1},
        }
        if json_mode:
            payload["format"] = "json"
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/api/chat",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        started = util.now_ms()
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                raw = response.read().decode("utf-8", "replace")
            parsed = json.loads(raw)
        except urllib.error.HTTPError as exc:
            kind = "auth" if exc.code in {401, 403} else "unavailable"
            raise ProviderError(kind, f"Ollama returned HTTP {exc.code}", retryable=exc.code >= 500, status=exc.code)
        except urllib.error.URLError as exc:
            raise ProviderError(
                "unavailable",
                f"Ollama is not reachable at {self.base_url} ({util.truncated(str(getattr(exc, 'reason', exc)), 160)})",
                retryable=True,
            )
        except (socket.timeout, TimeoutError):
            raise ProviderError("timeout", f"Ollama exceeded {self.timeout_s:.0f}s", retryable=True)
        except json.JSONDecodeError as exc:
            raise ProviderError("malformed", f"Ollama returned invalid JSON: {exc}")
        message = parsed.get("message") if isinstance(parsed, dict) else None
        text = message.get("content") if isinstance(message, dict) else None
        if not isinstance(text, str):
            raise ProviderError("malformed", "Ollama response contained no message content")
        eval_count = int(parsed.get("eval_count") or 0)
        return ProviderResponse(
            text=text,
            model=str(parsed.get("model") or chosen_model),
            provider=self.name,
            usage={"completion_tokens": eval_count} if eval_count else {},
            latency_ms=util.now_ms() - started,
            finish_reason=str(parsed.get("done_reason") or ""),
        )


def build_provider(settings_section: dict[str, Any], api_key: str = "") -> Any:
    """Create the configured provider, or raise ``unconfigured``."""
    provider = (settings_section.get("provider") or "none").strip()
    if provider == "none":
        raise ProviderError("unconfigured", "No AI provider configured")
    if provider == "ollama":
        return OllamaProvider(
            base_url=settings_section.get("base_url") or "http://127.0.0.1:11434",
            model=settings_section.get("model_reasoning") or settings_section.get("model_vision") or "",
            timeout_ms=int(settings_section.get("timeout_ms") or 120000),
            max_retries=int(settings_section.get("max_retries") or 1),
            max_context_chars=int(settings_section.get("max_context_chars") or 16000),
        )
    if provider == "openai_compat":
        return OpenAICompatibleProvider(
            base_url=settings_section.get("base_url") or "https://api.openai.com",
            api_key=api_key,
            model=settings_section.get("model_reasoning") or settings_section.get("model_vision") or "",
            timeout_ms=int(settings_section.get("timeout_ms") or 45000),
            max_retries=int(settings_section.get("max_retries") or 2),
            max_context_chars=int(settings_section.get("max_context_chars") or 24000),
        )
    raise ProviderError("unsupported", f"Unknown provider '{provider}'")
