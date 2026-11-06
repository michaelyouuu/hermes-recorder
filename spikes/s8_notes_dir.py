"""S8a: can a process launched the way Hermes launches its gateway (launchd job -> osascript) write notes to
~/Documents and ~/<home>? ~/Documents is TCC-protected and the gateway is osascript-responsible.

Run with Hermes's python:  python3 -I spikes/s8_notes_dir.py
Prints one JSON line per target: ok / errno / seconds.
"""
from __future__ import annotations

import json
import os
import plistlib
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

TARGETS = [Path.home() / "Documents" / "Hermes Notes", Path.home() / "Hermes Notes"]

WRITER = r'''
import json, os, sys, time
from pathlib import Path
out = []
for d in sys.argv[2:]:
    t0 = time.time(); p = Path(d)
    try:
        p.mkdir(parents=True, exist_ok=True)
        f = p / ".hermes-recorder-probe"
        f.write_text("probe"); ok = f.read_text() == "probe"; f.unlink()
        out.append({"dir": d, "ok": ok, "s": round(time.time() - t0, 2)})
    except OSError as e:
        out.append({"dir": d, "ok": False, "errno": e.errno, "err": str(e), "s": round(time.time() - t0, 2)})
Path(sys.argv[1]).write_text(json.dumps(out))
'''


def main() -> None:
    hermes = Path.home() / ".hermes" / "hermes-agent"
    sys.path.insert(0, str(hermes))
    from hermes_cli.gateway_launchd import launchd_program_arguments  # the gateway's own launch wrapper

    tmp = Path(tempfile.mkdtemp(prefix="s8-"))
    writer, result, log = tmp / "writer.py", tmp / "result.json", tmp / "log.txt"
    writer.write_text(WRITER)
    label = f"io.github.michaelyouuu.hermes-recorder.s8.{uuid.uuid4().hex[:8]}"
    cmd = [sys.executable, "-I", str(writer), str(result), *map(str, TARGETS)]
    plist = tmp / f"{label}.plist"
    plist.write_bytes(plistlib.dumps({"Label": label, "ProgramArguments": launchd_program_arguments(cmd, log, log),
                                      "RunAtLoad": True}))
    dom = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootstrap", dom, str(plist)], check=True)
    try:
        for _ in range(60):
            if result.exists():
                break
            time.sleep(0.5)
        print(result.read_text() if result.exists() else json.dumps({"error": "no result in 30 s", "log": log.read_text() if log.exists() else ""}))
    finally:
        subprocess.run(["launchctl", "bootout", f"{dom}/{label}"], capture_output=True)


if __name__ == "__main__":
    main()
