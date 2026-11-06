"""Walk the user through macOS permissions: ask, open the right Settings page, wait until it's on.

Used by `hermes recorder setup`, `hermes recorder doctor` (when something is missing) and
`hermes recorder teach on`. Every check runs inside the helper app (its own permission identity) via
``setup.run_preflight``, so what we report is exactly what the helper will get.
"""
from __future__ import annotations

import subprocess
import sys
import time
from typing import Callable

from . import setup

BASIC = ("screen_capture", "microphone")
TEACH = ("accessibility", "input_monitoring")
LABELS = {
    "screen_capture": "Screen & System Audio Recording",
    "microphone": "Microphone",
    "accessibility": "Accessibility",
    "input_monitoring": "Input Monitoring",
}
SETTINGS = {
    "screen_capture": "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture",
    "microphone": "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone",
    "accessibility": "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
    "input_monitoring": "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent",
}
WAIT_S = 180
POLL_S = 3.0


def granted(key: str, perms: dict) -> bool:
    v = perms.get(key)
    return v == "authorized" if key == "microphone" else bool(v)


def missing(perms: dict, keys: tuple[str, ...]) -> list[str]:
    return [k for k in keys if not granted(k, perms)]


def interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def ensure(paths: setup.Paths, keys: tuple[str, ...], preflight: Callable[..., dict] = setup.run_preflight,
           open_url: Callable[[str], None] | None = None, out: Callable[[str], None] = print,
           sleep: Callable[[float], None] = time.sleep, wait_s: float = WAIT_S) -> bool:
    """Get every permission in ``keys`` switched on, one at a time. Returns True when all are granted."""
    open_url = open_url or (lambda url: subprocess.run(["/usr/bin/open", url], check=False, timeout=10))
    events = any(k in TEACH for k in keys)
    pf = preflight(paths, request=True, events=events)  # the prompts, and it lists the app in Settings
    if "error" in pf:
        out(f"Couldn't start the helper app: {pf['error']}")
        return False
    todo = missing(pf.get("permissions", {}), keys)
    for key in todo:
        label = LABELS[key]
        out(f"→ Turn on “Hermes Recorder Helper” in {label}. Opening System Settings…")
        open_url(SETTINGS[key])
        end = time.monotonic() + wait_s
        ok = False
        while time.monotonic() < end:
            sleep(POLL_S)
            perms = preflight(paths, request=False, events=False).get("permissions", {})
            if granted(key, perms):
                ok = True
                break
        if ok:
            out(f"  ✓ {label}")
        else:
            out(f"  Still off. If “Hermes Recorder Helper” isn't in the list, click + and choose\n"
                f"  {paths.app}\n  Then run `hermes recorder doctor`.")
            return False
    return True
