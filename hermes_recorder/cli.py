"""`hermes recorder …` CLI. Runs in its own process, so live state comes from status.json."""
from __future__ import annotations

import json
import time

from . import runtime


def setup_parser(parser) -> None:
    sub = parser.add_subparsers(dest="recorder_cmd")
    sub.add_parser("status", help="Show capture status (as last written by the running gateway)")
    sub.add_parser("setup", help="Build the capture helper app and request permissions")
    sub.add_parser("doctor", help="Check permissions and helper health, with exact fix steps")


def run(args) -> int:
    cmd = getattr(args, "recorder_cmd", None) or "status"
    if cmd == "status":
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
    print(f"hermes recorder {cmd}: arrives in the next milestone (M2).")
    return 0
