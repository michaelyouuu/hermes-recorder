"""Slash commands: /recorder and /teach. Handlers take the raw argument string and return text."""
from __future__ import annotations

import json
import logging

from . import runtime, tools
from .helper_manager import LaunchError

log = logging.getLogger(__name__)

RECORDER_HELP = ("Usage: /recorder on [bundle ids…] | off | pause | resume | status | forget\n"
                 "Example: /recorder on com.microsoft.edgemac us.zoom.xos")


def _status_text(rec: runtime.Recorder) -> str:
    st = rec.status()
    apps = ", ".join(a["name"] for a in st["apps"]) or "none"
    lines = [f"Recorder: {st['state']} · apps: {apps} · buffer: {st['buffer']['minutes_available']} min "
             f"({st['buffer']['transcript_lines']} lines, {st['buffer']['ui_events']} events)"]
    perms = st["helper"].get("permissions")
    if perms:
        lines.append("Permissions: " + ", ".join(f"{k}={v}" for k, v in perms.items()))
    if st["error"]:
        lines.append(f"Error: {st['error']}")
    return "\n".join(lines)


def recorder(raw_args: str = "", **kwargs) -> str:
    try:
        parts = (raw_args or "").split()
        sub = parts[0].lower() if parts else "status"
        rec = runtime.get()
        if sub in ("on", "start"):
            rec.start(parts[1:] or None)
        elif sub in ("off", "stop"):
            rec.stop()
        elif sub == "pause":
            rec.pause()
        elif sub == "resume":
            rec.resume()
        elif sub == "forget":
            rec.forget()
            return "Recorder buffer cleared.\n" + _status_text(rec)
        elif sub != "status":
            return RECORDER_HELP
        return _status_text(rec)
    except LaunchError as e:
        return f"Recorder: {e}"
    except Exception as e:
        log.exception("hermes-recorder: /recorder failed")
        return f"Recorder error: {e}"


def teach(raw_args: str = "", **kwargs) -> str:
    try:
        minutes = float(raw_args.strip()) if raw_args and raw_args.strip() else 5.0
    except ValueError:
        return "Usage: /teach [minutes]"
    result = json.loads(tools.draft_skill({"minutes": minutes}))
    if "error" in result:
        return f"Teach: {result['error']}"
    return result.get("draft_md", "")
