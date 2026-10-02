"""Strip likely secrets from text before it is stored or shown to the model."""

import os
import re

_PATTERNS = [
    re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(r"\b(sk|pk|rk|ts|tsk)[-_][A-Za-z0-9_-]{16,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b([A-Z0-9_]*(api[_-]?key|secret|token|password|passwd)[A-Z0-9_]*)\s*[=:]\s*['\"]?[^\s'\"]{6,}"),
]


def redact(text: str) -> str:
    if not text:
        return text
    for value in _secret_env_values():
        text = text.replace(value, "[REDACTED]")
    for pattern in _PATTERNS:
        text = pattern.sub(lambda m: (m.group(1) + " [REDACTED]") if m.groups() and m.group(1) else "[REDACTED]", text)
    return text


def _secret_env_values() -> list[str]:
    names = ("KEY", "TOKEN", "SECRET", "PASSWORD")
    return [v for k, v in os.environ.items() if any(n in k.upper() for n in names) and v and len(v) >= 8]
