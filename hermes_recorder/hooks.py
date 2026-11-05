"""Hooks. pre_llm_call reads in-memory state only (budget: under 5 ms)."""
from __future__ import annotations

import logging
from typing import Any

from . import runtime

log = logging.getLogger(__name__)


def pre_llm_call(**kwargs: Any) -> dict | None:
    try:
        rec = runtime.peek()
        if rec is None:
            return None
        note = rec.live_note()
        return {"context": note} if note else None
    except Exception as e:
        log.debug("hermes-recorder: pre_llm_call failed: %s", e)
        return None


def on_session_end(**kwargs: Any) -> None:
    # Recap notes for pinned clips arrive with saved clips; nothing to do yet.
    return None
