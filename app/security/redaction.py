"""PII redaction for anything written to logs (spec §18 'handle PII carefully').

Applied to log output, not to stored session state: the system of record keeps the original text
under database access control; logs are the wide-distribution surface.
"""

from __future__ import annotations

import re

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "<email>"),
    (re.compile(r"\b(?:\d[ -]?){13,19}\b"), "<card>"),
    (re.compile(r"(?<!\w)\+?\d[\d ()-]{7,}\d\b"), "<phone>"),
    (re.compile(r"(?i)\b(sk-[a-z0-9-]{8,}|bearer\s+[a-z0-9._-]{8,})"), "<secret>"),
]


def redact(text: str) -> str:
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


def redact_obj(value):
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: redact_obj(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [redact_obj(v) for v in value]
    return value
