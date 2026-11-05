"""`hermes recorder …` CLI. Runs in its own process, so live state comes from status.json."""
from __future__ import annotations

import json
import subprocess
import time

import sys

from . import control, doctor, hotkey_agent, runtime, setup

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
    sub.add_parser("doctor", help="Check permissions and helper health, with exact fix steps")
    on = sub.add_parser("on", help="Start capture in the running gateway (last apps if none given)")
    on.add_argument("apps", nargs="*", help="Bundle ids, e.g. com.google.Chrome")
    sub.add_parser("off", help="Stop capture in the running gateway")
    t = sub.add_parser("toggle", help="Start or stop capture (what the global hotkey runs)")
    t.add_argument("apps", nargs="*", help="Bundle ids to use when starting")
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
        print("Asking macOS for permissions. Approve the prompts for 'Hermes Recorder Helper'.")
        pf = setup.run_preflight(paths, request=True, events=events)
        if "error" in pf:
            print(pf["error"])
    _offer_hotkey(paths, getattr(args, "hotkey_choice", None))
    text, code = doctor.render(doctor.check(paths, needs_events=events))
    print(text)
    if code:
        print("\nAfter switching a permission on, run `hermes recorder doctor` to confirm.")
    else:
        print("\nReady. In Hermes: /recorder on com.google.Chrome (or any app's bundle id).")
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
        print(f"Hotkey {hotkey_agent.pretty(combo)} is on. Press it in any app to start or stop recording "
              "(Tink = started, Pop = stopped, Basso = problem).")
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


def run(args) -> int:
    cmd = getattr(args, "recorder_cmd", None) or "status"
    if cmd in ("on", "off", "toggle"):
        return _control(cmd, getattr(args, "apps", None))
    if cmd == "hotkey":
        return _hotkey(args)
    if cmd == "status":
        return _status()
    if cmd == "setup":
        return _setup(args)
    if cmd == "doctor":
        paths = setup.Paths(runtime.data_dir())
        text, code = doctor.render(doctor.check(paths, needs_events=bool(runtime.config("capture_events"))))
        print(text)
        return code
    return 2
