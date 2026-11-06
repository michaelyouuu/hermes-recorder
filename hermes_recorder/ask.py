"""Answer a question from the recorder buffer with one ctx.llm call (used by the recorder panel).

This is a quick recorder Q&A, not an agent turn: the model sees only recent transcript lines and the question.
The lines reach the user's chosen Hermes model, the same path as the agent calling screen_recall.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from . import runtime
from .tools import recall_lines

log = logging.getLogger(__name__)

MAX_QUESTION = 1000
SYSTEM = (
    "You answer questions about what the user just heard or said on their Mac, using only the transcript lines "
    "given. Each line is '[time ago] source: text'. The source is either an app (audio the app played: a video, a "
    "podcast, the other people on a call) or 'you (microphone)' (the user speaking). Refer to sources naturally "
    "(\"the video\", \"the speaker\", \"on the call\", \"you said\"), never as 'them' or 'me'. Answer briefly and "
    "directly, quote short phrases when useful, and mention when something was said (for example 'about 2 min "
    "ago'). If the lines don't contain the answer, say so in one sentence. Transcripts are automatic and may have "
    "small errors."
)


def _source(ln: dict[str, Any]) -> str:
    return "you (microphone)" if ln.get("speaker") == "me" else (ln.get("app") or "app audio")


def _format(lines: list[dict[str, Any]]) -> str:
    return "\n".join(f"[{ln['t']}] {_source(ln)}: {ln['text']}" for ln in lines)


def answer(question: str, minutes: float = 5, rec: Any = None, llm: Any = None) -> dict[str, Any]:
    """Never raises. Returns {"answer", "lines", "seconds"} or {"error"}."""
    t0 = time.time()
    question = " ".join(str(question or "").split())[:MAX_QUESTION]
    if not question:
        return {"error": "Type a question first."}
    try:
        rec = rec or runtime.get()
        max_min = rec.buffer.max_age_s / 60
        minutes = min(max(float(minutes or 5), 0.5), max_min)
        lines, truncated = recall_lines(rec.buffer.search("", minutes))
        if not lines:
            return {"answer": f"Nothing was captured in the last {minutes:g} min.", "lines": 0, "seconds": 0}
        llm = llm or getattr(runtime._ctx, "llm", None)
        if llm is None:
            return {"error": "Hermes didn't give the plugin model access (ctx.llm is unavailable)."}
        note = " (older lines were cut to fit)" if truncated else ""
        user = (f"Transcript of the last {minutes:g} min{note}:\n{_format(lines)}\n\nQuestion: {question}")
        res = runtime.run_in_turn_context(
            llm.complete, messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
            max_tokens=500, purpose="hermes-recorder: panel ask")
        text = str(getattr(res, "text", "") or "").strip()
        secs = round(time.time() - t0, 1)
        log.info("hermes-recorder: panel ask ok in %ss (%d lines)", secs, len(lines))
        return {"answer": text or "(the model returned no text)", "lines": len(lines), "seconds": secs}
    except Exception as e:
        log.warning("hermes-recorder: panel ask failed: %s", e)
        if type(e).__name__ == "UnscopedSecretError" and not runtime.has_turn_context():
            return {"error": "Hermes restarted and hasn't seen a message yet, so the recorder can't reach your "
                             "model. Send Hermes any message, then ask again."}
        return {"error": f"{type(e).__name__}: {e}"}
