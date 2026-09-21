"""Strict normalization of a Claude judge's terminal JSON response."""

from __future__ import annotations

import json
import re
from typing import Any


_JSON_FENCE_START = re.compile(r"^```(?:json)?[ \t]*\r?\n", re.IGNORECASE)
_JSON_FENCE_END = re.compile(r"\r?\n?```[ \t]*$")


def parse_judgment_json(raw: object) -> dict[str, Any]:
    """Decode one JSON object, accepting one otherwise-content-free JSON fence."""
    if not isinstance(raw, str):
        raise ValueError("judge result is not text")
    text = raw.strip()
    # Claude occasionally emits the opening JSON fence but omits its closing
    # delimiter. The remaining text still has to be precisely one JSON object,
    # so accepting that presentation format does not admit prose or fragments.
    fenced = _JSON_FENCE_START.match(text)
    if fenced:
        text = text[fenced.end():]
        text = _JSON_FENCE_END.sub("", text).strip()
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("judge result must be a JSON object")
    return value
