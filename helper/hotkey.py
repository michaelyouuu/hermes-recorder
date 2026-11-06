"""Global recording hotkey, run as a tiny login item inside Hermes Recorder Helper.app.

Uses Carbon's RegisterEventHotKey, which catches one key combination from any app without Accessibility or
Input Monitoring permission (it never sees other keystrokes). A press shows or hides the recorder panel
(helper/panel.py) and, if capture is off, asks the gateway to start it over the owner-only control socket.
Sounds: Tink when capture starts, Basso on error. Without WebKit (deps not reinstalled yet) a press falls back to
toggling capture.
"""
from __future__ import annotations

import ctypes
import logging
import threading
import time
from pathlib import Path

log = logging.getLogger("hermes_recorder_helper")

from hermes_recorder.hotkeys import parse_combo  # shared with the plugin (stdlib only)

SOUNDS = {"on": "Tink", "off": "Pop", "error": "Basso"}


class _EventTypeSpec(ctypes.Structure):
    _fields_ = [("eventClass", ctypes.c_uint32), ("eventKind", ctypes.c_uint32)]


class _HotKeyID(ctypes.Structure):
    _fields_ = [("signature", ctypes.c_uint32), ("id", ctypes.c_uint32)]


_HANDLER = ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)


IGNORE_FRONT = {"com.nousresearch.hermes", "com.apple.finder", "com.apple.Terminal", "com.googlecode.iterm2",
                "com.apple.dock", "io.github.michaelyouuu.hermes-recorder.helper"}


def _frontmost() -> list[str] | None:
    """The app in front, used only when the recorder doesn't know which apps to capture yet."""
    try:
        from AppKit import NSWorkspace
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        bid = str(app.bundleIdentifier()) if app is not None and app.bundleIdentifier() else None
        return [bid] if bid and bid not in IGNORE_FRONT else None
    except Exception:
        return None


def _play(name: str) -> None:
    try:
        from AppKit import NSSound
        s = NSSound.soundNamed_(name)
        if s is not None:
            s.play()
    except Exception:
        pass


def run(combo: str, data_dir: str) -> int:
    from AppKit import NSApplication, NSDefaultRunLoopMode, NSEventMaskAny
    from Foundation import NSDate
    from hermes_recorder import control  # stdlib-only module from the plugin

    keycode, mask = parse_combo(combo)
    busy = threading.Lock()
    data = Path(data_dir)

    def client(op, timeout=30, apps=None, fallback_apps=None, args=None):
        return control.request(data, op, apps, timeout=timeout, fallback_apps=fallback_apps, args=args)

    panel = None
    try:
        from helper.panel import RecorderPanel
        panel = RecorderPanel(client)
    except Exception as e:  # WebKit missing: keep the plain toggle
        log.warning("recorder panel unavailable (%s); the hotkey only toggles capture", e)
    if panel is not None:  # offer to record calls when they start (helper/call_watch.py; offers only)
        try:
            from PyObjCTools import AppHelper
            from helper.call_watch import CallWatcher
            CallWatcher(lambda kind, app: AppHelper.callAfter(panel.on_call, kind, app)).start()
        except Exception as e:
            log.warning("call detection unavailable: %s", e)

    def summon() -> None:  # worker thread: start capture if it's off; the panel is already showing
        if not busy.acquire(blocking=False):
            return
        try:
            r = client("summon", fallback_apps=_frontmost())
            if r.get("error"):
                log.warning("hotkey summon error: %s", r["error"])
                _play(SOUNDS["error"])
            elif r.get("started"):
                log.info("hotkey started capture: %s", r.get("apps"))
                _play(SOUNDS["on"])
        except ConnectionError:
            log.info("hotkey pressed but the gateway isn't listening yet")
            _play(SOUNDS["error"])
        except Exception as e:
            log.warning("hotkey summon failed: %s", e)
            _play(SOUNDS["error"])
        finally:
            busy.release()

    def press() -> None:  # main thread
        if panel is None:
            threading.Thread(target=toggle, name="hotkey-toggle", daemon=True).start()
            return
        if panel.visible:
            panel.hide()
            return
        panel.show()
        panel.note_starting()
        threading.Thread(target=summon, name="hotkey-summon", daemon=True).start()

    def toggle() -> None:
        if not busy.acquire(blocking=False):
            return  # ignore presses while a toggle is in flight
        try:
            r = client("toggle", fallback_apps=_frontmost())
            state = r.get("state")
            if r.get("error"):
                log.warning("hotkey toggle error: %s", r["error"])
                _play(SOUNDS["error"])
            else:
                log.info("hotkey toggle -> %s %s", state, r.get("apps"))
                _play(SOUNDS["on"] if state == "running" else SOUNDS["off"])
        except ConnectionError:
            log.info("hotkey pressed but the gateway isn't listening yet")
            _play(SOUNDS["error"])
        except Exception as e:
            log.warning("hotkey toggle failed: %s", e)
            _play(SOUNDS["error"])
        finally:
            busy.release()

    @_HANDLER
    def on_hotkey(call_ref, event, user):  # Carbon callback (main thread): hand off, return immediately
        try:
            press()
        except Exception as e:
            log.warning("hotkey press failed: %s", e)
        return 0

    carbon = ctypes.CDLL("/System/Library/Frameworks/Carbon.framework/Carbon")
    carbon.GetApplicationEventTarget.restype = ctypes.c_void_p
    carbon.InstallEventHandler.argtypes = [ctypes.c_void_p, _HANDLER, ctypes.c_uint32,
                                           ctypes.POINTER(_EventTypeSpec), ctypes.c_void_p,
                                           ctypes.POINTER(ctypes.c_void_p)]
    carbon.RegisterEventHotKey.argtypes = [ctypes.c_uint32, ctypes.c_uint32, _HotKeyID, ctypes.c_void_p,
                                           ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)]
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(1)  # accessory: no Dock icon
    app.finishLaunching()
    target = carbon.GetApplicationEventTarget()
    spec = _EventTypeSpec(int.from_bytes(b"keyb", "big"), 5)  # kEventClassKeyboard, kEventHotKeyPressed
    handler_ref = ctypes.c_void_p()
    err = carbon.InstallEventHandler(target, on_hotkey, 1, ctypes.byref(spec), None, ctypes.byref(handler_ref))
    if err:
        log.error("InstallEventHandler failed: %s", err)
        return 0  # clean exit: launchd only restarts crashes, and retrying won't help
    ref = ctypes.c_void_p()
    err = carbon.RegisterEventHotKey(keycode, mask, _HotKeyID(int.from_bytes(b"hrec", "big"), 1), target, 0,
                                     ctypes.byref(ref))
    if err:
        log.error("RegisterEventHotKey(%s) failed: %s (another app may own this combination)", combo, err)
        return 0  # clean exit, see above; doctor reports the agent as not running
    log.info("hotkey %s registered", combo)
    while True:  # pump the app event queue so Carbon hotkey events are dispatched
        ev = app.nextEventMatchingMask_untilDate_inMode_dequeue_(
            NSEventMaskAny, NSDate.dateWithTimeIntervalSinceNow_(1.0), NSDefaultRunLoopMode, True)
        if ev is not None:
            app.sendEvent_(ev)
        time.sleep(0)  # let toggle threads run
