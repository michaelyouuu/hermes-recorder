"""S6 (throwaway): show the recorder panel with a fake gateway, to check the window and the JS bridge.

Run inside the helper app (same process type as the hotkey agent):
  open -n -g --env PYTHONHOME=... --env PYTHONPATH=<venv site-packages>:<repo> -a "Hermes Recorder Helper.app" \
      --args spikes/s6_panel.py --seconds 60 --log /tmp/s6.log
Checks: the panel floats over the front app without activating it, typing works, Esc hides, ask round-trips.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DEMO = [("them", "FaceTime", "So should we do Thursday at three for the review?"),
        ("me", None, "Thursday at three works for me, I'll send the invite."),
        ("them", "FaceTime", "Great. Bring the Q3 numbers and the hiring plan."),
        ("them", "FaceTime", "Oh and Priya wants the persimmon flavor for the launch party."),
        ("me", None, "Noted, persimmon it is.")]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=60)
    ap.add_argument("--log", required=True)
    a = ap.parse_args()
    logging.basicConfig(filename=a.log, level=logging.INFO, format="%(asctime)s %(message)s")
    log = logging.getLogger("hermes_recorder_helper")
    from AppKit import NSApplication, NSDefaultRunLoopMode, NSEventMaskAny
    from Foundation import NSDate
    from helper.panel import RecorderPanel

    t0 = time.time()
    state = {"s": "running"}

    def client(op, timeout=30, apps=None, fallback_apps=None, args=None):
        log.info("client %s %s", op, args)
        if op in ("pause", "resume", "on", "off"):
            state["s"] = {"pause": "paused", "resume": "running", "on": "running", "off": "stopped"}[op]
        if op == "ask":
            time.sleep(1.2)
            return {"answer": f"(fake) You asked: {args.get('question')!r}. Thursday at 3 PM.", "seconds": 1.2}
        n = min(len(DEMO), int((time.time() - t0) / 3) + 1)
        lines = [{"ts": t0 + i * 3, "speaker": s, "app": app or "", "text": txt} for i, (s, app, txt) in
                 enumerate(DEMO[:n])]
        return {"state": state["s"], "apps": ["FaceTime"], "error": None, "now": time.time(), "started_at": t0 - 65,
                "lines": lines}

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(1)
    app.finishLaunching()
    panel = RecorderPanel(client)
    panel.show()
    while time.time() - t0 < a.seconds:
        ev = app.nextEventMatchingMask_untilDate_inMode_dequeue_(
            NSEventMaskAny, NSDate.dateWithTimeIntervalSinceNow_(0.5), NSDefaultRunLoopMode, True)
        if ev is not None:
            app.sendEvent_(ev)
        time.sleep(0)
    log.info("done visible=%s", panel.visible)
    return 0


if __name__ == "__main__":
    sys.exit(main())
