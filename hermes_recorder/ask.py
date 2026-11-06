"""Answer a question from the recorder buffer with one ctx.llm call (used by the recorder panel).

This is a quick recorder Q&A, not an agent turn: the model sees only recent transcript lines and the question.
The lines reach the user's chosen Hermes model, the same path as the agent calling screen_recall.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any

from . import runtime
from .tools import recall_lines

log = logging.getLogger(__name__)

MAX_QUESTION = 1000
SYSTEM = (
    "You are a helpful assistant answering a quick question about something the user just heard or said on their "
    "Mac. You get an automatic transcript of the last few minutes and the question.\n\n"
    "Each transcript line is '[time ago] source: text'. The source is an app (whatever it played: a video, a "
    "podcast, the other people on a call) or 'you (microphone)' (the user).\n\n"
    "How to answer:\n"
    "- Reply in plain, natural sentences, the way a friend who was listening would. Usually one to three "
    "sentences; a short paragraph at most for summaries.\n"
    "- Plain text only. No markdown: no asterisks, bold, headings, bullet points or numbered lists (unless the "
    "user asks for a list).\n"
    "- Don't mention timestamps, seconds or how long ago something was said, unless the question is about when.\n"
    "- Refer to sources naturally (\"the video\", \"the speaker\", \"they said on the call\", \"you said\"), never "
    "as 'them', 'me' or 'microphone'.\n"
    "- The transcript may mishear words. Quietly fix obvious mistakes from context; don't comment on the "
    "transcript or its quality.\n"
    "- If the transcript doesn't cover the question, say so in one sentence."
)


_MD = [
    (re.compile(r"\*\*(.+?)\*\*|__(.+?)__", re.S), lambda m: m.group(1) or m.group(2)),
    (re.compile(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])"), lambda m: m.group(1)),
    (re.compile(r"`([^`]+)`"), lambda m: m.group(1)),
    (re.compile(r"^\s{0,3}#{1,6}\s*", re.M), lambda m: ""),
    (re.compile(r"^\s*[-*+]\s+", re.M), lambda m: "• "),
]


def plain(text: str) -> str:
    """Strip markdown the model may still emit; the panel shows plain text."""
    for rx, rep in _MD:
        text = rx.sub(rep, text)
    return re.sub(r"\n{3,}", "\n\n", text.replace("*", "")).strip()


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
            max_tokens=400, purpose="hermes-recorder: panel ask")
        text = plain(str(getattr(res, "text", "") or ""))
        secs = round(time.time() - t0, 1)
        log.info("hermes-recorder: panel ask ok in %ss (%d lines)", secs, len(lines))
        return {"answer": text or "(the model returned no text)", "lines": len(lines), "seconds": secs}
    except Exception as e:
        log.warning("hermes-recorder: panel ask failed: %s", e)
        if type(e).__name__ == "UnscopedSecretError" and not runtime.has_turn_context():
            return {"error": "Hermes restarted and hasn't seen a message yet, so the recorder can't reach your "
                             "model. Send Hermes any message, then ask again."}
        return {"error": f"{type(e).__name__}: {e}"}
