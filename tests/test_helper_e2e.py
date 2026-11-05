"""Real capture end to end on macOS: the generated helper app hears a test player app and transcribes it.

Opt-in because it plays audio out loud and needs `hermes recorder setup` done with permissions granted:
  HERMES_RECORDER_E2E=1 .venv/bin/python -m pytest tests/test_helper_e2e.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_recorder import runtime, setup

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" or os.environ.get("HERMES_RECORDER_E2E") != "1",
    reason="macOS e2e; set HERMES_RECORDER_E2E=1 (plays audio)")

PLAYER_ID = "io.github.michaelyouuu.hermes-recorder.testplayer"
ROOT = Path(__file__).resolve().parents[1]


def _helper_pids() -> list[str]:
    out = subprocess.run(["pgrep", "-f", "Hermes Recorder Helper.app/Contents/MacOS"], capture_output=True, text=True)
    return out.stdout.split()


@pytest.fixture(scope="module")
def paths():
    p = setup.Paths(runtime.data_dir())
    ok, why = setup.build_is_current(p, setup.host_python())
    if not ok:
        pytest.skip(f"helper runtime not ready ({why}); run `hermes recorder setup`")
    return p


def test_real_helper_transcribes_an_app(paths, tmp_path):
    audio = tmp_path / "review.aiff"
    subprocess.run(["say", "-o", str(audio), "Okay team. The launch review is on Thursday at three p m eastern, "
                    "and Priya will send the invite."], check=True)
    py = setup.host_python()
    player = setup.build_app(tmp_path / "Player.app", py, PLAYER_ID, "Hermes Recorder Test Player", "testplayer", {})
    env = {"PYTHONHOME": str(setup.python_home(py)), "PYTHONPATH": str(paths.site_packages())}
    rec = runtime.Recorder(tmp_path / "data")
    rec.manager.launcher.data_dir = paths.data  # use the real, permissioned helper build
    before = set(_helper_pids())
    rec.start([PLAYER_ID])
    try:
        st = rec.status()
        assert st["state"] == "running" and st["helper"]["permissions"]["screen_capture"] is True, st
        subprocess.run(setup.open_argv(player, env, [str(ROOT / "tests/support/player.py"), str(audio),
                                                     "--volume", "0.25", "--delay", "3"]), check=True, timeout=20)
        end = time.time() + 30
        text = ""
        while time.time() < end and "thursday" not in text:
            time.sleep(0.5)
            text = " ".join(e.text.lower() for e in rec.buffer.recent(2) if e.speaker == "them")
        assert "thursday" in text, (text, rec.manager.counters, rec.status())
        assert all(e.app == PLAYER_ID for e in rec.buffer.recent(2) if e.speaker == "them")
    finally:
        rec.stop()
    end = time.time() + 5
    while time.time() < end and set(_helper_pids()) - before:
        time.sleep(0.2)
    assert not (set(_helper_pids()) - before), "helper process left running after stop"
