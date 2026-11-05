from __future__ import annotations

import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest

from hermes_recorder.buffer import RingBuffer
from hermes_recorder.helper_manager import (AppLauncher, HelperManager, LaunchError, SubprocessLauncher,
                                            choose_socket_path)
from conftest import FIXTURES

FAKE = str(Path(__file__).with_name("fake_helper.py"))


def wait_for(pred, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def make(tmp_path, fixture="call.jsonl", extra=(), hello_timeout=5.0, delays=(0.05, 0.05, 0.05)):
    # short dir so the socket path fits under the macOS limit even in pytest's long tmp paths
    log = tmp_path / "ctl.log"
    launcher = SubprocessLauncher(lambda sock, tok, opts: [sys.executable, FAKE, "--socket", sock, "--token", tok,
                                                           "--fixture", str(FIXTURES / fixture), "--log", str(log),
                                                           *extra])
    buf = RingBuffer(30)
    m = HelperManager(buf, tmp_path / "data", launcher, hello_timeout=hello_timeout, restart_delays=delays)
    return m, buf, launcher, log


def controls(log: Path) -> list[str]:
    if not log.exists():
        return []
    return [json.loads(x)["op"] for x in log.read_text().splitlines() if x.strip()]


def test_start_streams_into_buffer_and_stop_cleans_up(tmp_path):
    m, buf, launcher, log = make(tmp_path, extra=["--hold"])
    m.start(["us.zoom.xos"])
    assert m.state == "running"
    assert wait_for(lambda: m.counters["stored"] == 7)
    st = m.status()
    assert st["helper"]["version"] == "fake-0" and st["helper"]["pid"]
    assert "{CARD}" in " ".join(e.text for e in buf.recent(5))  # privacy applied on ingest
    sock = m._sock_path
    assert stat.S_IMODE(os.stat(sock).st_mode) == 0o600
    m.stop()
    assert m.state == "stopped" and not os.path.exists(sock)
    assert wait_for(lambda: "stop" in controls(log))
    assert launcher.proc is None


def test_privacy_drops_unlisted_and_denylisted_apps(tmp_path):
    m, buf, _, _ = make(tmp_path, fixture="teach_notes.jsonl", extra=["--hold"])
    m.start(["com.apple.Notes", "com.1password.1password"])
    assert m.apps == ["com.apple.Notes"]
    assert wait_for(lambda: m.counters["lines"] >= 11)
    apps = {e.app for e in buf.recent(5)}
    assert "com.1password.1password" not in apps and "com.apple.dock" in apps
    assert "hunter2" not in repr([e.data for e in buf.recent(5)])
    m.stop()


def test_pause_resume_set_apps_send_controls(tmp_path):
    m, _, _, log = make(tmp_path, extra=["--hold"])
    m.start(["us.zoom.xos"])
    m.pause()
    assert m.state == "paused"
    m.resume()
    m.set_apps(["us.zoom.xos", "com.google.Chrome"])
    assert wait_for(lambda: controls(log)[:3] == ["pause", "resume", "set_apps"])
    m.stop()


def test_bad_token_is_rejected(tmp_path):
    m, _, _, _ = make(tmp_path, extra=["--bad-token", "--hold"], hello_timeout=1.0)
    with pytest.raises(LaunchError, match="didn't connect"):
        m.start(["us.zoom.xos"])
    assert m.state == "error" and m.counters["rejected_conns"] == 1


def test_no_hello_times_out_with_clear_error(tmp_path):
    m, _, _, _ = make(tmp_path, extra=["--no-hello", "--hold"], hello_timeout=0.5)
    with pytest.raises(LaunchError, match="doctor"):
        m.start(["us.zoom.xos"])
    assert "didn't connect" in m.status()["error"]


def test_no_apps_is_an_error(tmp_path):
    m, _, _, _ = make(tmp_path)
    with pytest.raises(LaunchError, match="apps"):
        m.start([])
    with pytest.raises(LaunchError):
        m.start(["com.1password.1password"])  # denylisted only


def test_crash_restarts_then_gives_up(tmp_path):
    m, _, _, _ = make(tmp_path, extra=["--crash-after", "2"])
    m.start(["us.zoom.xos"])
    assert wait_for(lambda: m.state == "error", timeout=10)
    st = m.status()
    assert st["helper"]["restarts"] == 3 and "gave up" in st["error"]


def test_crash_once_recovers(tmp_path):
    m, buf, launcher, _ = make(tmp_path, extra=["--crash-after", "2"])
    m.start(["us.zoom.xos"])
    # after the first crash, relaunch a well-behaved helper
    good = lambda sock, tok, opts: [sys.executable, FAKE, "--socket", sock, "--token", tok,
                                    "--fixture", str(FIXTURES / "call.jsonl"), "--hold"]
    launcher.argv_factory = good
    assert wait_for(lambda: m.status()["helper"]["restarts"] == 1 and m.state == "running" and m.counters["lines"] >= 9)
    # the replayed first two lines are exact repeats, so the overlap merge drops them
    assert m.counters["stored"] == 7
    m.stop()


def test_app_launcher_stub_says_how_to_fix(tmp_path):
    m = HelperManager(RingBuffer(30), tmp_path / "d", AppLauncher(tmp_path / "d"))
    with pytest.raises(LaunchError, match="hermes recorder setup"):
        m.start(["us.zoom.xos"])
    assert m.state == "error"


def test_long_data_dir_falls_back_to_short_socket(tmp_path):
    long_dir = tmp_path / ("x" * 120)
    path, tmp = choose_socket_path(long_dir)
    assert len(path.encode()) <= 100 and tmp and os.path.isdir(tmp)
    os.rmdir(tmp)


def test_stop_is_idempotent(tmp_path):
    m, _, _, _ = make(tmp_path)
    m.stop()
    m.stop()
    assert m.state == "stopped"


def test_helper_status_messages_are_exposed(tmp_path):
    m, _, _, _ = make(tmp_path, extra=["--hold"])
    seen = []
    m.on_status = lambda: seen.append(1)
    m.start(["us.zoom.xos"])
    m._handle_line(b'{"v":1,"type":"status","ts":1,"capturing":["us.zoom.xos"],"missing_apps":[],"rss_mb":240.5}\n')
    st = m.status()["helper"]
    assert st["capturing"] == ["us.zoom.xos"] and st["missing_apps"] == [] and st["rss_mb"] == 240.5
    assert seen == [1]
    m.stop()
