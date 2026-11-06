"""Notice when a call starts or ends, so the recorder panel can *offer* to record it (it never records on its own).

A call is an app whose processes are reading the microphone: CoreAudio's per-process "is running input" flag
(macOS 14+). That's a flag, not audio: nothing is captured here. FaceTime and iPhone calls read the mic in
``avconferenced``; Zoom, Teams, Slack, Discord and browsers (Google Meet) in their own processes or helpers.
The pure part (which processes count, debouncing) is testable anywhere; ``CoreAudioInputs`` reads the flags.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Iterable

log = logging.getLogger("hermes_recorder_helper")

# process bundle id (or its prefix before a dot) -> the app the user would record
CALL_APPS: dict[str, str] = {
    "com.apple.avconferenced": "com.apple.FaceTime",  # FaceTime and relayed iPhone calls
    "us.zoom.xos": "us.zoom.xos", "us.zoom.caphost": "us.zoom.xos",
    "com.microsoft.teams2": "com.microsoft.teams2",
    "com.tinyspeck.slackmacgap": "com.tinyspeck.slackmacgap",
    "com.hnc.Discord": "com.hnc.Discord",
    "Cisco-Systems.Spark": "Cisco-Systems.Spark",
    "net.whatsapp.WhatsApp": "net.whatsapp.WhatsApp",
    "org.whispersystems.signal-desktop": "org.whispersystems.signal-desktop",
    # browsers: Google Meet and other web calls (the mic is read in a helper process)
    "com.google.Chrome": "com.google.Chrome", "com.microsoft.edgemac": "com.microsoft.edgemac",
    "company.thebrowser.Browser": "company.thebrowser.Browser", "com.brave.Browser": "com.brave.Browser",
    "org.mozilla.firefox": "org.mozilla.firefox",
}
OWN_PREFIX = "io.github.michaelyouuu.hermes-recorder"
START_S = 3.0   # mic in use this long before we call it a call (skips a quick dictation blip)
END_S = 8.0     # mic idle this long before the call counts as over (mute toggles, reconnects)


def call_app(process_bundle_id: str) -> str | None:
    """The app a mic-reading process belongs to, if it's a call-capable app."""
    bid = process_bundle_id or ""
    if bid.startswith(OWN_PREFIX):
        return None
    if bid in CALL_APPS:
        return CALL_APPS[bid]
    for key, app in CALL_APPS.items():
        if bid.startswith(key + "."):
            return app
    return None


def active_calls(mic_process_ids: Iterable[str]) -> set[str]:
    return {a for a in (call_app(b) for b in mic_process_ids) if a}


class CallTracker:
    """Debounces the set of apps using the mic into ("start", app) / ("end", app) events."""

    def __init__(self, start_s: float = START_S, end_s: float = END_S):
        self.start_s, self.end_s = start_s, end_s
        self._seen: dict[str, float] = {}      # app -> first time seen using the mic (not yet a call)
        self._calls: dict[str, float] = {}     # app -> last time seen using the mic (a call)

    @property
    def calls(self) -> set[str]:
        return set(self._calls)

    def update(self, now: float, active: set[str]) -> list[tuple[str, str]]:
        events: list[tuple[str, str]] = []
        for app in active:
            if app in self._calls:
                self._calls[app] = now
            else:
                first = self._seen.setdefault(app, now)
                if now - first >= self.start_s:
                    self._seen.pop(app, None)
                    self._calls[app] = now
                    events.append(("start", app))
        for app in [a for a in self._seen if a not in active]:
            self._seen.pop(app)
        for app, last in list(self._calls.items()):
            if app not in active and now - last >= self.end_s:
                self._calls.pop(app)
                events.append(("end", app))
        return events


class CoreAudioInputs:
    """Bundle ids of processes currently reading audio input (CoreAudio process objects, macOS 14+)."""

    def __init__(self) -> None:
        import ctypes
        from helper import capture_tap as ct
        self._ct, self._ctypes = ct, ctypes
        self._procs = ct.ProcessList()
        self._run_in = int.from_bytes(b"piri", "big")  # kAudioProcessPropertyIsRunningInput

    def __call__(self) -> list[str]:
        out = []
        for p in self._procs.snapshot():
            err, v = self._ct._get(p.obj, self._run_in, self._ctypes.c_uint32)
            if not err and v.value:
                out.append(p.bundle_id)
        return out


class CallWatcher:
    """Polls mic use every ``every`` seconds on a daemon thread and calls ``on_event(kind, app)``."""

    def __init__(self, on_event: Callable[[str, str], None], inputs: Callable[[], list[str]] | None = None,
                 every: float = 2.0):
        self.on_event, self.every = on_event, every
        self.inputs = inputs
        self.tracker = CallTracker()
        self._stop = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._loop, name="call-watch", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        try:
            self.inputs = self.inputs or CoreAudioInputs()
        except Exception as e:
            log.warning("call detection unavailable: %s", e)
            return
        log.info("call detection on")
        while not self._stop.wait(self.every):
            try:
                for kind, app in self.tracker.update(time.time(), active_calls(self.inputs())):
                    log.info("call %s: %s", kind, app)
                    self.on_event(kind, app)
            except Exception as e:
                log.debug("call watch poll failed: %s", e)
