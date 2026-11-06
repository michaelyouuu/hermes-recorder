"""The recorder panel: a floating HUD (live transcript, ask box, pause/stop) shown by the global hotkey.

Runs in the hotkey agent (Hermes Recorder Helper.app in hotkey mode), which never captures anything itself. It is
a non-activating NSPanel, so it takes typing without pulling focus from the call or video underneath, and it renders
``panel.html`` in a WKWebView. Everything it shows comes from the gateway over the owner-only control socket:
``feed`` once a second while visible, ``ask`` for questions, ``pause``/``resume``/``on``/``off`` for the buttons.
Nothing is written to disk.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable

import objc
from AppKit import NSColor, NSEvent, NSMakeRect, NSPanel, NSScreen, NSVisualEffectView
from Foundation import NSURL, NSObject
from PyObjCTools import AppHelper
from WebKit import WKUserContentController, WKWebView, WKWebViewConfiguration

log = logging.getLogger("hermes_recorder_helper")

HTML = Path(__file__).with_name("panel.html")
W, H = 400, 540
POLL_S = 1.0
NOT_LISTENING = "Hermes isn't listening yet. Send Hermes any message, then try again."

# NSWindowStyleMask bits and other AppKit constants (numeric, so older pyobjc metadata can't trip us up)
TITLED, CLOSABLE, RESIZABLE, NONACTIVATING, FULL_SIZE = 1, 2, 8, 1 << 7, 1 << 15
FLOATING_LEVEL = 3
JOIN_ALL_SPACES, FULLSCREEN_AUX = 1, 1 << 8
HUD_MATERIAL, BEHIND_WINDOW, ACTIVE = 13, 0, 1
SIZE_TO_FIT = 2 | 16  # width + height sizable

Client = Callable[..., dict]  # client(op, apps=None, fallback_apps=None, args=None) -> reply


class _Panel(NSPanel):
    def canBecomeKeyWindow(self):
        return True

    def canBecomeMainWindow(self):
        return False


class _Bridge(NSObject, protocols=[objc.protocolNamed("WKScriptMessageHandler")]):
    def userContentController_didReceiveScriptMessage_(self, controller, message):
        owner = getattr(self, "owner", None)
        if owner is not None:
            owner._on_js(message.body())


def _plain(v: Any) -> Any:
    """NSDictionary/NSNumber/NSString from a script message -> plain Python."""
    if hasattr(v, "keys"):
        return {str(k): _plain(v[k]) for k in v.keys()}
    if isinstance(v, (list, tuple)) or hasattr(v, "count") and hasattr(v, "objectAtIndex_"):
        return [_plain(x) for x in v]
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v
    return None if v is None else str(v)


class RecorderPanel:
    def __init__(self, client: Client):
        self.client = client
        self.win = None
        self.web = None
        self._ready = False
        self._visible = threading.Event()
        self._reset = True
        self._since = 0.0
        self._state = "unknown"  # last state from the gateway
        self._poller: threading.Thread | None = None

    # ------------------------------------------------------------------ main thread
    def _build(self) -> None:
        style = TITLED | CLOSABLE | RESIZABLE | NONACTIVATING | FULL_SIZE
        win = _Panel.alloc().initWithContentRect_styleMask_backing_defer_(NSMakeRect(0, 0, W, H), style, 2, False)
        win.setTitle_("Hermes Recorder")
        win.setTitlebarAppearsTransparent_(True)
        win.setTitleVisibility_(1)  # hidden
        for b in (0, 1, 2):  # close, miniaturize, zoom buttons: the panel has its own ✕
            btn = win.standardWindowButton_(b)
            if btn is not None:
                btn.setHidden_(True)
        win.setMovableByWindowBackground_(True)
        win.setFloatingPanel_(True)
        win.setHidesOnDeactivate_(False)
        win.setLevel_(FLOATING_LEVEL)
        win.setCollectionBehavior_(JOIN_ALL_SPACES | FULLSCREEN_AUX)
        win.setReleasedWhenClosed_(False)
        win.setOpaque_(False)
        win.setBackgroundColor_(NSColor.clearColor())
        win.setMinSize_((320, 360))

        fx = NSVisualEffectView.alloc().initWithFrame_(NSMakeRect(0, 0, W, H))
        fx.setMaterial_(HUD_MATERIAL)
        fx.setBlendingMode_(BEHIND_WINDOW)
        fx.setState_(ACTIVE)
        fx.setAutoresizingMask_(SIZE_TO_FIT)
        win.setContentView_(fx)

        cfg = WKWebViewConfiguration.alloc().init()
        ucc = WKUserContentController.alloc().init()
        self._bridge = _Bridge.alloc().init()
        self._bridge.owner = self
        ucc.addScriptMessageHandler_name_(self._bridge, "rec")
        cfg.setUserContentController_(ucc)
        web = WKWebView.alloc().initWithFrame_configuration_(fx.bounds(), cfg)
        web.setValue_forKey_(False, "drawsBackground")  # let the blur show through
        web.setAutoresizingMask_(SIZE_TO_FIT)
        fx.addSubview_(web)
        web.loadFileURL_allowingReadAccessToURL_(NSURL.fileURLWithPath_(str(HTML)),
                                                 NSURL.fileURLWithPath_(str(HTML.parent)))
        self._place(win)
        win.setFrameAutosaveName_("HermesRecorderPanel")  # restores the user's last position, if any
        self.win, self.web = win, web

    @staticmethod
    def _place(win) -> None:
        """Top-right of the screen under the pointer."""
        mouse = NSEvent.mouseLocation()
        screen = next((s for s in NSScreen.screens()
                       if s.frame().origin.x <= mouse.x <= s.frame().origin.x + s.frame().size.width
                       and s.frame().origin.y <= mouse.y <= s.frame().origin.y + s.frame().size.height),
                      NSScreen.mainScreen())
        vf = screen.visibleFrame()
        win.setFrame_display_(NSMakeRect(vf.origin.x + vf.size.width - W - 16,
                                         vf.origin.y + vf.size.height - H - 16, W, H), False)

    @property
    def visible(self) -> bool:
        return self._visible.is_set()

    def show(self) -> None:
        if self.win is None:
            self._build()
        self._reset = True
        self._since = 0.0
        self.win.orderFrontRegardless()
        self.win.makeKeyWindow()
        self.win.makeFirstResponder_(self.web)
        self._visible.set()
        self._js("window.rec && rec.shown()")
        if self._poller is None or not self._poller.is_alive():
            self._poller = threading.Thread(target=self._poll, name="panel-feed", daemon=True)
            self._poller.start()
        log.info("panel shown")

    def hide(self) -> None:
        self._visible.clear()
        if self.win is not None:
            self.win.orderOut_(None)
        log.info("panel hidden")

    def toggle(self) -> None:
        self.hide() if self.visible else self.show()

    def _js(self, code: str) -> None:
        if self.web is not None and self._ready:
            self.web.evaluateJavaScript_completionHandler_(code, None)

    def _push(self, method: str, *args: Any) -> None:
        """Call ``rec.<method>(...)`` in the page (any thread)."""
        code = f"window.rec && rec.{method}({', '.join(json.dumps(a) for a in args)})"
        AppHelper.callAfter(self._js, code)

    def _on_js(self, body: Any) -> None:  # main thread
        msg = _plain(body) or {}
        op = msg.get("op")
        if op == "ready":
            self._ready = True
            self._reset = True
            self._since = 0.0
            self._js("rec.shown()")
        elif op == "hide":
            self.hide()
        elif op in ("pause", "resume", "start", "stop"):
            self._async(self._control, {"start": "on", "stop": "off"}.get(op, op))
        elif op == "ask":
            self._async(self._ask, msg.get("id"), msg.get("question") or "", msg.get("minutes") or 5)

    # ------------------------------------------------------------------ worker threads
    @staticmethod
    def _async(fn: Callable, *args: Any) -> None:
        threading.Thread(target=fn, args=args, name="panel-req", daemon=True).start()

    def _request(self, op: str, timeout: float = 30, **kw: Any) -> dict:
        try:
            return self.client(op, timeout=timeout, **kw)
        except ConnectionError:
            return {"state": "error", "error": NOT_LISTENING}
        except Exception as e:
            return {"state": "error", "error": f"{type(e).__name__}: {e}"}

    def _control(self, op: str) -> None:
        if op == "on":
            self._push("feed", {"state": "starting", "lines": []})
        self._push("feed", {**self._request(op), "lines": []})

    def _ask(self, qid: Any, question: str, minutes: Any) -> None:
        t0 = time.time()
        r = self._request("ask", timeout=90, args={"question": question, "minutes": minutes})
        log.info("panel ask %s in %.1fs", "failed" if r.get("error") else "ok", time.time() - t0)
        self._push("reply", qid, {k: r.get(k) for k in ("answer", "error", "seconds")})

    def _poll(self) -> None:
        while True:
            if not self._visible.wait(timeout=30):
                continue
            if self._ready:
                r = self._request("feed", timeout=10, args={"since": self._since})
                if r.get("now"):
                    self._since = r["now"]
                self._state = r.get("state", "error")
                if self._reset:
                    r["reset"], self._reset = True, False
                self._push("feed", r)
            time.sleep(POLL_S)

    def note_starting(self) -> None:
        """The hotkey just asked the gateway to start capture (which takes a few seconds)."""
        if self._state in ("stopped", "error"):
            self._push("feed", {"state": "starting", "lines": []})
