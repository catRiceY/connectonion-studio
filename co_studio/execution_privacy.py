"""Best-effort credential redaction for readable local history responses.

This is a projection, not a modification of stored history. It recognizes
common credential keys and text patterns without reading actual user secrets.
It is NOT a complete secret/PII detector or a replacement for access control:
unlabelled, encoded, unusual or split credentials and personal data may remain.
"""

from __future__ import annotations

import re

from pydantic import JsonValue


REDACTED = "[REDACTED]"

_SECRET_KEYS = {
    "apikey", "accesstoken", "refreshtoken", "token", "password", "passwd",
    "secret", "clientsecret", "authorization", "proxyauthorization", "cookie",
    "setcookie", "privatekey", "invitecode", "credential", "credentials",
}
_SECRET_SUFFIXES = (
    "apikey", "accesstoken", "refreshtoken", "clientsecret", "privatekey",
    "password", "passwd",
)


def _is_secret_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    return normalized in _SECRET_KEYS or normalized.endswith(_SECRET_SUFFIXES)


_PRIVATE_KEY = re.compile(
    r"-----BEGIN (?P<label>(?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY)-----"
    r".*?(?:-----END (?P=label)-----|\Z)",
    re.DOTALL,
)
# Keep the surrounding quote style and readable field label. Quoted values
# support escaped quotes; unquoted values stop at common JSON/env separators.
_VALUE = r'''(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|\[REDACTED\]|[^\s,;}\]"']+)'''
# Match only credential assignments. Consuming an arbitrary field such as
# message="password=..." first would otherwise hide a nested assignment.
_TEXT_SECRET_KEY = (
    r"(?:(?:[A-Za-z][A-Za-z0-9_-]*)?"
    r"(?:api[-_]?key|access[-_]?token|refresh[-_]?token|client[-_]?secret|"
    r"private[-_]?key|password|passwd)|"
    r"token|secret|authorization|proxy[-_]?authorization|cookie|set[-_]?cookie|"
    r"invite[-_]?code|credentials?)"
)
_ASSIGNMENT = re.compile(
    r'''(?P<prefix>(?<![\w-])(?P<quote>["']?)(?P<key>''' + _TEXT_SECRET_KEY + ")"
    r'''(?P=quote)[ \t]*[:=][ \t]*)(?P<value>''' + _VALUE + ")",
    re.IGNORECASE,
)
# Authorization can contain a scheme and value separated by whitespace;
# unlike generic assignments, the complete unquoted header value is masked.
_AUTHORIZATION = re.compile(
    r'''(?P<prefix>(?<![\w-])["']?(?:proxy[-_])?authorization["']?[ \t]*[:=][ \t]*)'''
    r'''(?P<value>"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|\[REDACTED\]|'''
    r'''(?:(?:Bearer|Basic)[ \t]+)?[^\s,;}\]"']+)''',
    re.IGNORECASE,
)
_BEARER = re.compile(r"(?P<prefix>\bBearer[ \t]+)[A-Za-z0-9._~+/-]{8,}=*", re.IGNORECASE)
_PROVIDER_KEY = re.compile(r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{8,}(?![A-Za-z0-9_-])")


def _masked_value(value: str) -> str:
    if value.startswith(('"', "'")) and value[-1:] == value[:1]:
        return value[0] + REDACTED + value[-1]
    return REDACTED


def redact_text(value: str) -> str:
    """Mask recognizable credentials, retaining ordinary readable text.

    An unterminated PEM private-key block is masked through the end of the
    string. Other unrecognized formats are not guaranteed to be sanitized.
    """
    value = _PRIVATE_KEY.sub(REDACTED, value)
    value = _AUTHORIZATION.sub(
        lambda match: match["prefix"] + _masked_value(match["value"]), value,
    )
    value = _ASSIGNMENT.sub(
        lambda match: match["prefix"] + _masked_value(match["value"]),
        value,
    )
    value = _BEARER.sub(lambda match: match["prefix"] + REDACTED, value)
    return _PROVIDER_KEY.sub(REDACTED, value)


def redact_value(value: JsonValue) -> JsonValue:
    """Return a recursively copied JSON value with credential fields masked.

    Keys and non-string scalar values are preserved, except that an entire
    value under a recognizable credential key is replaced by the marker.
    This does not mutate source records, infer credentials from ordinary token
    counts, or remove arbitrary conversation text.
    """
    if isinstance(value, dict):
        return {
            key: REDACTED if _is_secret_key(key) else redact_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value
