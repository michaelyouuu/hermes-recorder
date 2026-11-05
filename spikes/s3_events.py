"""S3: listen-only event tap + Accessibility -> normalized UI steps (JSONL).

Clicks resolve the element under the pointer (role/subrole/title/description, app, window).
Key-downs are coalesced per focused element into one "typed" step, whose value is read from the
field's AXValue at flush time (so backspaces/autocorrect are reflected). Secure fields never have
their value read: they yield "{SECRET}". Values matching card/OTP/API-key patterns are masked.

Usage:
  python spikes/s3_events.py --seconds 120 [--out spikes/out/s3_notes.jsonl]
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402
import Quartz  # noqa: E402
import HIServices as AX  # noqa: E402
from AppKit import NSWorkspace, NSRunningApplication  # noqa: E402

SYS = AX.AXUIElementCreateSystemWide()
AX.AXUIElementSetMessagingTimeout(SYS, 0.25)

MASKS = [
    (re.compile(r"\b(?:\d[ -]?){13,19}\b"), "{CARD}"),
    (re.compile(r"\b(sk|pk|rk|ghp|gho|xox[abp]|AKIA)[-_A-Za-z0-9]{12,}\b"), "{API_KEY}"),
    (re.compile(r"\b[A-Za-z0-9_\-]{32,}\b"), "{TOKEN}"),
    (re.compile(r"^\s*\d{4,8}\s*$"), "{CODE}"),
]
DENY = {"com.1password.1password", "com.agilebits.onepassword7", "com.bitwarden.desktop",
        "com.apple.keychainaccess", "com.apple.Passwords"}


def mask(v: str) -> str:
    for rx, rep in MASKS:
        v = rx.sub(rep, v)
    return v


def attr(el, name):
    if el is None:
        return None
    err, v = AX.AXUIElementCopyAttributeValue(el, name, None)
    return v if err == 0 else None


def pid_of(el) -> int | None:
    try:
        err, pid = AX.AXUIElementGetPid(el, None)
        return pid if err == 0 else None
    except Exception:
        return None


def describe(el) -> dict:
    d = {}
    for k, a in [("role", "AXRole"), ("subrole", "AXSubrole"), ("title", "AXTitle"),
                 ("desc", "AXDescription"), ("help", "AXHelp"), ("placeholder", "AXPlaceholderValue"),
                 ("identifier", "AXIdentifier")]:
        v = attr(el, a)
        if v not in (None, ""):
            d[k] = str(v)[:120]
    # unlabeled controls: borrow the title of an AXTitleUIElement or the parent
    if not any(k in d for k in ("title", "desc", "placeholder")):
        t = attr(attr(el, "AXTitleUIElement"), "AXValue") or attr(attr(el, "AXParent"), "AXTitle")
        if t:
            d["label_from_context"] = str(t)[:120]
    pid = pid_of(el)
    if pid:
        app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        if app:
            d["app"] = str(app.bundleIdentifier())
            d["app_name"] = str(app.localizedName())
        app_el = AX.AXUIElementCreateApplication(pid)
        w = attr(app_el, "AXFocusedWindow")
        if w is not None:
            d["window"] = str(attr(w, "AXTitle") or "")[:120]
    return d


def is_secure(info: dict) -> bool:
    return info.get("subrole") == "AXSecureTextField" or "secure" in info.get("role", "").lower()


class Recorder:
    def __init__(self, out_path: str):
        self.raw = collections.deque(maxlen=5000)
        self.out = open(out_path, "w")
        self.typing = None  # {"el":..., "info":..., "keys": n, "t0":..., "t1":...}
        self.last_app = None
        self.counts = collections.Counter()

    def emit(self, step: dict) -> None:
        if step.get("app") in DENY:
            self.counts["denied"] += 1
            return
        self.out.write(json.dumps(step) + "\n")
        self.out.flush()
        self.counts[step["type"]] += 1
        print(json.dumps(step), flush=True)

    def flush_typing(self) -> None:
        t = self.typing
        if not t:
            return
        self.typing = None
        info = t["info"]
        if is_secure(info):
            value = "{SECRET}"
        else:
            v = attr(t["el"], "AXValue")
            value = mask(str(v)) if isinstance(v, (str,)) or v is not None else None
            if value is not None and len(value) > 500:
                value = value[:500] + "…"
        self.emit({"type": "typed", "ts": t["t0"], "ts_end": t["t1"], "keys": t["keys"],
                   "value": value, "special_keys": t["special"], **{k: v for k, v in info.items()}})

    def check_app(self, ts: float) -> None:
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        bid = str(app.bundleIdentifier()) if app else None
        if bid != self.last_app:
            self.last_app = bid
            self.emit({"type": "app_switch", "ts": ts, "app": bid,
                       "app_name": str(app.localizedName()) if app else None})

    def process(self) -> None:
        while self.raw:
            kind, ts, x, y, keyinfo = self.raw.popleft()
            self.check_app(ts)
            if kind == "click":
                self.flush_typing()
                err, el = AX.AXUIElementCopyElementAtPosition(SYS, x, y, None)
                info = describe(el) if err == 0 else {"ax_error": err}
                self.emit({"type": "click", "ts": ts, "x": round(x), "y": round(y), **info})
            elif kind == "key":
                foc = attr(SYS, "AXFocusedUIElement")
                same = self.typing is not None and foc is not None and \
                    bool(Quartz.CFEqual(foc, self.typing["el"]))
                if not same:
                    self.flush_typing()
                    info = describe(foc) if foc is not None else {}
                    self.typing = {"el": foc, "info": info, "keys": 0, "t0": ts, "t1": ts, "special": []}
                self.typing["keys"] += 1
                self.typing["t1"] = ts
                if keyinfo in ("return", "tab", "escape"):
                    self.typing["special"].append(keyinfo)
                    if keyinfo in ("return", "tab"):
                        # Return/Tab usually commits the field; flush after the app applies it
                        self.flush_typing()
                elif keyinfo.startswith("cmd+"):
                    self.typing["special"].append(keyinfo)
        if self.typing and time.time() - self.typing["t1"] > 2.0:
            self.flush_typing()


SPECIAL = {36: "return", 48: "tab", 53: "escape", 51: "delete"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=120)
    ap.add_argument("--out", default=os.path.join(common.OUT_DIR, "s3_events.jsonl"))
    a = ap.parse_args()
    rec = Recorder(a.out)
    state = {"tap": None}

    def cb(proxy, etype, event, refcon):
        if etype in (Quartz.kCGEventTapDisabledByTimeout, Quartz.kCGEventTapDisabledByUserInput):
            rec.counts["tap_reenabled"] += 1
            Quartz.CGEventTapEnable(state["tap"], True)
            return event
        ts = time.time()
        loc = Quartz.CGEventGetLocation(event)
        if etype == Quartz.kCGEventLeftMouseDown:
            rec.raw.append(("click", ts, loc.x, loc.y, ""))
        elif etype == Quartz.kCGEventKeyDown:
            code = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode)
            flags = Quartz.CGEventGetFlags(event)
            # Never keep the characters themselves: only the special-key name or a modifier combo
            k = SPECIAL.get(code, "char")
            if flags & Quartz.kCGEventFlagMaskCommand:
                _, s = Quartz.CGEventKeyboardGetUnicodeString(event, 4, None, None)
                k = f"cmd+{s}" if s else "cmd+?"
            rec.raw.append(("key", ts, loc.x, loc.y, k))
        return event

    mask_bits = Quartz.CGEventMaskBit(Quartz.kCGEventLeftMouseDown) | Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown)
    tap = Quartz.CGEventTapCreate(Quartz.kCGSessionEventTap, Quartz.kCGHeadInsertEventTap,
                                  Quartz.kCGEventTapOptionListenOnly, mask_bits, cb, None)
    if tap is None:
        sys.exit("CGEventTapCreate failed: grant Input Monitoring")
    state["tap"] = tap
    src = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
    Quartz.CFRunLoopAddSource(Quartz.CFRunLoopGetCurrent(), src, Quartz.kCFRunLoopCommonModes)
    Quartz.CGEventTapEnable(tap, True)
    print(f"recording UI events for {a.seconds:.0f}s -> {a.out}", file=sys.stderr, flush=True)
    end = time.time() + a.seconds
    lat = []
    while time.time() < end:
        common.pump(0.1)
        t = time.time()
        rec.process()
        lat.append(time.time() - t)
    rec.flush_typing()
    rec.out.close()
    print(json.dumps({"counts": rec.counts, "process_ms_p50": 1000 * sorted(lat)[len(lat) // 2],
                      "process_ms_max": 1000 * max(lat)}), file=sys.stderr)


if __name__ == "__main__":
    main()
