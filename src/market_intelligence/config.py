from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Config:
    data_dir: Path
    config_path: Path
    timezone: str
    poll_seconds: int
    queue_capacity: int
    http_timeout: float
    max_response_bytes: int
    raw: dict[str, Any]

    @classmethod
    def load(cls) -> "Config":
        path = Path(os.getenv("MI_CONFIG", "config.json"))
        raw = json.loads(path.read_text("utf-8")) if path.exists() else {}
        return cls(
            data_dir=Path(os.getenv("MI_DATA_DIR", "./data")).resolve(),
            config_path=path.resolve(),
            timezone=str(raw.get("timezone", "America/New_York")),
            poll_seconds=max(60, int(os.getenv("MI_POLL_SECONDS", "900"))),
            queue_capacity=max(8, int(os.getenv("MI_QUEUE_CAPACITY", "128"))),
            http_timeout=max(2.0, float(os.getenv("MI_HTTP_TIMEOUT_SECONDS", "12"))),
            max_response_bytes=max(65536, int(os.getenv("MI_MAX_RESPONSE_BYTES", "4000000"))),
            raw=raw,
        )

