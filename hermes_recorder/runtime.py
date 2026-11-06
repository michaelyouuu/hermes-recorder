"""Process-wide recorder state, created lazily on first use (never in register())."""
from __future__ import annotations

import contextvars
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
    "apps": [], "buffer_minutes": 90, "whisper_model": "base.en", "capture_mic": True,
    "capture_events": False, "capture_snapshots": False, "autostart": False, "hotkey": "ctrl+opt+r",
    "notes_dir": "~/Documents/Hermes Notes",
}
APP_NAMES = {
    "com.google.Chrome": "Google Chrome", "com.microsoft.edgemac": "Microsoft Edge", "com.apple.Safari": "Safari",
    "company.thebrowser.Browser": "Arc", "org.mozilla.firefox": "Firefox", "com.brave.Browser": "Brave",
    "us.zoom.xos": "Zoom", "com.apple.FaceTime": "FaceTime", "com.microsoft.teams2": "Microsoft Teams",
    "com.tinyspeck.slackmacgap": "Slack", "com.hnc.Discord": "Discord", "com.apple.Notes": "Notes",
    "com.spotify.client": "Spotify", "com.apple.podcasts": "Podcasts", "com.apple.dock": "Dock",
    "com.apple.mobilephone": "Phone", "net.whatsapp.WhatsApp": "WhatsApp", "Cisco-Systems.Spark": "Webex",
    "org.whispersystems.signal-desktop": "Signal",
}
# Short names accepted wherever apps are given ("/recorder on facetime zoom"), besides full app names.
APP_ALIASES = {"chrome": "com.google.Chrome", "edge": "com.microsoft.edgemac", "teams": "com.microsoft.teams2",
               "zoom": "us.zoom.xos", "meet": "com.google.Chrome", "phone": "com.apple.mobilephone"}


def _key(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())


_BY_NAME = {**{_key(n): b for b, n in APP_NAMES.items()}, **{_key(a): b for a, b in APP_ALIASES.items()}}


def resolve_apps(apps: list[str]) -> list[str]:
    """Map friendly names (FaceTime, zoom, "Microsoft Edge") to bundle ids; keep anything else as given."""
    out: list[str] = []
    for a in apps:
        bid = _BY_NAME.get(_key(a), a)
        if bid not in out:
            out.append(bid)
    return out
# Dev-only: replay a fixture through the fake helper instead of the real one (tests and the M1 live check).
DEV_FIXTURE_ENV = "HERMES_RECORDER_DEV_FIXTURE"

_ctx = None
_recorder: "Recorder | None" = None
_lock = threading.Lock()
_control = None  # control.ControlServer once started in the gateway
_control_tried = False
# Turns from these surfaces run in a short-lived CLI/TUI process with its own buffer, not the gateway.
NON_GATEWAY_PLATFORMS = frozenset({"cli", "tui", "acp", ""})
LAST_APPS_KEY = "last_apps"


def app_name(bundle_id: str | None) -> str:
    if not bundle_id:
        return "unknown app"
    return APP_NAMES.get(bundle_id, bundle_id.rsplit(".", 1)[-1])


def bind_context(ctx) -> None:
    """Remember the plugin context for config lookups. Called from register(); does nothing else."""
    global _ctx
    _ctx = ctx


def llm() -> Any:
    """The plugin's ``ctx.llm`` (the user's Hermes model), or None."""
    return getattr(_ctx, "llm", None)


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
        self.buffer = RingBuffer(max_minutes=int(config("buffer_minutes") or 90))
        self.manager = HelperManager(self.buffer, self.data_dir,
                                     launcher or _dev_launcher() or AppLauncher(self.data_dir))
        self._autostart_tried = False
        self.started_at: float | None = None  # when capture last started (the panel shows elapsed time)
        self.last_started_at: float | None = None  # survives stop, so Save notes can cover the last session
        self.manager.on_status = self.write_status  # keep `hermes recorder status` current (every ~15 s)

    # ------------------------------------------------------------------ actions
    def start(self, apps: list[str] | None = None) -> None:
        apps = resolve_apps(list(apps or remembered_apps() or config("apps") or []))
        opts = {k: config(k) for k in ("whisper_model", "capture_mic", "capture_events", "capture_snapshots")}
        try:
            self.manager.start(apps, opts)
            self.started_at = self.last_started_at = time.time()
            remember_apps(self.manager.apps)
        finally:
            self.write_status()

    def stop(self) -> None:
        self.manager.stop()
        self.started_at = None
        self.write_status()

    def pause(self) -> None:
        self.manager.pause()
        self.write_status()

    def resume(self) -> None:
        self.manager.resume()
        self.write_status()

    def set_apps(self, apps: list[str]) -> None:
        self.manager.set_apps(resolve_apps(apps))
        remember_apps(self.manager.apps)
        self.write_status()

    def toggle(self, apps: list[str] | None = None) -> None:
        if self.manager.state in ("running", "paused", "starting"):
            self.stop()
        else:
            self.start(apps)

    def forget(self) -> None:
        """Clear the live buffer and the summaries made from it (saved notes are files the user owns)."""
        from . import digest
        self.buffer.clear()
        digest.clear_cache()
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


_memory_apps: list[str] = []


# Context of the latest gateway turn. On a multi-profile gateway, Hermes only resolves credentials inside a turn's
# context (a contextvar secret scope); the panel's ask runs on our control-socket thread, so it reuses a copy.
_turn_context: contextvars.Context | None = None


def remember_turn_context(platform: str | None) -> None:
    global _turn_context
    if (platform or "") not in NON_GATEWAY_PLATFORMS:
        _turn_context = contextvars.copy_context()


def run_in_turn_context(fn, *args, **kwargs):
    """Run ``fn`` in a copy of the latest turn's context (or the current one if there was no turn yet)."""
    snap = _turn_context
    return snap.copy().run(fn, *args, **kwargs) if snap is not None else fn(*args, **kwargs)


def has_turn_context() -> bool:
    return _turn_context is not None


def remembered_apps() -> list[str]:
    try:
        if _ctx is not None:
            v = _ctx.state.get(LAST_APPS_KEY, default=None)
            if isinstance(v, list):
                return [str(a) for a in v]
    except Exception:
        pass
    return list(_memory_apps)


def remember_apps(apps: list[str]) -> None:
    global _memory_apps
    _memory_apps = list(apps)
    try:
        if _ctx is not None:
            _ctx.state.set(LAST_APPS_KEY, list(apps))
    except Exception:
        pass


FEED_MAX = 200        # transcript lines per panel feed
FEED_OVERLAP_S = 15   # resend this much before `since`: mic and app lines can arrive out of order


def _control_handle(op: str, apps: list[str] | None, fallback_apps: list[str] | None = None,
                    args: dict[str, Any] | None = None) -> dict[str, Any]:
    """``fallback_apps`` (the hotkey sends the frontmost app) is used only if no apps are known yet."""
    from .helper_manager import LaunchError
    rec = get()
    args = args or {}
    if not apps and fallback_apps and not (remembered_apps() or config("apps")):
        apps = fallback_apps
    started = False
    try:
        if op == "toggle":
            rec.toggle(apps)
        elif op == "on":
            rec.start(apps)
        elif op == "off":
            rec.stop()
        elif op == "summon":  # hotkey: start if off; the panel is shown either way
            if rec.manager.state not in ("running", "paused", "starting"):
                rec.start(apps)
                started = True
        elif op == "pause":
            rec.pause()
        elif op == "resume":
            rec.resume()
        elif op == "feed":
            return {**_brief(rec), **_feed(rec, args.get("since"))}
        elif op == "diag":  # `hermes recorder check`: streams and lines per source since `since`
            st = rec.status()
            try:
                since = float(args.get("since") or 0)
            except (TypeError, ValueError):
                since = 0.0
            counts: dict[str, int] = {}
            for e in rec.buffer.slice(since, time.time() + 1, ("transcript",)):
                k = "mic" if e.speaker == "me" else (e.app or "app")
                counts[k] = counts.get(k, 0) + 1
            return {**_brief(rec), "bundle_ids": [a["bundle_id"] for a in st["apps"]],
                    "helper": st["helper"], "lines_by_source": counts}
        elif op == "notes":
            from .notes import save
            return save(args.get("minutes"), args.get("title_hint"), rec=rec)
        elif op == "ask":
            from .ask import answer
            return answer(str(args.get("question") or ""), args.get("minutes") or 5, rec=rec, history=args.get("history"))
    except LaunchError as e:
        return {**_brief(rec), "error": str(e), "started": False}
    out = _brief(rec)
    if op == "summon":
        out["started"] = started
    return out


def _feed(rec: Recorder, since: Any) -> dict[str, Any]:
    now = time.time()
    try:
        t0 = float(since) - FEED_OVERLAP_S
    except (TypeError, ValueError):
        t0 = 0.0
    t0 = max(t0, now - rec.buffer.max_age_s)
    entries = rec.buffer.slice(t0, now + 1, ("transcript",))[-FEED_MAX:]
    lines = [{"ts": round(e.ts, 3), "speaker": e.speaker, "app": app_name(e.app), "text": e.text} for e in entries]
    return {"now": now, "started_at": rec.started_at, "lines": lines}


def _brief(rec: Recorder) -> dict[str, Any]:
    st = rec.status()
    last = rec.last_started_at
    session = [e for e in rec.buffer.slice(last, time.time() + 1, ("transcript",))] if last else []
    return {"state": st["state"], "apps": [a["name"] for a in st["apps"]], "error": st["error"],
            "minutes_available": st["buffer"]["minutes_available"], "started_at": rec.started_at,
            "session_lines": len(session),
            "session_minutes": round((session[-1].ts - session[0].ts) / 60, 1) if session else 0,
            "now": time.time()}


def _socket_is_served(path: Path) -> bool:
    import socket as _s
    if not path.exists():
        return False
    c = _s.socket(_s.AF_UNIX, _s.SOCK_STREAM)
    c.settimeout(0.3)
    try:
        c.connect(str(path))
        return True
    except OSError:
        return False
    finally:
        c.close()


def ensure_control(platform: str | None = None, in_gateway: bool = False) -> None:
    """Start the hotkey control socket the first time the plugin is active in the gateway. Never raises.

    Called from hooks and tools, never from register(). CLI/TUI turns run in their own short-lived process
    with a separate buffer, so they must not take the socket.
    """
    global _control, _control_tried
    if _control is not None or _control_tried:
        return
    if not in_gateway and (platform is None or str(platform).lower() in NON_GATEWAY_PLATFORMS):
        return
    with _lock:
        if _control is not None or _control_tried:
            return
        _control_tried = True
        try:
            from .control import ControlServer, paths
            d = data_dir()
            if _socket_is_served(paths(d)[0]):
                log.info("hermes-recorder: control socket already served by another process")
                return
            srv = ControlServer(d, _control_handle)
            srv.start()
            _control = srv
        except Exception as e:
            log.warning("hermes-recorder: control socket unavailable: %s", e)


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
    global _recorder, _control, _control_tried
    with _lock:
        r, _recorder = _recorder, None
        c, _control = _control, None
        _control_tried = False
    if c is not None:
        try:
            c.stop()
        except Exception:
            pass
    if r is not None:
        try:
            r.stop()
        except Exception as e:
            log.warning("hermes-recorder: error during unload: %s", e)


def reset_for_tests(recorder: Recorder | None = None) -> None:
    global _recorder, _control_tried, _memory_apps, _turn_context
    _turn_context = None
    _recorder = recorder
    _control_tried = False
    _memory_apps = []
