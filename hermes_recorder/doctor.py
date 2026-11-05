"""`hermes recorder doctor`: what's working, what isn't, and the exact fix for each."""
from __future__ import annotations

import json
import platform
import subprocess
import time
from typing import Callable

from . import runtime, setup

PERMISSION_LABELS = {
    "screen_capture": ("Screen & System Audio Recording", "needed to hear app audio"),
    "microphone": ("Microphone", "needed for your narration (\"me\" lines)"),
    "accessibility": ("Accessibility", "teach mode only"),
    "input_monitoring": ("Input Monitoring", "teach mode only"),
}
SETTINGS = {
    "screen_capture": "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture",
    "microphone": "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone",
    "accessibility": "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
    "input_monitoring": "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent",
}


def _granted(key: str, value) -> bool:
    return value == "authorized" if key == "microphone" else bool(value)


def check(paths: setup.Paths, run_preflight: Callable[..., dict] = setup.run_preflight,
          needs_events: bool = False) -> list[dict]:
    """Return a list of {item, ok, detail, fix} rows. Never raises."""
    rows: list[dict] = []

    def row(item: str, ok: bool, detail: str = "", fix: str = "", required: bool = True) -> None:
        rows.append({"item": item, "ok": ok, "detail": detail, "fix": fix, "required": required})

    probs = setup.platform_problems()
    row("Platform", not probs, f"macOS {platform.mac_ver()[0]} {platform.machine()}", "; ".join(probs))
    if probs:
        return rows
    ok, why = setup.build_is_current(paths, setup.host_python())
    row("Helper runtime", ok, why, "" if ok else "Run `hermes recorder setup`.")
    if not ok:
        return rows
    model = str(runtime.config("whisper_model") or "base.en")
    have_model = any(paths.models.glob(f"hub/models--Systran--faster-whisper-{model}"))
    row("Transcription model", have_model, model, "" if have_model else "Run `hermes recorder setup` to download it.")
    pf = run_preflight(paths, request=False, events=False)
    if "error" in pf:
        row("Helper app launch", False, pf["error"], "Re-run `hermes recorder setup`; check Console.app for "
            "'Hermes Recorder Helper' launch errors.")
        return rows
    row("Helper app launch", True, f"pid {pf.get('pid')}")
    perms = pf.get("permissions", {})
    for key, (label, why_needed) in PERMISSION_LABELS.items():
        teach = key in ("accessibility", "input_monitoring")
        granted = _granted(key, perms.get(key))
        fix = ("" if granted else
               f"System Settings > Privacy & Security > {label}: switch on 'Hermes Recorder Helper' "
               f"(add it with + from {paths.app} if it isn't listed). Open: {SETTINGS[key]}")
        row(f"Permission: {label}", granted, why_needed, fix, required=not teach or needs_events)
    if perms.get("screen_capture") and not pf.get("screen_capture_works"):
        row("Screen capture works", False, pf.get("screen_capture_error", ""),
            "macOS sometimes needs the helper relaunched after granting; run doctor again.")
    if not pf.get("mic_supported", True):
        row("Microphone capture", False, "needs macOS 15 or newer", "App audio still works.", required=False)
    st = runtime.data_dir() / "status.json"
    if st.exists():
        s = json.loads(st.read_text())
        age = time.time() - s.get("written_at", 0)
        row("Gateway capture state", s.get("state") != "error", f"{s.get('state')} ({age:.0f} s ago)",
            s.get("error") or "", required=False)
    return rows


def render(rows: list[dict]) -> tuple[str, int]:
    lines, bad = [], 0
    for r in rows:
        mark = "✓" if r["ok"] else ("✗" if r["required"] else "·")
        lines.append(f"{mark} {r['item']}: {r['detail']}".rstrip(": "))
        if not r["ok"] and r["fix"]:
            lines.append(f"    fix: {r['fix']}")
        if not r["ok"] and r["required"]:
            bad += 1
    lines.append("All required checks passed." if not bad else f"{bad} problem(s) to fix.")
    return "\n".join(lines), (1 if bad else 0)


def open_settings(key: str) -> None:
    subprocess.run(["/usr/bin/open", SETTINGS[key]], check=False, timeout=10)
