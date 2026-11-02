"""Minimal JSON-Schema validator (subset) used for all external input.

GuidePilot validates every piece of data that crosses a trust boundary:

* model output (task plans, grounding results, verification claims)
* workflow imports
* HTTP request bodies
* settings patches (see :mod:`guidepilot.config` for section rules)

The subset implemented here covers the keywords used by the schemas in
``shared/schemas`` — type, properties, required, additionalProperties, enum,
items, minItems/maxItems, minimum/maximum, pattern, oneOf, anyOf, const and
local ``$ref`` — which is enough to be strict without pulling in a dependency.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

SCHEMA_DIR = Path(__file__).resolve().parents[2] / "shared" / "schemas"


class ValidationError(ValueError):
    """Raised when data does not satisfy a schema."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors) if errors else "validation failed")


_TYPE_MAP = {
    "object": dict,
    "array": list,
    "string": str,
    "boolean": bool,
    "null": type(None),
}


def _is_type(value: Any, expected: str) -> bool:
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "any":
        return True
    py_type = _TYPE_MAP.get(expected)
    if py_type is None:
        return True
    if expected == "boolean":
        return isinstance(value, bool)
    return isinstance(value, py_type) and not (expected in {"object"} and isinstance(value, bool))


def _resolve_ref(root: dict[str, Any], ref: str) -> dict[str, Any]:
    if not ref.startswith("#/"):
        raise ValidationError([f"unsupported $ref: {ref}"])
    node: Any = root
    for part in ref[2:].split("/"):
        if not isinstance(node, dict) or part not in node:
            raise ValidationError([f"unresolvable $ref: {ref}"])
        node = node[part]
    if not isinstance(node, dict):
        raise ValidationError([f"$ref does not point at a schema: {ref}"])
    return node


def validate(
    value: Any,
    schema: dict[str, Any],
    path: str = "$",
    root: dict[str, Any] | None = None,
    errors: list[str] | None = None,
    depth: int = 0,
) -> list[str]:
    """Return a list of human-readable validation errors (empty when valid)."""
    errors = errors if errors is not None else []
    root = root if root is not None else schema
    if depth > 40:
        errors.append(f"{path}: schema nesting too deep")
        return errors
    if not isinstance(schema, dict):
        errors.append(f"{path}: invalid schema node")
        return errors

    if "$ref" in schema:
        return validate(value, _resolve_ref(root, schema["$ref"]), path, root, errors, depth + 1)

    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: must equal {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: must be one of {schema['enum']}")
    if "type" in schema:
        expected_types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_is_type(value, t) for t in expected_types):
            errors.append(f"{path}: expected {'/'.join(expected_types)}, got {type(value).__name__}")
            return errors

    if isinstance(value, str):
        min_len = schema.get("minLength")
        max_len = schema.get("maxLength")
        if min_len is not None and len(value) < int(min_len):
            errors.append(f"{path}: shorter than {min_len} characters")
        if max_len is not None and len(value) > int(max_len):
            errors.append(f"{path}: longer than {max_len} characters")
        pattern = schema.get("pattern")
        if pattern and not re.search(pattern, value):
            errors.append(f"{path}: does not match {pattern}")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: must be >= {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: must be <= {schema['maximum']}")

    if isinstance(value, list):
        if "minItems" in schema and len(value) < int(schema["minItems"]):
            errors.append(f"{path}: needs at least {schema['minItems']} items")
        if "maxItems" in schema and len(value) > int(schema["maxItems"]):
            errors.append(f"{path}: allows at most {schema['maxItems']} items")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                validate(item, item_schema, f"{path}[{index}]", root, errors, depth + 1)

    if isinstance(value, dict):
        required = schema.get("required") or []
        for key in required:
            if key not in value:
                errors.append(f"{path}: missing required property '{key}'")
        properties = schema.get("properties") or {}
        for key, sub_schema in properties.items():
            if key in value:
                validate(value[key], sub_schema, f"{path}.{key}", root, errors, depth + 1)
        additional = schema.get("additionalProperties", True)
        if additional is False:
            for key in value:
                if key not in properties:
                    errors.append(f"{path}: unexpected property '{key}'")
        elif isinstance(additional, dict):
            for key, item in value.items():
                if key not in properties:
                    validate(item, additional, f"{path}.{key}", root, errors, depth + 1)

    for sub in schema.get("allOf", []) or []:
        validate(value, sub, path, root, errors, depth + 1)
    if "anyOf" in schema:
        if not any(not validate(value, sub, path, root, [], depth + 1) for sub in schema["anyOf"]):
            errors.append(f"{path}: does not match any allowed shape")
    if "oneOf" in schema:
        matches = sum(1 for sub in schema["oneOf"] if not validate(value, sub, path, root, [], depth + 1))
        if matches != 1:
            errors.append(f"{path}: must match exactly one allowed shape ({matches} matched)")
    return errors


_SCHEMA_CACHE: dict[str, dict[str, Any]] = {}


def load_schema(name: str) -> dict[str, Any]:
    """Load a schema from ``shared/schemas/<name>.schema.json``."""
    if name in _SCHEMA_CACHE:
        return _SCHEMA_CACHE[name]
    path = SCHEMA_DIR / f"{name}.schema.json"
    if not path.exists():
        raise ValidationError([f"schema not found: {name}"])
    with open(path, "r", encoding="utf-8") as handle:
        schema = json.load(handle)
    _SCHEMA_CACHE[name] = schema
    return schema


def validate_against(name: str, value: Any) -> None:
    """Validate ``value`` against a named schema, raising on failure."""
    errors = validate(value, load_schema(name))
    if errors:
        raise ValidationError(errors)


def validate_against_bool(name: str, value: Any, path: str = "$") -> list[str]:
    return validate(value, load_schema(name), path)
