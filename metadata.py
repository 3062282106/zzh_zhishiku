"""Validate metadata and encode exact-match terms consistently."""

import json
import re


FIELD_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")


def normalize(values):
    if values is None:
        return {}
    if not isinstance(values, dict) or len(values) > 32:
        raise ValueError("Metadata must be an object with at most 32 fields.")
    result = {}
    for key, raw in values.items():
        if not FIELD_NAME.fullmatch(key):
            raise ValueError("Metadata names must use lowercase letters, digits and underscores.")
        items = raw if isinstance(raw, list) else [raw]
        if not 1 <= len(items) <= 20 or any(
            not isinstance(item, str) or not 1 <= len(item.strip()) <= 256 for item in items
        ):
            raise ValueError("Metadata values must be nonempty strings or lists of up to 20 strings.")
        result[key] = list(dict.fromkeys(item.strip() for item in items))
    return result


def term(key, value):
    return json.dumps([key, value], ensure_ascii=False, separators=(",", ":"))
