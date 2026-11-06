"""Fit a long transcript window into a character budget without silently dropping its start.

If every line fits, the lines are returned as they are. Otherwise the newest lines stay verbatim (half the budget)
and older lines are summarized in chunks of about five minutes with one short ``ctx.llm`` call each, so a question
about "the last 30 minutes" sees all 30. Chunk summaries are cached in RAM (cleared by ``forget``), so repeat
questions cost nothing. The lines reach the same model they would have reached anyway; nothing new leaves the Mac.
"""
from __future__ import annotations

import contextvars
import hashlib
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from . import runtime

log = logging.getLogger(__name__)

CHUNK_S = 300          # summarize older lines in chunks of about this many seconds
MAX_CHUNKS = 12        # longer windows get longer chunks
LINE_OVERHEAD = 40     # same per-line allowance as tools.recall_lines
CACHE_MAX = 256
WORKERS = 4
SUMMARY_TOKENS = 220
SUMMARY_PROMPT = (
    "Summarize this excerpt of an automatic transcript in two to four plain sentences. Keep names, numbers, "
    "definitions, decisions and anything someone might ask about later. Quietly fix obvious mishearings. "
    "Don't add anything that isn't there, and don't mention the transcript itself.")

_cache: dict[str, str] = {}
_cache_lock = threading.Lock()

Runner = Callable[..., Any]  # runner(fn, *args, **kwargs) -> fn's result


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _size(e: Any) -> int:
    return len(e.text) + LINE_OVERHEAD


def _ago(ts: float, now: float) -> str:
    s = max(0, int(round(now - ts)))
    return f"-{s // 60:02d}:{s % 60:02d}"


def _key(chunk: list) -> str:
    h = hashlib.sha1()
    for e in chunk:
        h.update(f"{e.ts:.3f}|{e.speaker}|{e.text}\n".encode())
    return h.hexdigest()


def _chunks(entries: list, span_s: float) -> list[list]:
    size = max(CHUNK_S, span_s / MAX_CHUNKS)
    out: list[list] = []
    for e in entries:
        if out and e.ts - out[-1][0].ts < size:
            out[-1].append(e)
        else:
            out.append([e])
    return out


def _source(e: Any) -> str:
    return "you (microphone)" if e.speaker == "me" else runtime.app_name(e.app)


def current_context_runner() -> Runner:
    """Run calls in a copy of the caller's context (a tool call already runs inside its turn)."""
    snap = contextvars.copy_context()
    return lambda fn, *a, **kw: snap.copy().run(fn, *a, **kw)


def _summarize(chunk: list, llm: Any, runner: Runner) -> str:
    key = _key(chunk)
    with _cache_lock:
        if key in _cache:
            return _cache[key]
    text = "\n".join(f"{_source(e)}: {e.text}" for e in chunk)
    res = runner(llm.complete, messages=[{"role": "system", "content": SUMMARY_PROMPT},
                                         {"role": "user", "content": text}],
                 max_tokens=SUMMARY_TOKENS, timeout=60, purpose="hermes-recorder: summarize older transcript")
    summary = " ".join(str(getattr(res, "text", "") or "").split())
    if not summary:
        raise RuntimeError("the model returned an empty summary")
    with _cache_lock:
        if len(_cache) >= CACHE_MAX:
            _cache.pop(next(iter(_cache)))
        _cache[key] = summary
    return summary


def build(entries: list, budget: int, now: float, llm: Any = None,
          runner: Runner | None = None) -> tuple[list[dict[str, Any]], str | None]:
    """Recall lines covering all of ``entries`` within about ``budget`` characters, and a note (or None).

    Summary lines look like ``{"t": "-28:00 … -23:00", "app": "Zoom", "speaker": "summary", "text": ...}``.
    """
    from .tools import recall_lines
    if sum(_size(e) for e in entries) <= budget:
        return recall_lines(entries, now, cap=budget)[0], None
    recent, _ = recall_lines(entries, now, cap=budget // 2)
    older = entries[:len(entries) - len(recent)]
    covered = round((now - entries[len(older)].ts) / 60, 1) if recent else 0
    if llm is None:
        return recent, (f"Only the last {covered:g} min fit; older lines were left out "
                        "(the plugin has no model access to summarize them).")
    runner = runner or runtime.run_in_turn_context
    groups = _chunks(older, older[-1].ts - older[0].ts)
    try:
        with ThreadPoolExecutor(max_workers=min(WORKERS, len(groups))) as pool:
            summaries = list(pool.map(lambda g: _summarize(g, llm, runner), groups))
    except Exception as e:
        log.warning("hermes-recorder: could not summarize older lines: %s", e)
        return recent, (f"Only the last {covered:g} min are included; summarizing the older part failed "
                        f"({type(e).__name__}: {e}).")
    lines = []
    for g, s in zip(groups, summaries):
        apps = sorted({_source(e) for e in g})
        lines.append({"t": f"{_ago(g[0].ts, now)} … {_ago(g[-1].ts, now)}", "app": ", ".join(apps),
                      "speaker": "summary", "text": s})
    note = (f"The last {covered:g} min are verbatim; earlier parts are summarized "
            f"({len(groups)} summary line{'s' if len(groups) != 1 else ''}, speaker 'summary').")
    return lines + recent, note
