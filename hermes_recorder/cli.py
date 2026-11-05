"""`hermes recorder …` CLI. Runs in its own process, so live state comes from status.json."""
from __future__ import annotations

import json
import subprocess
import time

from . import doctor, runtime, setup


def setup_parser(parser) -> None:
    sub = parser.add_subparsers(dest="recorder_cmd")
    sub.add_parser("status", help="Show capture status (as last written by the running gateway)")
    p = sub.add_parser("setup", help="Build the capture helper app, download the model, request permissions")
    p.add_argument("--force", action="store_true", help="Rebuild even if up to date")
    p.add_argument("--no-prompts", action="store_true", help="Don't trigger the macOS permission prompts")
    sub.add_parser("doctor", help="Check permissions and helper health, with exact fix steps")


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
    text, code = doctor.render(doctor.check(paths, needs_events=events))
    print(text)
    if code:
        print("\nAfter switching a permission on, run `hermes recorder doctor` to confirm.")
    else:
        print("\nReady. In Hermes: /recorder on com.google.Chrome (or any app's bundle id).")
    return code


def run(args) -> int:
    cmd = getattr(args, "recorder_cmd", None) or "status"
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
