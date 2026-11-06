"""`hermes recorder …` CLI. Runs in its own process, so live state comes from status.json."""
from __future__ import annotations

import json
import subprocess
import time

import sys

from . import audio_procs, control, doctor, guide, hotkey_agent, runtime, setup

NOT_LOADED = ("Hermes hasn't loaded the recorder in the gateway yet (it starts listening on the first "
              "message or chat after a restart). Send Hermes any message, then retry.")


def setup_parser(parser) -> None:
    sub = parser.add_subparsers(dest="recorder_cmd")
    sub.add_parser("status", help="Show capture status (as last written by the running gateway)")
    p = sub.add_parser("setup", help="Build the capture helper app, download the model, request permissions")
    p.add_argument("--force", action="store_true", help="Rebuild even if up to date")
    p.add_argument("--no-prompts", action="store_true", help="Don't trigger the macOS permission prompts")
    hk = p.add_mutually_exclusive_group()
    hk.add_argument("--hotkey", dest="hotkey_choice", action="store_const", const=True,
                    help="Install the global recording hotkey without asking")
    hk.add_argument("--no-hotkey", dest="hotkey_choice", action="store_const", const=False,
                    help="Don't install the global recording hotkey")
    sub.add_parser("doctor", help="Check permissions and helper health, and fix what's missing")
    tm = sub.add_parser("teach", help="Turn teach mode (clicks and typing in captured apps) on or off")
    tm.add_argument("action", choices=["on", "off"])
    on = sub.add_parser("on", help="Start capture in the running gateway (last apps if none given)")
    on.add_argument("apps", nargs="*", help="Bundle ids, e.g. com.google.Chrome")
    sub.add_parser("off", help="Stop capture in the running gateway")
    t = sub.add_parser("toggle", help="Start or stop capture (what the global hotkey runs)")
    t.add_argument("apps", nargs="*", help="Bundle ids to use when starting")
    c = sub.add_parser("check", help="Record an app briefly and report whether its audio and transcript arrive")
    c.add_argument("app", nargs="?", help="App name or bundle id, e.g. zoom, chrome, 'Microsoft Teams' "
                                          "(default: whatever is being captured now)")
    c.add_argument("--seconds", type=float, default=30, help="How long to listen (default 30)")
    sp = sub.add_parser("steps", help="Show the clicks and typing teach mode recorded (what Hermes would learn from)")
    sp.add_argument("--minutes", type=float, default=5, help="How far back (default 5)")
    h = sub.add_parser("hotkey", help="Install, remove or check the global recording hotkey")
    h.add_argument("action", choices=["on", "off", "status"])
    h.add_argument("--combo", help="Key combination, e.g. ctrl+opt+r (default: the 'hotkey' setting)")


def _status() -> int:
    path = runtime.data_dir() / "status.json"
    if not path.exists():
        print("hermes-recorder: no status yet (capture has never been started in this profile).")
        return 0
    st = json.loads(path.read_text())
    age = time.time() - st.get("written_at", 0)
    apps = ", ".join(a["name"] for a in st.get("apps", [])) or "none"
    print(f"state: {st['state']} (as of {age:.0f} s ago, pid {st.get('pid')})")
    print(f"apps: {apps}")
    print(f"buffer: {st['buffer']['minutes_available']} min, {st['buffer']['transcript_lines']} lines")
    if st.get("error"):
        print(f"error: {st['error']}")
    return 0


def _setup(args) -> int:
    probs = setup.platform_problems()
    if probs:
        print("\n".join(probs))
        return 1
    paths = setup.Paths(runtime.data_dir())
    try:
        setup.build_runtime(paths, force=getattr(args, "force", False))
        setup.download_model(paths, str(runtime.config("whisper_model") or "base.en"))
    except subprocess.CalledProcessError as e:
        print(f"Setup failed running {e.cmd[0]}:\n{(e.stderr or e.stdout or '').strip()[-2000:]}")
        return 1
    events = bool(runtime.config("capture_events"))
    if not getattr(args, "no_prompts", False):
        print("Permissions (macOS asks once, for “Hermes Recorder Helper”):")
        guide.ensure(paths, guide.BASIC + (guide.TEACH if events else ()))
    _offer_hotkey(paths, getattr(args, "hotkey_choice", None))
    text, code = doctor.render(doctor.check(paths, needs_events=events))
    print(text)
    if code:
        print("\nRun `hermes recorder doctor` to finish.")
    else:
        print("\nReady. Press ⌃⌥R in any app (or join a call) and Hermes takes it from there."
              + ("" if events else "\nWant Hermes to learn tasks from what you do? `hermes recorder teach on`"))
    return code


def _combo(override: str | None = None) -> str:
    return str(override or runtime.config("hotkey") or hotkey_agent.DEFAULT_COMBO)


def _offer_hotkey(paths: setup.Paths, choice: bool | None) -> None:
    combo = _combo()
    st = hotkey_agent.status()
    if choice is None and st["installed"]:
        choice = True  # keep an existing hotkey current (paths or key may have changed)
    if choice is None:
        if not sys.stdin.isatty():
            print(f"Tip: `hermes recorder hotkey on` adds a {hotkey_agent.pretty(combo)} start/stop hotkey.")
            return
        ans = input(f"Enable the {hotkey_agent.pretty(combo)} hotkey to start/stop recording from any app? "
                    "It adds a small login item (no extra permissions). [Y/n] ").strip().lower()
        choice = ans in ("", "y", "yes")
    if choice:
        _hotkey_on(paths, combo)


def _hotkey_on(paths: setup.Paths, combo: str) -> int:
    try:
        hotkey_agent.install(paths, combo)
    except ValueError as e:
        print(f"Invalid hotkey {combo!r}: {e}")
        return 1
    time.sleep(1.0)
    st = hotkey_agent.status()
    if st["running"]:
        print(f"Hotkey {hotkey_agent.pretty(combo)} is on. Press it in any app to open the recorder panel; "
              "it starts recording that app if capture is off (Tink = started, Basso = problem).")
        return 0
    print(f"Hotkey installed but not running. Another app may own {hotkey_agent.pretty(combo)}; "
          "set a different 'hotkey' and re-run, or see ~/.hermes/plugin-data/hermes-recorder/logs/hotkey.log.")
    return 1


def _hotkey(args) -> int:
    paths = setup.Paths(runtime.data_dir())
    if args.action == "off":
        hotkey_agent.uninstall()
        print("Hotkey removed.")
        return 0
    if args.action == "status":
        st = hotkey_agent.status()
        if not st["installed"]:
            print("Hotkey: not installed (`hermes recorder hotkey on`).")
        else:
            print(f"Hotkey: {hotkey_agent.pretty(st['combo'] or '?')} · "
                  f"{'running' if st['running'] else 'installed, not running'}")
        return 0
    ok, why = setup.build_is_current(paths, setup.host_python())
    if not ok:
        print(f"Run `hermes recorder setup` first ({why}).")
        return 1
    return _hotkey_on(paths, _combo(args.combo))


def _control(op: str, apps: list[str] | None) -> int:
    try:
        r = control.request(runtime.data_dir(), op, apps or None)
    except ConnectionError:
        print(NOT_LOADED)
        return 1
    apps_s = ", ".join(r.get("apps") or []) or "no apps"
    if r.get("error"):
        print(f"Recorder: {r.get('state', 'error')} · {r['error']}")
        return 1
    print(f"Recorder: {r.get('state')} · {apps_s}")
    return 0


CHECK_POLL_S = 2.0
SILENT_PEAK = 0.002


def _check(app: str | None, seconds: float, req=None, sleep=time.sleep, out=print) -> int:
    """Start capture of ``app`` (unless capture is already running), watch audio and lines, then restore."""
    req = req or (lambda op, **kw: control.request(runtime.data_dir(), op, timeout=60, **kw))
    try:
        st = req("diag")
    except ConnectionError:
        out(NOT_LOADED)
        return 1
    running = st.get("state") in ("running", "paused", "starting")
    started = False
    if running:
        if app and runtime.resolve_apps([app])[0] not in (st.get("bundle_ids") or []):
            out(f"Capture is already running for {', '.join(st.get('apps') or [])}. Stop it first "
                f"(`hermes recorder off`) to check {app}, or run `hermes recorder check` to check those apps.")
            return 1
    else:
        if not app:
            out("Name an app to check, e.g. `hermes recorder check zoom` (start the call or video first).")
            return 2
        r = req("on", apps=[app])
        if r.get("error"):
            out(f"Couldn't start capture of {app}: {r['error']}")
            return 1
        started = True
    t0 = time.time()
    peaks: dict[str, float] = {}
    st = {}
    try:
        out(f"Listening to {', '.join(req('diag').get('apps') or [app or '?'])} for {seconds:g} s. "
            "Make sure it's playing sound (or someone is talking)…")
        while time.time() - t0 < seconds:
            sleep(CHECK_POLL_S)
            st = req("diag", args={"since": t0})
            for k, v in ((st.get("helper") or {}).get("streams") or {}).items():
                peaks[k] = max(peaks.get(k, 0.0), float(v.get("peak") or 0))
    finally:
        if started:
            req("off")
    helper = st.get("helper") or {}
    lines = st.get("lines_by_source") or {}
    ok = True
    out("")
    for bid in st.get("bundle_ids") or []:
        name = runtime.app_name(bid)
        path = (helper.get("audio_path") or {}).get(bid, "screen capture")
        heard = peaks.get(bid, peaks.get("app", 0.0)) > SILENT_PEAK
        got = lines.get(bid, 0)
        missing = bid in (helper.get("missing_apps") or [])
        mark = "✓" if heard and got else "✗"
        ok &= bool(heard and got)
        out(f"{mark} {name} ({path}): audio {'heard' if heard else 'silent'}, {got} transcript line(s)")
        if missing:
            out("   The app isn't running or has no audio process yet. Open it and start playing, then retry.")
        elif not heard:
            out("   No sound reached the recorder. Is it playing, and not muted inside the app? On a call, someone "
                "else has to talk (your own voice is the microphone line).")
            playing = sorted({p["label"] for p in audio_procs.active() if p["output"]
                              and "Hermes Recorder Helper" not in p["label"]})
            if playing:
                out(f"   Playing sound right now: {', '.join(playing)}.")
        elif not got:
            out("   Sound arrived but nothing was transcribed. Is it speech (music isn't transcribed)? Try a longer "
                "--seconds.")
    if helper.get("mic"):
        out(f"{'✓' if peaks.get('mic', 0) > SILENT_PEAK else '·'} Microphone: "
            f"{'heard you' if peaks.get('mic', 0) > SILENT_PEAK else 'quiet'}, {lines.get('mic', 0)} line(s)")
    if st.get("error"):
        out(f"Error: {st['error']}")
        ok = False
    return 0 if ok else 1


def _teach(action: str, paths: setup.Paths | None = None) -> int:
    paths = paths or setup.Paths(runtime.data_dir())
    ctx = runtime._ctx
    if action == "off":
        if ctx is not None:
            ctx.set_config("capture_events", False)
        print("Teach mode off. Clicks and typing aren't recorded (from the next recording on).")
        return 0
    ok, why = setup.build_is_current(paths, setup.host_python())
    if not ok:
        print(f"Run `hermes recorder setup` first ({why}).")
        return 1
    print("Teach mode records what you click and type in the apps being recorded, so Hermes can learn a task\n"
          "from watching you do it once. Passwords are never read. It uses Accessibility and Input Monitoring.")
    if not guide.ensure(paths, guide.TEACH):
        return 1
    if ctx is not None:
        ctx.set_config("capture_events", True)
    print("Teach mode on. It starts with your next recording. Do a task, then tell Hermes "
          "“turn what I just did into a skill”.")
    return 0


def _doctor(paths: setup.Paths) -> int:
    events = bool(runtime.config("capture_events"))
    rows = doctor.check(paths, needs_events=events)
    text, code = doctor.render(rows)
    print(text)
    perms = next((r.get("perms") for r in rows if r.get("perms") is not None), None)
    if code and perms is not None and guide.interactive():
        todo = guide.missing(perms, guide.BASIC + (guide.TEACH if events else ()))
        if todo and input("\nFix the permissions now? [Y/n] ").strip().lower() in ("", "y", "yes"):
            if guide.ensure(paths, tuple(todo)):
                text, code = doctor.render(doctor.check(paths, needs_events=events))
                print("\n" + text)
    return code


def format_step(s: dict) -> str:
    line = f"{s.get('n', '?'):>3}. [{s.get('app') or '?'}] {s.get('action', '')}"
    if s.get("target"):
        line += f" {s['target']}"
    if s.get("text") not in (None, ""):
        line += f" ← {s['text']!r}"
    if s.get("then_press"):
        line += f" + {s['then_press']}"
    if s.get("url"):
        line += f"  @ {s['url']}"
    if s.get("said"):
        line += f"\n       “{s['said']}”"
    return line


def _steps(minutes: float, req=None, out=print) -> int:
    req = req or (lambda op, **kw: control.request(runtime.data_dir(), op, timeout=20, **kw))
    try:
        r = req("steps", args={"minutes": minutes})
    except ConnectionError:
        out(NOT_LOADED)
        return 1
    if r.get("error"):
        out(f"Recorder: {r['error']}")
        return 1
    steps = r.get("steps") or []
    if not steps:
        why = {"needs_input_monitoring": " Teach mode is missing Input Monitoring: `hermes recorder teach on`.",
               "needs_accessibility": " Teach mode is missing Accessibility: `hermes recorder teach on`.",
               "off": " Teach mode is off for this recording: `hermes recorder teach on`, then record again."}
        out(f"No clicks or typing in the last {r.get('minutes', minutes):g} min."
            + why.get(r.get("events") or "", " Teach mode only records apps being captured "
                                              f"(now: {', '.join(r.get('apps') or []) or 'none'})."))
        return 1
    out(f"{len(steps)} step(s) in {', '.join(r.get('step_apps') or []) or '?'} (last {r.get('minutes', minutes):g} min):")
    for s in steps:
        out(format_step(s))
    return 0


def run(args) -> int:
    cmd = getattr(args, "recorder_cmd", None) or "status"
    if cmd == "steps":
        return _steps(float(getattr(args, "minutes", 5) or 5))
    if cmd == "teach":
        return _teach(args.action)
    if cmd == "check":
        return _check(getattr(args, "app", None), float(getattr(args, "seconds", 30) or 30))
    if cmd in ("on", "off", "toggle"):
        return _control(cmd, getattr(args, "apps", None))
    if cmd == "hotkey":
        return _hotkey(args)
    if cmd == "status":
        return _status()
    if cmd == "setup":
        return _setup(args)
    if cmd == "doctor":
        return _doctor(setup.Paths(runtime.data_dir()))
    return 2
