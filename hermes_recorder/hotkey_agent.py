"""Install/remove the opt-in global hotkey login item (a LaunchAgent running the helper app's hotkey mode).

The agent only listens for one key combination (no permissions needed) and asks the gateway to toggle
capture. It never captures anything itself.
"""
from __future__ import annotations

import json
import os
import plistlib
import subprocess
import time
from pathlib import Path
from typing import Callable

from . import runtime, setup
from .hotkeys import parse_combo, pretty  # noqa: F401  (pretty re-exported)

LABEL = "io.github.michaelyouuu.hermes-recorder.hotkey"
DEFAULT_COMBO = "ctrl+opt+r"
BOOTSTRAP_TRIES = 10
Run = Callable[..., subprocess.CompletedProcess]


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _domain() -> str:
    return f"gui/{os.getuid()}"


def plist_bytes(paths: setup.Paths, combo: str) -> bytes:
    exe = paths.app / "Contents" / "MacOS" / setup.HELPER_EXE
    return plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": [str(exe), str(runtime.PLUGIN_ROOT / "helper" / "main.py"),
                             "--hotkey", combo, "--data-dir", str(paths.data),
                             "--log", str(paths.logs / "hotkey.log")],
        "EnvironmentVariables": setup.helper_env(paths),
        "RunAtLoad": True,
        "KeepAlive": True,  # macOS quits it when the helper's permissions change; always come back
        "ThrottleInterval": 5,
        "ProcessType": "Interactive",
        "LimitLoadToSessionType": "Aqua",
    })


def install(paths: setup.Paths, combo: str = DEFAULT_COMBO, run: Run = subprocess.run) -> Path:
    parse_combo(combo)  # validate before touching launchd
    p = plist_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    uninstall(run=run, keep_file=True)
    p.write_bytes(plist_bytes(paths, combo))
    for _ in range(BOOTSTRAP_TRIES):  # bootout finishes asynchronously; bootstrap fails (EIO) until it has
        r = run(["/bin/launchctl", "bootstrap", _domain(), str(p)], check=False, capture_output=True, text=True)
        if getattr(r, "returncode", 0) == 0:
            break
        time.sleep(0.3)
    return p


def uninstall(run: Run = subprocess.run, keep_file: bool = False) -> None:
    run(["/bin/launchctl", "bootout", f"{_domain()}/{LABEL}"], check=False, capture_output=True, text=True)
    if not keep_file:
        plist_path().unlink(missing_ok=True)


def status(run: Run = subprocess.run) -> dict:
    p = plist_path()
    r = run(["/bin/launchctl", "print", f"{_domain()}/{LABEL}"], check=False, capture_output=True, text=True)
    running = r.returncode == 0 and "state = running" in (r.stdout or "")
    combo = None
    if p.exists():
        try:
            args = plistlib.loads(p.read_bytes())["ProgramArguments"]
            combo = args[args.index("--hotkey") + 1]
        except Exception:
            pass
    out = {"installed": p.exists(), "running": running, "combo": combo, "registered": None, "error": None,
           "outdated": False}
    if p.exists():
        try:
            out["outdated"] = plistlib.loads(p.read_bytes()).get("KeepAlive") is not True
        except Exception:
            pass
    try:
        hs = json.loads((runtime.data_dir() / "hotkey.json").read_text())
        out["registered"], out["error"] = hs.get("registered"), hs.get("error")
    except (OSError, ValueError):
        pass
    return out

