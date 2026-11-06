"""Hermes Recorder capture helper entry point (runs inside Hermes Recorder Helper.app).

  main.py --socket PATH --token T --apps a,b [--model base.en] [--mic 1] [--log FILE]
  main.py --preflight OUT.json [--request] [--events]

Talks to the plugin over the Unix socket only (launched via `open`, there are no pipes). Exits when the
socket closes. Audio never leaves the process: only transcript text is sent to the plugin.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import resource
import socket
import sys
import threading
import time
from pathlib import Path

if __package__ in (None, ""):  # run as a script: make `helper` importable
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from helper import __version__, dsp, permissions  # noqa: E402
from helper import log as hlog  # noqa: E402
from helper.segments import EchoFilter, keep  # noqa: E402

PROTOCOL_VERSION = 1
SILENCE_RMS = 1e-4
QUEUE_MAX = 8
RETRY_APPS_S = 2.0
STATUS_EVERY_S = 15.0


class Helper:
    def __init__(self, a: argparse.Namespace):
        self.a = a
        self.log = hlog.setup(a.log)
        self.apps = [x for x in (a.apps or "").split(",") if x]
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.send_lock = threading.Lock()
        self.stop_ev = threading.Event()
        self.paused = False
        self.windows = {}   # (kind, app) -> Windower; app is None for the mixed SCK stream
        self.first = {}     # (kind, app) -> next window is the stream's first
        self.q: queue.Queue = queue.Queue(QUEUE_MAX)
        self.echo = EchoFilter()
        self.counters = {"windows": 0, "silent": 0, "dropped_backpressure": 0, "lines": 0, "filtered": 0}
        # audio actually received per stream: "mic", "app" (mixed SCK stream) or a tapped app's bundle id
        self.streams: dict[str, dict] = {}
        self.capturing: list[str] = []
        self.sck_capturing: list[str] = []
        self.audio_path: dict[str, str] = {}  # bundle id -> "tap" | "sck"
        self.capture = None
        self.taps = None
        self.fatal: str | None = None

    # ------------------------------------------------------------------ socket
    def send(self, mtype: str, **fields) -> None:
        line = json.dumps({"v": PROTOCOL_VERSION, "type": mtype, "ts": time.time(), **fields}) + "\n"
        with self.send_lock:
            try:
                self.sock.sendall(line.encode())
            except OSError:
                self.stop_ev.set()

    def reader(self) -> None:
        f = self.sock.makefile("rb")
        try:
            for line in f:
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if msg.get("type") == "control":
                    self.controls.put(msg)
        except OSError:
            pass
        self.stop_ev.set()  # socket closed: the plugin is gone, so exit

    # ------------------------------------------------------------------ audio path
    def on_audio(self, kind: int, x, rate: float, app: str | None = None) -> None:
        """kind 1 = app audio ("them"), 2 = mic ("me"). ``app`` is set for per-app (tapped) streams."""
        if self.paused or self.stop_ev.is_set():
            return
        key = (kind, app)
        st = self.streams.setdefault("mic" if kind == 2 else (app or "app"),
                                     {"samples": 0, "peak": 0.0, "rate": None})
        st["samples"] += len(x)
        st["rate"] = rate
        if len(x):
            st["peak"] = max(st["peak"], float(abs(x).max()))
        w = self.windows.get(key)
        if w is None or w.rate != rate:
            w = self.windows[key] = dsp.Windower(rate)
        for _, win in w.push(x):
            if rate != dsp.TARGET_RATE:
                win = dsp.resample(win, rate)
            self.counters["windows"] += 1
            first = self.first.get(key, True)
            self.first[key] = False
            if dsp.rms(win) < SILENCE_RMS:
                self.counters["silent"] += 1
                continue
            item = (kind, app, time.time(), win, first)
            try:
                self.q.put_nowait(item)
            except queue.Full:
                try:
                    self.q.get_nowait()  # drop the oldest window
                except queue.Empty:
                    pass
                self.counters["dropped_backpressure"] += 1
                self.q.put_nowait(item)

    def attribute_app(self) -> str | None:
        """The SCK stream mixes its apps; attribute its lines (and mic lines) to the frontmost captured app."""
        if len(self.capturing) == 1:
            return self.capturing[0]
        try:
            from AppKit import NSWorkspace
            front = NSWorkspace.sharedWorkspace().frontmostApplication()
            bid = str(front.bundleIdentifier()) if front else None
            if bid in self.capturing:
                return bid
        except Exception:
            pass
        return self.capturing[0] if self.capturing else None

    def worker(self) -> None:
        from helper.transcribe import Transcriber
        try:
            tr = Transcriber(self.a.model)
        except Exception as e:
            self.fatal = (f"Transcription model '{self.a.model}' isn't available ({type(e).__name__}). "
                          "Run `hermes recorder setup`.")
            self.send("error", message=self.fatal)
            self.log.error("model load failed: %s", e)
            return
        self.log.info("model loaded: %s", self.a.model)
        while not self.stop_ev.is_set():
            try:
                kind, src_app, ts, win, first = self.q.get(timeout=0.5)
            except queue.Empty:
                continue
            t0 = time.time()
            try:
                segs = tr.run(win)
            except Exception as e:
                self.log.error("transcribe failed: %s", e)
                continue
            text = " ".join(s.text for s in segs if keep(s, first)).strip()
            self.counters["filtered"] += sum(1 for s in segs if not keep(s, first))
            if not text:
                continue
            speaker = "them" if kind == 1 else "me"
            if speaker == "them":
                self.echo.add_them(ts, text)
            elif self.echo.is_echo(ts, text):
                self.counters["filtered"] += 1
                continue
            app = src_app or self.attribute_app()
            if app is None:
                continue
            self.send("transcript", app=app, speaker=speaker, text=text, proc_s=round(time.time() - t0, 3))
            self.counters["lines"] += 1

    # ------------------------------------------------------------------ control
    def split_apps(self) -> tuple[list[str], list[str]]:
        """(tap apps, ScreenCaptureKit apps). Taps need macOS 14.2+; Safari's audio can't be attributed to it."""
        from helper.capture_tap import taps_supported
        from helper.proc_match import SCK_ONLY
        tap_ok = taps_supported()
        taps = [b for b in self.apps if tap_ok and b not in SCK_ONLY]
        return taps, [b for b in self.apps if b not in taps]

    def apply_apps(self) -> None:
        """Bring both capture paths in line with self.apps and recompute what is being captured."""
        tap_apps, sck_apps = self.split_apps()
        self.sck_capturing = self.capture.update_apps(sck_apps) if self.capture else []
        tapped = self.taps.update_apps(tap_apps) if self.taps else []
        self.audio_path = {**{b: "tap" for b in tap_apps}, **{b: "sck" for b in sck_apps}}
        self.capturing = [b for b in self.apps if b in tapped or b in self.sck_capturing]

    def start_capture(self) -> None:
        from helper.capture_audio import AudioCapture
        from helper.capture_tap import TapCapture
        if self.capture is None:
            self.capture = AudioCapture(self.on_audio, mic=bool(int(self.a.mic)))
        if self.taps is None:
            self.taps = TapCapture(self.on_audio)
        tap_apps, sck_apps = self.split_apps()
        try:
            self.sck_capturing = self.capture.start(sck_apps)
        except PermissionError as e:
            self.fatal = (f"Screen & System Audio Recording isn't allowed for Hermes Recorder Helper ({e}). "
                          "Run `hermes recorder doctor` for the fix.")
            self.send("error", message=self.fatal)
            return
        tapped = self.taps.start(tap_apps)
        self.audio_path = {**{b: "tap" for b in tap_apps}, **{b: "sck" for b in sck_apps}}
        self.capturing = [b for b in self.apps if b in tapped or b in self.sck_capturing]
        self.windows.clear()
        self.first.clear()
        self.log.info("capturing %s (mic=%s, audio_path=%s)", self.capturing, self.capture.mic, self.audio_path)

    def stop_capture(self) -> None:
        if self.capture:
            self.capture.stop()  # turns the macOS recording indicator off
        if self.taps:
            self.taps.stop()
        self.capturing = []
        self.sck_capturing = []

    def handle(self, msg: dict) -> None:
        op = msg.get("op")
        self.log.info("control %s", op)
        if op == "pause" and not self.paused:
            self.paused = True
            self.stop_capture()
        elif op == "resume" and self.paused:
            self.paused = False
            self.start_capture()
        elif op == "set_apps":
            self.apps = [str(x) for x in msg.get("apps") or []]
            if not self.paused and self.capture is not None:
                self.apply_apps()
        elif op == "stop":
            self.stop_ev.set()

    def status(self) -> None:
        r = resource.getrusage(resource.RUSAGE_SELF)
        missing = [a for a in self.apps if a not in self.capturing]
        streams = {k: {"seconds": round(v["samples"] / v["rate"], 1) if v["rate"] else 0.0,
                       "peak": round(v["peak"], 4)} for k, v in self.streams.items()}
        fields = dict(capturing=self.capturing, missing_apps=missing, paused=self.paused, audio_path=self.audio_path,
                      mic=bool(self.capture and self.capture.mic), counters=dict(self.counters), streams=streams,
                      cpu_s=round(r.ru_utime + r.ru_stime, 1), rss_mb=round(r.ru_maxrss / 1048576, 1))
        self.send("status", **fields)
        self.log.info("status %s", json.dumps(fields))
        for v in self.streams.values():
            v["peak"] = 0.0  # peak per status interval

    def run(self) -> int:
        from helper.capture_audio import mic_supported, pump
        self.controls: queue.Queue = queue.Queue()
        self.sock.connect(self.a.socket)
        self.send("hello", pid=os.getpid(), helper_version=__version__, token=self.a.token,
                  permissions=permissions.status(), mic_supported=mic_supported(),
                  python=sys.version.split()[0])
        threading.Thread(target=self.reader, name="reader", daemon=True).start()
        threading.Thread(target=self.worker, name="transcribe", daemon=True).start()
        self.start_capture()
        last_retry = last_status = time.time()
        while not self.stop_ev.is_set():
            pump(0.1)
            while not self.controls.empty():
                self.handle(self.controls.get_nowait())
            now = time.time()
            if self.taps is not None and self.taps.changed.is_set():
                last_retry = 0.0  # a process started or stopped using audio: re-resolve taps now
                self.taps.changed.clear()
            if not self.fatal and not self.paused and now - last_retry > RETRY_APPS_S and self.capture:
                last_retry = now  # re-resolve tapped processes (calls start, helpers spawn) and retry missing apps
                try:
                    if set(self.capturing) != set(self.apps):
                        self.apply_apps()
                    elif self.taps:
                        tapped = self.taps.refresh()
                        self.capturing = [b for b in self.apps if b in tapped or b in self.sck_capturing]
                except Exception as e:
                    self.log.warning("app retry failed: %s", e)
            if now - last_status > STATUS_EVERY_S:
                last_status = now
                self.status()
        self.stop_capture()
        try:
            self.sock.close()
        except OSError:
            pass
        self.log.info("exit: %s", self.counters)
        return 0


def preflight(out: str, request: bool, events: bool) -> int:
    from helper.capture_audio import mic_supported, pump
    if request:
        permissions.request(events, pump)
        pump(0.5)
    result = {"ts": time.time(), "pid": os.getpid(), "executable": sys.executable,
              "permissions": permissions.status(), "mic_supported": mic_supported(),
              "helper_version": __version__}
    try:
        from helper.capture_audio import shareable_apps
        shareable_apps()
        result["screen_capture_works"] = True
    except Exception as e:
        result["screen_capture_works"] = False
        result["screen_capture_error"] = str(e)
    tmp = out + ".tmp"
    Path(tmp).write_text(json.dumps(result))
    os.replace(tmp, out)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--socket")
    ap.add_argument("--token")
    ap.add_argument("--apps", default="")
    ap.add_argument("--model", default="base.en")
    ap.add_argument("--mic", default="1")
    ap.add_argument("--log")
    ap.add_argument("--preflight")
    ap.add_argument("--request", action="store_true")
    ap.add_argument("--events", action="store_true")
    ap.add_argument("--hotkey", help="run only the global hotkey listener, e.g. ctrl+opt+r")
    ap.add_argument("--data-dir", help="plugin data dir (hotkey mode)")
    a = ap.parse_args(argv)
    if a.preflight:
        return preflight(a.preflight, a.request, a.events)
    if a.hotkey:
        hlog.setup(a.log)
        from helper import hotkey
        return hotkey.run(a.hotkey, a.data_dir)
    if not (a.socket and a.token):
        ap.error("--socket and --token are required")
    return Helper(a).run()


if __name__ == "__main__":
    sys.exit(main())
