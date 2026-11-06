"""Tool handlers. Each returns a JSON string and never raises."""
from __future__ import annotations

import functools
import json
import logging
import time
from typing import Any, Callable

from . import runtime
from .helper_manager import LaunchError

log = logging.getLogger(__name__)
RECALL_CHAR_CAP = 12000  # about 12 min of speech verbatim; longer windows are digested


def _safe(fn: Callable[..., dict]) -> Callable[..., str]:
    @functools.wraps(fn)
    def wrapper(args: dict | None = None, **kwargs: Any) -> str:
        try:
            return json.dumps(fn(args if isinstance(args, dict) else {}), ensure_ascii=False)
        except LaunchError as e:
            return json.dumps({"error": str(e)})
        except Exception as e:  # never raise into Hermes
            log.exception("hermes-recorder: %s failed", fn.__name__)
            return json.dumps({"error": f"{type(e).__name__}: {e}"})
    return wrapper


def _ago(ts: float, now: float) -> str:
    s = max(0, int(round(now - ts)))
    return f"-{s // 60:02d}:{s % 60:02d}"


def _num(v: Any, default: float, lo: float, hi: float) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    return min(max(x, lo), hi)


def recall_lines(entries: list, now: float | None = None,
                 cap: int = RECALL_CHAR_CAP) -> tuple[list[dict[str, Any]], bool]:
    """Transcript entries as recall lines, keeping the newest ones within ``cap`` characters."""
    now = time.time() if now is None else now
    lines, used, truncated = [], 0, False
    for e in reversed(entries):
        size = len(e.text) + 40
        if used + size > cap:
            truncated = True
            break
        lines.append({"t": _ago(e.ts, now), "app": runtime.app_name(e.app), "speaker": e.speaker, "text": e.text})
        used += size
    lines.reverse()
    return lines, truncated


@_safe
def screen_recall(args: dict) -> dict:
    rec = runtime.get()
    rec.maybe_autostart()
    max_min = rec.buffer.max_age_s / 60
    minutes = _num(args.get("minutes"), 5.0, 0.1, max_min)
    speaker = args.get("speaker") if args.get("speaker") in ("me", "them", "any") else "any"
    app = args.get("app") or None
    query = str(args.get("query") or "")
    entries = rec.buffer.search(query, minutes, app=app, speaker=speaker)
    fallback = False
    if query and not entries:
        # Keyword search misses paraphrased questions ("what time did we agree on" vs "3 PM Thursday"),
        # so return the whole window rather than an empty answer.
        entries = rec.buffer.search("", minutes, app=app, speaker=speaker)
        fallback = bool(entries)
    from . import digest
    lines, digest_note = digest.build(entries, RECALL_CHAR_CAP, time.time(), llm=runtime.llm(),
                                      runner=digest.current_context_runner())
    out: dict[str, Any] = {"lines": lines, "truncated": digest_note is not None, "minutes": round(minutes, 1)}
    notes = []
    if fallback:
        notes.append(f"No line contained the words in '{query}', so these are all lines from the last {minutes:g} min.")
    if digest_note:
        notes.append(digest_note)
    if notes:
        out["note"] = " ".join(notes)
    if not lines:
        state = rec.manager.state
        if state in ("stopped", "error"):
            out["note"] = ("Capture is off, so there is nothing to recall. Ask the user if they want to start it "
                           "(recorder_control op=start with the app they are using).")
            if rec.manager.status()["error"]:
                out["note"] += f" Last error: {rec.manager.status()['error']}"
        else:
            out["note"] = f"Nothing was captured in the last {minutes:g} min."
    return out


@_safe
def recorder_status(args: dict) -> dict:
    rec = runtime.get()
    rec.maybe_autostart()
    return rec.status()


@_safe
def recorder_control(args: dict) -> dict:
    rec = runtime.get()
    op = args.get("op")
    apps = args.get("apps")
    if apps is not None and not (isinstance(apps, list) and all(isinstance(a, str) for a in apps)):
        return {"error": "apps must be a list of bundle id strings"}
    if op == "start":
        rec.start(apps)
    elif op == "stop":
        rec.stop()
    elif op == "pause":
        rec.pause()
    elif op == "resume":
        rec.resume()
    elif op == "set_apps":
        if not apps:
            return {"error": "set_apps needs apps"}
        rec.set_apps(apps)
    else:
        return {"error": f"unknown op {op!r}; use start, stop, pause, resume or set_apps"}
    return rec.status()


@_safe
def save_notes(args: dict) -> dict:
    from . import digest, notes
    hint = args.get("title_hint")
    return notes.save(args.get("minutes"), str(hint) if hint else None, runner=digest.current_context_runner())


@_safe
def recorder_notes(args: dict) -> dict:
    from . import notes
    if args.get("path"):
        return notes.read(str(args["path"]))
    return notes.search(str(args.get("query") or ""), args.get("days") or 30)


@_safe
def draft_skill(args: dict) -> dict:
    return {"error": "Teach mode isn't available yet in this version of hermes-recorder (coming in M3)."}
