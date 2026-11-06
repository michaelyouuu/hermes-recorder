"""The recorder panel: a floating HUD (live transcript, ask box, pause/stop) shown by the global hotkey.

Also: Save notes (the ``notes`` op writes a markdown note through the gateway; "Show in Finder" reveals it), an
offer to save notes when recording stops, and follow-up questions (the page sends its last few Q&As with each ask).

Runs in the hotkey agent (Hermes Recorder Helper.app in hotkey mode), which never captures anything itself. It is
a non-activating NSPanel, so it takes typing without pulling focus from the call or video underneath, and it renders
``panel.html`` in a WKWebView: Q&A over the recent buffer, no live transcript. Everything comes from the gateway
over the owner-only control socket: ``status`` every 2 s while visible, ``ask`` for questions,
``pause``/``resume``/``on``/``off`` for the buttons.
Nothing is written to disk.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

import objc
from AppKit import NSColor, NSEvent, NSMakeRect, NSPanel, NSScreen, NSVisualEffectView, NSWorkspace
from Foundation import NSURL, NSObject
from PyObjCTools import AppHelper
from WebKit import WKUserContentController, WKWebView, WKWebViewConfiguration

log = logging.getLogger("hermes_recorder_helper")

HTML = Path(__file__).with_name("panel.html")
W, H = 400, 460
POLL_S = 2.0
NOT_LISTENING = "Hermes is waking up. Send it a message, then try again."
HERMES_APP_IDS = ("com.nousresearch.hermes.setup", "com.nousresearch.hermes")  # Hermes Desktop
NOTES_TIMEOUT_S = 180

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


def _app_name(bid: str) -> str:
    from hermes_recorder.runtime import APP_NAMES  # friendly names shared with the plugin ("Zoom", not "zoom.us")
    if bid in APP_NAMES:
        return APP_NAMES[bid]
    try:
        url = NSWorkspace.sharedWorkspace().URLForApplicationWithBundleIdentifier_(bid)
        if url is not None:
            return str(url.lastPathComponent()).removesuffix(".app")
    except Exception:
        pass
    return bid.rsplit(".", 1)[-1]


def _hermes_app():
    """URL of the Hermes Desktop app, if installed."""
    ws = NSWorkspace.sharedWorkspace()
    for bid in HERMES_APP_IDS:
        url = ws.URLForApplicationWithBundleIdentifier_(bid)
        if url is not None:
            return url
    return None


class RecorderPanel:
    def __init__(self, client: Client):
        self.client = client
        self.win = None
        self.web = None
        self._ready = False
        self._visible = threading.Event()
        self._state = "unknown"  # last state from the gateway
        self._poller: threading.Thread | None = None
        self._queued: list[str] = []  # page calls made before the page finished loading

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

    def show(self, focus: bool = True) -> None:
        """``focus=False`` (call offers) floats the panel without taking the keyboard from the call."""
        if self.win is None:
            self._build()
        self.win.orderFrontRegardless()
        if focus:
            self.win.makeKeyWindow()
            self.win.makeFirstResponder_(self.web)
            self._js("window.rec && rec.shown()")
        self._visible.set()
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
        elif code.startswith("window.rec && rec.") and "rec.shown()" not in code:
            self._queued.append(code)

    def _push(self, method: str, *args: Any) -> None:
        """Call ``rec.<method>(...)`` in the page (any thread)."""
        code = f"window.rec && rec.{method}({', '.join(json.dumps(a) for a in args)})"
        AppHelper.callAfter(self._js, code)

    def _on_js(self, body: Any) -> None:  # main thread
        msg = _plain(body) or {}
        op = msg.get("op")
        if op == "ready":
            self._ready = True
            self._js("rec.shown()")
            queued, self._queued = self._queued, []
            for code in queued:
                self._js(code)
        elif op == "hide":
            self.hide()
        elif op in ("pause", "resume", "start", "stop"):
            self._async(self._control, {"start": "on", "stop": "off"}.get(op, op))
        elif op == "ask":
            self._async(self._ask, msg.get("id"), msg.get("question") or "", msg.get("minutes") or 5,
                        msg.get("history") or [])
        elif op == "notes":
            self._async(self._notes, msg.get("id"))
        elif op == "reveal":
            self._reveal(str(msg.get("path") or ""))
        elif op == "recordCall":
            self._async(self._record_call, str(msg.get("bid") or ""))
        elif op == "openHermes":
            url = _hermes_app()
            if url is not None:
                NSWorkspace.sharedWorkspace().openURL_(url)

    # ------------------------------------------------------------------ worker threads
    @staticmethod
    def _async(fn: Callable, *args: Any) -> None:
        threading.Thread(target=fn, args=args, name="panel-req", daemon=True).start()

    def _request(self, op: str, timeout: float = 30, **kw: Any) -> dict:
        try:
            return self.client(op, timeout=timeout, **kw)
        except ConnectionError:
            return {"state": "error", "error": NOT_LISTENING, "not_listening": True,
                    "can_open_hermes": _hermes_app() is not None}
        except Exception as e:
            return {"state": "error", "error": f"{type(e).__name__}: {e}"}

    def _control(self, op: str) -> None:
        if op == "on":
            self._push("status", {"state": "starting"})
        self._push("status", self._request(op))

    def _ask(self, qid: Any, question: str, minutes: Any, history: Any = None) -> None:
        t0 = time.time()
        r = self._request("ask", timeout=90, args={"question": question, "minutes": minutes, "history": history})
        log.info("panel ask %s in %.1fs", "failed" if r.get("error") else "ok", time.time() - t0)
        self._push("reply", qid, {k: r.get(k) for k in ("answer", "error", "seconds")})

    def _notes(self, qid: Any) -> None:
        t0 = time.time()
        r = self._request("notes", timeout=NOTES_TIMEOUT_S, args={})
        log.info("panel notes %s in %.1fs", "failed" if r.get("error") else "saved", time.time() - t0)
        self._push("reply", qid, {k: r.get(k) for k in ("path", "title", "summary", "note", "error", "kind",
                                                          "decisions", "action_items")})

    def _record_call(self, bid: str) -> None:
        self._push("status", {"state": "starting"})
        r = self._request("record_call", apps=[bid] if bid else None)
        self._push("status", r)

    # ------------------------------------------------------------------ calls (helper/call_watch.py)
    def on_call(self, kind: str, app: str) -> None:
        """A call started or ended in ``app`` (main thread). Offers only; never starts recording itself."""
        self._async(self._call_event, kind, app)

    def _call_event(self, kind: str, app: str) -> None:
        try:
            st = self.client("status", timeout=5)
        except Exception:
            st = {"state": "unknown", "error": NOT_LISTENING, "not_listening": True}
        recording = st.get("state") in ("running", "paused", "starting") and app in (st.get("bundle_ids") or [])
        name = _app_name(app)
        if kind == "start":
            if recording or st.get("call_prompt") is False:
                return
            AppHelper.callAfter(self._offer, "offerCall", {"bid": app, "app": name})
        elif kind == "end":
            if recording:
                AppHelper.callAfter(self._offer, "callEnded", {"bid": app, "app": name})

    def _offer(self, method: str, info: dict) -> None:  # main thread
        self.show(focus=False)
        self._push(method, info)

    @staticmethod
    def _reveal(path: str) -> None:
        """Show a saved note in Finder (only .md files that exist)."""
        if path.endswith(".md") and os.path.isfile(path):
            subprocess.run(["/usr/bin/open", "-R", path], check=False, timeout=10)

    def _poll(self) -> None:
        while True:
            if not self._visible.wait(timeout=30):
                continue
            if self._ready:
                r = self._request("status", timeout=10)
                self._state = r.get("state", "error")
                self._push("status", r)
            time.sleep(POLL_S)

    def note_starting(self) -> None:
        """The hotkey just asked the gateway to start capture (which takes a few seconds)."""
        if self._state in ("stopped", "error"):
            self._push("status", {"state": "starting"})
