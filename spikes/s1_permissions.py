"""S1: permission preflight/request + proof-of-capture, and who TCC attributes it to.

Usage:
  python spikes/s1_permissions.py            # status only, no prompts
  python spikes/s1_permissions.py --request  # trigger all four prompts
  python spikes/s1_permissions.py --prove    # 3 s app audio + mic + event-tap proof
Writes a JSON line per run to spikes/out/s1_results.jsonl.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402
import Quartz  # noqa: E402


def prove_audio(seconds: float = 3.0) -> dict:
    got = {1: 0, 2: 0, -1: []}
    fmts = {}

    def h(kind, data, info):
        if kind == -1:
            got[-1].append(info)
            return
        got[kind] += len(data)
        fmts.setdefault(kind, info)

    try:
        cap = common.AudioCapture(None, h, mic=True)
        cap.start()
        common.pump(seconds)
        cap.stop()
    except Exception as e:
        return {"ok": False, "error": repr(e)}
    return {"ok": got[1] > 0 or got[2] > 0, "app_audio_bytes": got[1], "mic_bytes": got[2],
            "formats": {str(k): v for k, v in fmts.items()}, "errors": got[-1][:3]}


def prove_events(seconds: float = 3.0) -> dict:
    count = {"n": 0}

    def cb(proxy, etype, event, refcon):
        count["n"] += 1
        return event

    mask = (Quartz.CGEventMaskBit(Quartz.kCGEventLeftMouseDown) | Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown)
            | Quartz.CGEventMaskBit(Quartz.kCGEventMouseMoved))
    tap = Quartz.CGEventTapCreate(Quartz.kCGSessionEventTap, Quartz.kCGHeadInsertEventTap,
                                  Quartz.kCGEventTapOptionListenOnly, mask, cb, None)
    if tap is None:
        return {"ok": False, "error": "CGEventTapCreate returned None (Input Monitoring not granted?)"}
    src = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
    Quartz.CFRunLoopAddSource(Quartz.CFRunLoopGetCurrent(), src, Quartz.kCFRunLoopCommonModes)
    Quartz.CGEventTapEnable(tap, True)
    print(f"  (move the mouse / press keys for {seconds:.0f} s)", file=sys.stderr)
    common.pump(seconds)
    Quartz.CGEventTapEnable(tap, False)
    return {"ok": count["n"] > 0, "events": count["n"]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--request", action="store_true")
    ap.add_argument("--prove", action="store_true")
    ap.add_argument("--label", default="terminal")
    a = ap.parse_args()

    rec = {"ts": time.time(), "label": a.label, "macos": platform.mac_ver()[0],
           "python": sys.executable, "attribution": common.responsible_process(),
           "parent_pid": os.getppid(), "before": common.permission_status()}
    if a.request:
        common.request_all()
        common.pump(1.0)
        rec["after_request"] = common.permission_status()
    if a.prove:
        rec["prove_audio"] = prove_audio()
        rec["prove_events"] = prove_events()
    print(json.dumps(rec, indent=2))
    with open(os.path.join(common.OUT_DIR, "s1_results.jsonl"), "a") as f:
        f.write(json.dumps(rec) + "\n")


if __name__ == "__main__":
    main()
