"""Process-wide recorder state, created lazily on first use (never in register())."""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .buffer import RingBuffer
from .helper_manager import AppLauncher, HelperManager, LaunchError, SubprocessLauncher

log = logging.getLogger(__name__)

PLUGIN_NAME = "hermes-recorder"
PLUGIN_ROOT = Path(__file__).resolve().parent.parent
DEFAULTS: dict[str, Any] = {
    "apps": [], "buffer_minutes": 30, "whisper_model": "base.en", "capture_mic": True,
    "capture_events": False, "capture_snapshots": False, "autostart": False,
}
APP_NAMES = {
    "com.google.Chrome": "Google Chrome", "com.microsoft.edgemac": "Microsoft Edge", "com.apple.Safari": "Safari",
    "company.thebrowser.Browser": "Arc", "org.mozilla.firefox": "Firefox", "com.brave.Browser": "Brave",
    "us.zoom.xos": "Zoom", "com.apple.FaceTime": "FaceTime", "com.microsoft.teams2": "Microsoft Teams",
    "com.tinyspeck.slackmacgap": "Slack", "com.hnc.Discord": "Discord", "com.apple.Notes": "Notes",
    "com.spotify.client": "Spotify", "com.apple.podcasts": "Podcasts", "com.apple.dock": "Dock",
}
# Dev-only: replay a fixture through the fake helper instead of the real one (tests and the M1 live check).
DEV_FIXTURE_ENV = "HERMES_RECORDER_DEV_FIXTURE"

_ctx = None
_recorder: "Recorder | None" = None
_lock = threading.Lock()


def app_name(bundle_id: str | None) -> str:
    if not bundle_id:
        return "unknown app"
    return APP_NAMES.get(bundle_id, bundle_id.rsplit(".", 1)[-1])


def bind_context(ctx) -> None:
    """Remember the plugin context for config lookups. Called from register(); does nothing else."""
    global _ctx
    _ctx = ctx


def config(key: str) -> Any:
    default = DEFAULTS.get(key)
    if _ctx is None:
        return default
    try:
        value = _ctx.get_config(key, default)
    except Exception:
        return default
    return default if value is None else value


def data_dir() -> Path:
    try:
        from plugins.plugin_storage import plugin_data_dir  # documented Hermes plugin API
        return Path(plugin_data_dir(PLUGIN_NAME))
    except Exception:
        home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
        return home / "plugin-data" / PLUGIN_NAME


def _dev_launcher() -> SubprocessLauncher | None:
    fixture = os.environ.get(DEV_FIXTURE_ENV)
    if not fixture:
        return None
    fx = Path(fixture).resolve()
    allowed = (PLUGIN_ROOT / "tests" / "fixtures").resolve()
    if allowed not in fx.parents or not fx.is_file():
        log.warning("hermes-recorder: ignoring %s outside tests/fixtures", DEV_FIXTURE_ENV)
        return None
    fake = str(PLUGIN_ROOT / "tests" / "fake_helper.py")
    return SubprocessLauncher(lambda sock, tok, opts: [sys.executable, fake, "--socket", sock, "--token", tok,
                                                       "--fixture", str(fx), "--delay", "0.05", "--hold"])


class Recorder:
    def __init__(self, data: Path | None = None, launcher=None):
        self.data_dir = Path(data) if data else data_dir()
        self.buffer = RingBuffer(max_minutes=int(config("buffer_minutes") or 30))
        self.manager = HelperManager(self.buffer, self.data_dir,
                                     launcher or _dev_launcher() or AppLauncher(self.data_dir))
        self._autostart_tried = False
        self.manager.on_status = self.write_status  # keep `hermes recorder status` current (every ~15 s)

    # ------------------------------------------------------------------ actions
    def start(self, apps: list[str] | None = None) -> None:
        apps = list(apps or config("apps") or [])
        opts = {k: config(k) for k in ("whisper_model", "capture_mic", "capture_events", "capture_snapshots")}
        try:
            self.manager.start(apps, opts)
        finally:
            self.write_status()

    def stop(self) -> None:
        self.manager.stop()
        self.write_status()

    def pause(self) -> None:
        self.manager.pause()
        self.write_status()

    def resume(self) -> None:
        self.manager.resume()
        self.write_status()

    def set_apps(self, apps: list[str]) -> None:
        self.manager.set_apps(apps)
        self.write_status()

    def forget(self) -> None:
        """Clear the live buffer (saved clips arrive with SQLite storage later)."""
        self.buffer.clear()
        self.write_status()

    def maybe_autostart(self) -> None:
        if self._autostart_tried or not config("autostart") or self.manager.state != "stopped":
            return
        self._autostart_tried = True
        try:
            self.start()
        except LaunchError as e:
            log.info("hermes-recorder: autostart failed: %s", e)

    # ------------------------------------------------------------------ status
    def status(self) -> dict[str, Any]:
        m = self.manager.status()
        b = self.buffer.stats()
        span = self.buffer.span()
        return {
            "running": m["state"] in ("running", "paused"), "state": m["state"],
            "apps": [{"bundle_id": a, "name": app_name(a)} for a in m["apps"]],
            "buffer": {"minutes_available": round(b["span_s"] / 60, 1), "max_minutes": b["max_minutes"],
                       "transcript_lines": b["counts"].get("transcript", 0),
                       "ui_events": b["counts"].get("ui_event", 0),
                       "newest_age_s": round(time.time() - span[1], 1) if span else None},
            "helper": m["helper"], "error": m["error"],
            "settings": {k: config(k) for k in ("whisper_model", "capture_mic", "capture_events", "autostart")},
            "protocol_version": m["protocol_version"],
        }

    def write_status(self) -> None:
        """Snapshot for `hermes recorder status`, which runs in a different process."""
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.data_dir / "status.json.tmp"
            tmp.write_text(json.dumps({**self.status(), "written_at": time.time(), "pid": os.getpid()}))
            os.replace(tmp, self.data_dir / "status.json")
        except Exception as e:
            log.debug("hermes-recorder: could not write status: %s", e)

    def live_note(self) -> str | None:
        """One line for pre_llm_call, built from in-memory state only."""
        if self.manager.state != "running":
            return None
        names = ", ".join(app_name(a) for a in self.manager.apps) or "no apps"
        span = self.buffer.span()
        minutes = max(1, round((time.time() - span[0]) / 60)) if span else 0
        avail = f"last {minutes} min" if minutes else "nothing captured yet"
        mic = " (incl. your narration)" if config("capture_mic") else ""
        teach = "; teach with recorder_draft_skill" if config("capture_events") else ""
        return f"[Recorder: live capture of {names}; {avail}{mic} via screen_recall{teach}]"


def get() -> Recorder:
    global _recorder
    with _lock:
        if _recorder is None:
            _recorder = Recorder()
        return _recorder


def peek() -> Recorder | None:
    """The recorder if it exists; never creates one (for hooks)."""
    return _recorder


def shutdown() -> None:
    global _recorder
    with _lock:
        r, _recorder = _recorder, None
    if r is not None:
        try:
            r.stop()
        except Exception as e:
            log.warning("hermes-recorder: error during unload: %s", e)


def reset_for_tests(recorder: Recorder | None = None) -> None:
    global _recorder
    _recorder = recorder
