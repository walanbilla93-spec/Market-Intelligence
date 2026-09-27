from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from typing import Any


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime | None = None) -> str:
    value = value or utc_now()
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def stable_id(prefix: str, *parts: Any) -> str:
    return f"{prefix}_{sha256_text(canonical_json(parts))[:24]}"


_SECRET_TEXT = re.compile(
    r"(?i)((?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret)=)[^&\s\"']+|"
    r"(authorization\s*:\s*bearer\s+)[^\s\"']+"
)


def redact_sensitive_text(value: str) -> str:
    """Remove configured credentials and common credential-bearing text forms."""
    def replace(match: re.Match[str]) -> str:
        tail = match.group(0).split("=", 1)[-1].split()[-1].strip("[]")
        if tail.upper() == "REDACTED":
            return match.group(0)
        return (match.group(1) or match.group(2) or "") + "[REDACTED]"
    redacted = _SECRET_TEXT.sub(replace, value)
    for name in ("FRED_API_KEY", "GEMINI_API_KEY", "MI_EXPORT_TOKEN"):
        secret = os.getenv(name, "")
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    return redacted

