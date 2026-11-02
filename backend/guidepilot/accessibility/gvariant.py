"""Minimal GVariant text-format parser.

``gdbus call`` prints return values in GVariant text notation, for example::

    (<'App Name'>,)
    (['org.a11y.atspi.Component'],)
    ((':1.7', objectpath '/org/a11y/atspi/accessible/root'),)
    ((148, 357, 130, 22),)

Parsing that reliably matters: these values are element names and screen
coordinates. A tolerant hand-written parser avoids both a ``gdbus`` dependency
in Python and fragile regexes over user-controlled text (labels legitimately
contain quotes, parentheses and brackets).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

_TYPE_PREFIXES = {
    "objectpath",
    "uint32",
    "int32",
    "uint64",
    "int64",
    "uint16",
    "int16",
    "byte",
    "double",
    "handle",
    "signature",
    "uint8",
    "int8",
}


class GVariantError(ValueError):
    """Raised when the GVariant text cannot be parsed."""


@dataclass
class _Cursor:
    text: str
    index: int = 0

    def eof(self) -> bool:
        return self.index >= len(self.text)

    def peek(self) -> str:
        return self.text[self.index] if not self.eof() else ""

    def skip_ws(self) -> None:
        while not self.eof() and self.text[self.index].isspace():
            self.index += 1

    def take(self, count: int = 1) -> str:
        chunk = self.text[self.index : self.index + count]
        self.index += count
        return chunk


def _parse_string(cursor: _Cursor) -> str:
    if cursor.peek() != "'":
        raise GVariantError("expected a quoted string")
    cursor.take()
    out: list[str] = []
    while not cursor.eof():
        char = cursor.take()
        if char == "\\":
            nxt = cursor.take()
            out.append({"n": "\n", "t": "\t", "r": "\r", "0": "\0"}.get(nxt, nxt))
            continue
        if char == "'":
            return "".join(out)
        out.append(char)
    raise GVariantError("unterminated string")


def _parse_number(cursor: _Cursor) -> int | float:
    start = cursor.index
    while not cursor.eof() and (cursor.peek().isdigit() or cursor.peek() in "+-.eE"):
        cursor.index += 1
    raw = cursor.text[start : cursor.index]
    try:
        if any(c in raw for c in ".eE"):
            return float(raw)
        return int(raw)
    except ValueError as exc:  # pragma: no cover - defensive
        raise GVariantError(f"bad number {raw!r}") from exc


def _parse_identifier(cursor: _Cursor) -> Any:
    start = cursor.index
    while not cursor.eof() and (cursor.peek().isalnum() or cursor.peek() in "_-@"):
        cursor.index += 1
    word = cursor.text[start : cursor.index]
    if word in {"true", "false"}:
        return word == "true"
    if word == "nothing":
        return None
    if word in _TYPE_PREFIXES:
        cursor.skip_ws()
        return _parse_value(cursor)
    if word.startswith("@"):  # type annotation, value follows
        cursor.skip_ws()
        return _parse_value(cursor)
    if word:
        return word
    raise GVariantError(f"unexpected character {cursor.peek()!r}")


def _parse_value(cursor: _Cursor) -> Any:
    cursor.skip_ws()
    char = cursor.peek()
    if char == "(":
        cursor.take()
        items: list[Any] = []
        while True:
            cursor.skip_ws()
            if cursor.peek() == ")":
                cursor.take()
                return tuple(items)
            items.append(_parse_value(cursor))
            cursor.skip_ws()
            if cursor.peek() == ",":
                cursor.take()
                continue
            if cursor.peek() == ")":
                cursor.take()
                return tuple(items)
            raise GVariantError("expected ',' or ')' in tuple")
    if char == "[":
        cursor.take()
        items = []
        while True:
            cursor.skip_ws()
            if cursor.peek() == "]":
                cursor.take()
                return items
            items.append(_parse_value(cursor))
            cursor.skip_ws()
            if cursor.peek() == ",":
                cursor.take()
                continue
            if cursor.peek() == "]":
                cursor.take()
                return items
            raise GVariantError("expected ',' or ']' in array")
    if char == "{":
        cursor.take()
        items: list[tuple[Any, Any]] = []
        while True:
            cursor.skip_ws()
            if cursor.peek() == "}":
                cursor.take()
                return dict(items)
            key = _parse_value(cursor)
            cursor.skip_ws()
            if cursor.peek() != ":":
                raise GVariantError("expected ':' after dictionary key")
            cursor.take()
            value = _parse_value(cursor)
            items.append((key, value))
            cursor.skip_ws()
            if cursor.peek() == ",":
                cursor.take()
                continue
            if cursor.peek() == "}":
                cursor.take()
                return dict(items)
            raise GVariantError("expected ',' or '}' in dict")
    if char == "<":
        cursor.take()
        inner = _parse_value(cursor)
        cursor.skip_ws()
        if cursor.peek() == ">":
            cursor.take()
        return inner
    if char == "'":
        return _parse_string(cursor)
    if char and (char.isdigit() or char in "+-."):
        return _parse_number(cursor)
    return _parse_identifier(cursor)


def parse(text: str) -> Any:
    """Parse GVariant text into Python objects (tuples, lists, dicts, scalars)."""
    if text is None:
        raise GVariantError("no data")
    cursor = _Cursor(text.strip())
    if cursor.eof():
        raise GVariantError("empty variant text")
    value = _parse_value(cursor)
    return value


def parse_call_result(text: str) -> Any:
    """Parse a ``gdbus call`` result and unwrap the outer return tuple.

    ``gdbus`` always prints the method's return value as a tuple, so a single
    return value arrives as ``(value,)``.
    """
    parsed = parse(text)
    if isinstance(parsed, tuple) and len(parsed) == 1:
        return parsed[0]
    return parsed


def first_string(text: str) -> str | None:
    """Convenience: parse and return the first string, else None."""
    try:
        value = parse_call_result(text)
    except GVariantError:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, str):
                return item
    return None


def parse_children(text: str) -> list[tuple[str, str]]:
    """Parse ``GetChildren`` output into [(bus_name, object_path), ...]."""
    try:
        parsed = parse_call_result(text)
    except GVariantError:
        return []
    out: list[tuple[str, str]] = []
    if isinstance(parsed, (list, tuple)):
        for entry in parsed:
            if isinstance(entry, (list, tuple)) and len(entry) >= 2 and isinstance(entry[0], str):
                path = entry[1]
                if isinstance(path, str):
                    out.append((entry[0], path))
    return out


def parse_rect(text: str) -> list[int] | None:
    """Parse a ``Component.GetExtents`` response into [x, y, w, h]."""
    try:
        parsed = parse_call_result(text)
    except GVariantError:
        return None
    while isinstance(parsed, (list, tuple)) and parsed and isinstance(parsed[0], (list, tuple)):
        parsed = parsed[0]
    if isinstance(parsed, (list, tuple)) and len(parsed) == 4:
        try:
            return [int(v) for v in parsed]
        except (TypeError, ValueError):
            return None
    return None
