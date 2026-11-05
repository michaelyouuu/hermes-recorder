"""JSON-lines logging with rotation. Never log transcript text."""
from __future__ import annotations

import json
import logging
import logging.handlers
import time
from pathlib import Path


class _Json(logging.Formatter):
    def format(self, r: logging.LogRecord) -> str:
        out = {"ts": round(time.time(), 3), "level": r.levelname, "msg": r.getMessage()}
        if r.exc_info:
            out["exc"] = self.formatException(r.exc_info)
        return json.dumps(out)


def setup(path: str | None) -> logging.Logger:
    log = logging.getLogger("hermes_recorder_helper")
    log.setLevel(logging.INFO)
    if path:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        h = logging.handlers.RotatingFileHandler(path, maxBytes=1_000_000, backupCount=5)
        h.setFormatter(_Json())
        log.addHandler(h)
    return log
