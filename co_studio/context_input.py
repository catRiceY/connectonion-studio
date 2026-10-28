"""Recognize explicit user task cards, never infer facts or grant permissions.

The iOS V2 markers describe a string format, not authenticated client identity.
Only the complete request body may contain a task card. Personalisation stays
in the original message and is never mined for protected task fields.
"""
from __future__ import annotations

import json

START = "<<<CONNECTONION_PERSONALISATION_V2>>>"
END = "<<<CONNECTONION_END_PERSONALISATION_V2>>>"
REQUEST = "<<<CONNECTONION_USER_REQUEST_V2>>>"


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate task-card key")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("Task cards require standard JSON")


def user_task_fields(content, allowed) -> tuple[dict, str | None]:
    """Return verbatim allowlisted fields and their *format*, not a trust label.

    Accept plain JSON or exactly one leading iOS V2 envelope with LF/CRLF
    separators. Do not scan prose, code fences, header JSON, nested wrappers or
    multimodal parts. Callers must restrict this to user-role messages.
    """
    if not isinstance(content, str):
        return {}, None
    request, source_format = content, "user_json"
    if content.startswith(START):
        if content.startswith(START + "\r\n"):
            newline = "\r\n"
        elif content.startswith(START + "\n"):
            newline = "\n"
        else:
            return {}, None
        start = len(START) + len(newline)
        separator = f"{newline}{END}{newline}{newline}{REQUEST}{newline}"
        end = content.find(separator, start)
        if end < 0:
            return {}, None
        header = content[start:end]
        if "<<<CONNECTONION_" in header:
            return {}, None
        request = content[end + len(separator):]
        source_format = "ios_v2_request_json"
    try:
        value = json.loads(request, object_pairs_hook=_unique_object,
                           parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        return {}, None
    body = value.get("studio_task") if isinstance(value, dict) else None
    if not isinstance(body, dict):
        return {}, None
    fields = {key: item for key, item in body.items() if key in allowed and
              (isinstance(item, str) or isinstance(item, list) and
               all(isinstance(part, str) for part in item))}
    return fields, source_format
