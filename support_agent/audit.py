"""Append-only JSONL audit trail of every tool call (PII redacted)."""

from __future__ import annotations

import json
import time
from pathlib import Path

from .safety import redact


class AuditLog:
    def __init__(self, path: Path | None):
        self.path = Path(path) if path else None

    def write(self, **event) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        event = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **event}
        with self.path.open("a") as f:
            f.write(redact(json.dumps(event, default=str)) + "\n")
