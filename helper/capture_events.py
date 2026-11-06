"""UI events for teach mode: what the user clicked and typed, named by Accessibility. Production version of
spikes/s3_events.py (S3 findings in spikes/RESULTS.md).

- A listen-only CGEventTap (Input Monitoring) sees left clicks and key-downs. The callback only enqueues
  (time, position, key name); it never keeps characters. A worker thread resolves elements with Accessibility.
- Clicks name the element under the pointer, climbing from static text/groups to the actionable ancestor.
- Typing is coalesced per focused field into one "typed" step: single-line fields keep their short final value,
  multi-line fields only the delta (inserted text, deleted count) against a snapshot taken before typing.
- Secure fields (AXSecureTextField) are never read: they yield {SECRET}. macOS Secure Event Input also withholds
  their keys from every tap. Values are masked again in the plugin (hermes_recorder/privacy.py).
- Chromium and Electron apps (browsers, Linear, Slack) expose their page elements once AXManualAccessibility is set.

``emit(mtype, **fields)`` sends ``ui_event`` (``app``, ``kind`` click|typed|key, ``target``…) and ``app_switch``.
"""
from __future__ import annotations

import collections
import logging
import threading
import time
from typing import Any, Callable

log = logging.getLogger("hermes_recorder_helper")

ACTIONABLE = {"AXLink", "AXButton", "AXMenuItem", "AXCheckBox", "AXRadioButton", "AXPopUpButton",
              "AXMenuButton", "AXTab", "AXCell", "AXRow", "AXDisclosureTriangle", "AXComboBox", "AXTextField",
              "AXTextArea"}
SINGLE_LINE = {"AXTextField", "AXComboBox", "AXSearchField"}
SPECIAL = {36: "return", 76: "return", 48: "tab", 53: "escape"}
IDLE_FLUSH_S = 2.0
SECRET = "{SECRET}"


def text_delta(before: str, after: str) -> dict[str, Any]:
    """Common prefix/suffix diff: what was inserted and how many characters were removed."""
    i = 0
    while i < min(len(before), len(after)) and before[i] == after[i]:
        i += 1
    j = 0
    while j < min(len(before), len(after)) - i and before[-1 - j] == after[-1 - j]:
        j += 1
    return {"inserted": after[i:len(after) - j], "deleted_chars": len(before) - i - j}


def page_url(url: Any) -> str | None:
    """Scheme, host and path only: query strings and fragments often carry tokens or search terms."""
    if not url:
        return None
    from urllib.parse import urlsplit
    try:
        u = urlsplit(str(url))
    except ValueError:
        return None
    if u.scheme not in ("http", "https") or not u.netloc:
        return None
    return f"{u.scheme}://{u.netloc}{u.path}"[:200]


def is_secure(info: dict[str, Any]) -> bool:
    return info.get("subrole") == "AXSecureTextField" or "secure" in str(info.get("role", "")).lower()


class EventCapture:
    def __init__(self, emit: Callable[..., None]):
        import HIServices as AX
        import Quartz
        from AppKit import NSRunningApplication, NSWorkspace
        self.AX, self.Q, self.NSRunningApplication, self.NSWorkspace = AX, Quartz, NSRunningApplication, NSWorkspace
        self.emit = emit
        self.sys = AX.AXUIElementCreateSystemWide()
        AX.AXUIElementSetMessagingTimeout(self.sys, 0.25)
        self.raw: collections.deque = collections.deque(maxlen=5000)
        self.typing: dict[str, Any] | None = None
        self.last_app: str | None = None
        self.focus_cache: tuple[Any, Any] = (None, None)
        self.tap = self.src = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.counts: collections.Counter = collections.Counter()

    # ------------------------------------------------------------------ lifecycle (main thread)
    def start(self) -> bool:
        Q = self.Q
        mask = Q.CGEventMaskBit(Q.kCGEventLeftMouseDown) | Q.CGEventMaskBit(Q.kCGEventKeyDown)
        self.tap = Q.CGEventTapCreate(Q.kCGSessionEventTap, Q.kCGHeadInsertEventTap, Q.kCGEventTapOptionListenOnly,
                                      mask, self._cb, None)
        if self.tap is None:
            log.warning("event tap unavailable: Input Monitoring isn't allowed for Hermes Recorder Helper")
            return False
        self.src = Q.CFMachPortCreateRunLoopSource(None, self.tap, 0)
        Q.CFRunLoopAddSource(Q.CFRunLoopGetCurrent(), self.src, Q.kCFRunLoopCommonModes)
        Q.CGEventTapEnable(self.tap, True)
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="ui-events", daemon=True)
        self._thread.start()
        log.info("ui events on (accessibility trusted=%s)", bool(self.AX.AXIsProcessTrusted()))
        return True

    def stop(self) -> None:
        if self.tap is not None:
            self.Q.CGEventTapEnable(self.tap, False)
            self.Q.CFRunLoopRemoveSource(self.Q.CFRunLoopGetCurrent(), self.src, self.Q.kCFRunLoopCommonModes)
            self.tap = self.src = None
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        self.raw.clear()
        self.typing = None

    def _cb(self, proxy, etype, event, refcon):  # tap callback: enqueue only, never characters
        Q = self.Q
        if etype in (Q.kCGEventTapDisabledByTimeout, Q.kCGEventTapDisabledByUserInput):
            if self.tap is not None:
                Q.CGEventTapEnable(self.tap, True)
            return event
        ts = time.time()
        loc = Q.CGEventGetLocation(event)
        if etype == Q.kCGEventLeftMouseDown:
            self.raw.append(("click", ts, loc.x, loc.y, ""))
        elif etype == Q.kCGEventKeyDown:
            code = Q.CGEventGetIntegerValueField(event, Q.kCGKeyboardEventKeycode)
            flags = Q.CGEventGetFlags(event)
            k = SPECIAL.get(code, "char")
            if flags & Q.kCGEventFlagMaskCommand:
                if k == "char":
                    _, s = Q.CGEventKeyboardGetUnicodeString(event, 4, None, None)
                    k = (s or "?").lower()
                k = f"cmd+{k}"
            self.raw.append(("key", ts, loc.x, loc.y, k))
        return event

    # ------------------------------------------------------------------ worker
    def _loop(self) -> None:
        while not self._stop.wait(0.1):
            try:
                self.process()
            except Exception as e:  # never let one bad element stop capture
                self.counts["errors"] += 1
                log.debug("ui event processing failed: %s", e)
        self.flush_typing()

    def attr(self, el, name):
        if el is None:
            return None
        err, v = self.AX.AXUIElementCopyAttributeValue(el, name, None)
        return v if err == 0 else None

    def describe(self, el) -> tuple[dict[str, Any], dict[str, Any]]:
        """(target, context): the element's names, and its app and window."""
        d: dict[str, Any] = {}
        for k, a in (("role", "AXRole"), ("subrole", "AXSubrole"), ("title", "AXTitle"), ("desc", "AXDescription"),
                     ("placeholder", "AXPlaceholderValue"), ("identifier", "AXIdentifier")):
            v = self.attr(el, a)
            if v not in (None, ""):
                d[k] = str(v)[:120]
        if d.get("role") in ("AXStaticText", "AXMenuItem", "AXCell", "AXRow") and not d.get("title"):
            v = self.attr(el, "AXValue")
            if isinstance(v, str) and v and not is_secure(d):
                d["text"] = v[:80]
        if not any(k in d for k in ("title", "desc", "placeholder", "text")):
            t = self.attr(self.attr(el, "AXTitleUIElement"), "AXValue") or self.attr(self.attr(el, "AXParent"),
                                                                                     "AXTitle")
            if t:
                d["label"] = str(t)[:120]
        ctx: dict[str, Any] = {}
        try:
            err, pid = self.AX.AXUIElementGetPid(el, None)
        except Exception:
            err, pid = 1, None
        if err == 0 and pid:
            app = self.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
            if app is not None:
                ctx["app"] = str(app.bundleIdentifier() or "")
                ctx["app_name"] = str(app.localizedName() or "")
            w = self.attr(self.AX.AXUIElementCreateApplication(pid), "AXFocusedWindow")
            if w is not None:
                ctx["window"] = str(self.attr(w, "AXTitle") or "")[:120]
        url = self.web_url(el)
        if url:
            ctx["url"] = url
        return d, ctx

    def web_url(self, el, depth: int = 30) -> str | None:
        """The page URL for an element inside a web view (browsers, Electron apps), via its AXWebArea."""
        for _ in range(depth):
            if el is None:
                return None
            if self.attr(el, "AXRole") == "AXWebArea":
                return page_url(self.attr(el, "AXURL"))
            el = self.attr(el, "AXParent")
        return None

    def check_app(self, ts: float) -> None:
        app = self.NSWorkspace.sharedWorkspace().frontmostApplication()
        bid = str(app.bundleIdentifier()) if app is not None and app.bundleIdentifier() else None
        if bid == self.last_app:
            return
        self.last_app = bid
        if app is not None:
            # Chromium/Electron build their web accessibility tree lazily; this asks them to expose it.
            self.AX.AXUIElementSetAttributeValue(self.AX.AXUIElementCreateApplication(app.processIdentifier()),
                                                 "AXManualAccessibility", True)
        self.emit("app_switch", ts=ts, app=bid, app_name=str(app.localizedName()) if app is not None else None)

    def same(self, a, b) -> bool:
        return a is not None and b is not None and bool(self.Q.CFEqual(a, b))

    def flush_typing(self) -> None:
        t, self.typing = self.typing, None
        if not t:
            return
        target = t["target"]
        if is_secure(target):
            change: dict[str, Any] = {"value": SECRET}
        else:
            v = self.attr(t["el"], "AXValue") if self.same(self.attr(self.sys, "AXFocusedUIElement"), t["el"]) \
                else t.get("last_value")
            after = v if isinstance(v, str) else t.get("last_value")
            if after is None or t["before"] is None:
                change = {"value": None}
            elif target.get("role") in SINGLE_LINE:
                change = {"value": after[:500]}
            else:
                d = text_delta(t["before"], after)
                d["inserted"] = d["inserted"][:1000]
                change = d
        self.emit("ui_event", ts=t["t0"], kind="typed", ts_end=t["t1"], keys=t["keys"], special=t["special"],
                  target=target, **t["ctx"], **change)

    def process(self) -> None:
        while self.raw:
            kind, ts, x, y, key = self.raw.popleft()
            self.check_app(ts)
            if kind == "click":
                self.flush_typing()
                err, el = self.AX.AXUIElementCopyElementAtPosition(self.sys, x, y, None)
                if err != 0:
                    self.emit("ui_event", ts=ts, kind="click", app=self.last_app, target={"ax_error": err})
                    continue
                target, ctx = self.describe(el)
                if target.get("role") in ("AXStaticText", "AXGroup", "AXImage"):
                    anc = el
                    for _ in range(4):
                        anc = self.attr(anc, "AXParent")
                        if self.attr(anc, "AXRole") in ACTIONABLE:
                            up, _ = self.describe(anc)
                            up["inner"] = {k: target[k] for k in ("role", "text", "title", "desc") if k in target}
                            target = up
                            break
                self.emit("ui_event", ts=ts, kind="click", target=target, **{"app": self.last_app, **ctx})
            else:
                self.key(ts, key)
        if self.typing and not is_secure(self.typing["target"]):
            v = self.attr(self.typing["el"], "AXValue")
            if isinstance(v, str):
                self.typing["last_value"] = v
        if self.typing and time.time() - self.typing["t1"] > IDLE_FLUSH_S:
            self.flush_typing()
        if self.typing is None:
            foc = self.attr(self.sys, "AXFocusedUIElement")
            sub = self.attr(foc, "AXSubrole") if foc is not None else None
            v = None if foc is None or sub == "AXSecureTextField" else self.attr(foc, "AXValue")
            self.focus_cache = (foc, v if isinstance(v, str) else None)

    def key(self, ts: float, key: str) -> None:
        foc = self.attr(self.sys, "AXFocusedUIElement")
        same = self.typing is not None and self.same(foc, self.typing["el"])
        if not same and key in ("return", "tab", "cmd+return") and self.typing is not None \
                and ts - self.typing["t1"] < 1.0:
            # the key that submitted a field lands after focus moved on: it belongs to the field
            self.typing["special"].append(key)
            self.flush_typing()
            return
        if key.startswith("cmd+") and key not in ("cmd+return", "cmd+v", "cmd+x", "cmd+z"):
            self.flush_typing()  # shortcuts (cmd+k, cmd+s) are steps of their own
            target, ctx = self.describe(foc) if foc is not None else ({}, {})
            self.emit("ui_event", ts=ts, kind="key", key=key, target=target, **{"app": self.last_app, **ctx})
            return
        if not same:
            self.flush_typing()
            target, ctx = self.describe(foc) if foc is not None else ({}, {})
            cel, cval = self.focus_cache
            if is_secure(target):
                before = None
            elif self.same(cel, foc):
                before = cval  # captured before the first keystroke landed
            else:
                before = self.attr(foc, "AXValue")
            self.typing = {"el": foc, "target": target, "ctx": {"app": self.last_app, **ctx}, "keys": 0, "t0": ts,
                           "t1": ts, "special": [], "before": before if isinstance(before, str) else None}
        self.typing["keys"] += 1
        self.typing["t1"] = ts
        if key in ("return", "tab", "escape", "cmd+return"):
            self.typing["special"].append(key)
            single = self.typing["target"].get("role") in SINGLE_LINE
            if key in ("tab", "cmd+return") or (key == "return" and single):
                self.flush_typing()
