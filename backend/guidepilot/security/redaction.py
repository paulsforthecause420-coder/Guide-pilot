"""Secret detection and redaction.

Used before any text is sent to a model, written to logs, persisted, or echoed
back to the UI. This is defence in depth, not a guarantee: the product's primary
protection is that sensitive values are never captured from the screen in the
first place.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

# Field labels that indicate a value must never be displayed, stored or sent.
SENSITIVE_LABEL_TOKENS = (
    "password",
    "passwd",
    "passphrase",
    "secret",
    "token",
    "api key",
    "apikey",
    "access key",
    "private key",
    "client secret",
    "seed phrase",
    "recovery code",
    "one time code",
    "otp",
    "pin",
    "cvv",
    "credit card",
    "card number",
    "social security",
    "ssn",
    "routing number",
    "security answer",
)

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{16,}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b")),
    ("private_key_block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]+?-----END [A-Z ]*PRIVATE KEY-----")),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{16,}")),
    ("password_assignment", re.compile(r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key)\s*[=:]\s*[\"']?([^\s\"']{6,})")),
    ("connection_string", re.compile(r"(?i)\b(?:postgres|mysql|mongodb(?:\+srv)?|redis)://[^\s:@]+:[^\s@]{4,}@")),
]

_SAFE_PLACEHOLDERS = {"", "null", "none", "undefined", "changeme", "<your-key>", "..."}


def is_sensitive_label(label: str) -> bool:
    """Whether a form field label indicates a secret value.

    Underscores, dashes and camelCase are normalised first: config keys arrive
    as ``api_key``/``clientSecret`` while the token list is written in plain
    words, and a secret must not slip through because of punctuation.
    """
    raw = (label or "")
    normalized = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", raw).replace("_", " ").replace("-", " ").casefold()
    return any(token in normalized for token in SENSITIVE_LABEL_TOKENS)


def contains_secret(text: str) -> bool:
    if not text:
        return False
    return any(pattern.search(text) for _, pattern in _PATTERNS)


def redact(text: str, replacement: str = "[REDACTED]") -> str:
    """Mask any recognisable credential in ``text``."""
    if not text:
        return ""
    out = text
    for name, pattern in _PATTERNS:
        if name == "password_assignment":
            out = pattern.sub(lambda m: f"{m.group(1)}={replacement}", out)
        else:
            out = pattern.sub(f"{replacement}:{name}", out)
    return out


def sanitize_payload(value: Any, max_string: int = 4000) -> Any:
    """Recursively redact strings inside a JSON-like payload before transmission."""
    if isinstance(value, str):
        return redact(value[:max_string])
    if isinstance(value, dict):
        sanitized = {}
        for key, item in value.items():
            if isinstance(key, str) and is_sensitive_label(key) and isinstance(item, str) and item not in _SAFE_PLACEHOLDERS:
                sanitized[key] = "[REDACTED:sensitive_field]"
            else:
                sanitized[key] = sanitize_payload(item, max_string)
        return sanitized
    if isinstance(value, (list, tuple)):
        return [sanitize_payload(item, max_string) for item in value]
    return value


def redact_elements(elements: Iterable[Any]) -> list[dict[str, Any]]:
    """Element summaries safe to send to a model."""
    out: list[dict[str, Any]] = []
    for element in elements:
        label = getattr(element, "name", "") or ""
        value = getattr(element, "value", None)
        if value and is_sensitive_label(label):
            value = "[REDACTED:sensitive_field]"
        elif value:
            value = redact(str(value))[:120]
        rect = getattr(element, "rect", None)
        out.append(
            {
                "label": redact(label)[:120],
                "role": getattr(element, "role", ""),
                "value": value,
                "rect": rect.to_list() if rect is not None else None,
                "source": getattr(getattr(element, "source", None), "value", "unknown"),
            }
        )
    return out
