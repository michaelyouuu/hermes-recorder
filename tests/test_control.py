from __future__ import annotations

import json
import threading

import pytest

from hermes_recorder import cli, control, registration, runtime
from hermes_recorder.helper_manager import AppLauncher
from test_tools import FakeCtx, fake_launcher
from test_manager import wait_for


@pytest.fixture
def short_dir(tmp_path_factory):
    import tempfile
    d = tempfile.mkdtemp(prefix="hrc-", dir="/tmp")
    yield __import__("pathlib").Path(d)


def test_round_trip_and_token(short_dir):
    calls = []
    srv = control.ControlServer(short_dir, lambda op, apps: calls.append((op, apps)) or {"state": "ok"})
    srv.start()
    try:
        assert oct(short_dir.joinpath("control.sock").stat().st_mode & 0o777) == "0o600"
        assert oct(short_dir.joinpath("control.token").stat().st_mode & 0o777) == "0o600"
        assert control.request(short_dir, "toggle", ["us.zoom.xos"]) == {"state": "ok"}
        assert calls == [("toggle", ["us.zoom.xos"])]
        short_dir.joinpath("control.token").write_text("forged")
        assert control.request(short_dir, "on")["error"] == "unauthorized"
        assert len(calls) == 1
    finally:
        srv.stop()
    assert not short_dir.joinpath("control.sock").exists() and not short_dir.joinpath("control.token").exists()
    with pytest.raises(ConnectionError):
        control.request(short_dir, "status")


def test_bad_requests_return_errors(short_dir):
    srv = control.ControlServer(short_dir, lambda op, apps: 1 / 0)
    srv.start()
    try:
        assert "unknown op" in control.request(short_dir, "explode")["error"]
        assert "ZeroDivisionError" in control.request(short_dir, "on")["error"]
    finally:
        srv.stop()


@pytest.fixture
def gateway(short_dir, monkeypatch):
    c = FakeCtx({"apps": []})
    c.state = type("S", (), {"d": {}, "get": lambda self, k, default=None: self.d.get(k, default),
                             "set": lambda self, k, v: self.d.__setitem__(k, v)})()
    registration.register_all(c)
    monkeypatch.setattr(runtime, "data_dir", lambda: short_dir)
    rec = runtime.Recorder(short_dir / "data", fake_launcher("call.jsonl"))
    runtime.reset_for_tests(rec)
    yield c, rec
    runtime.shutdown()
    runtime.bind_context(None)


def test_control_only_starts_in_gateway(gateway, short_dir):
    c, _ = gateway
    c.hooks["pre_llm_call"](session_id="s", platform="cli")
    assert runtime._control is None  # CLI process must not take the socket
    c.hooks["pre_llm_call"](session_id="s", platform="desktop")
    assert runtime._control is not None and short_dir.joinpath("control.sock").exists()
    c.hooks["pre_llm_call"](session_id="s", platform="photon")  # idempotent
    for cb in c.unload:
        cb()
    assert runtime._control is None and not short_dir.joinpath("control.sock").exists()


def test_pre_gateway_dispatch_starts_control(gateway, short_dir):
    c, _ = gateway
    assert c.hooks["pre_gateway_dispatch"](platform="photon", text="hi") is None
    assert runtime._control is not None


def test_toggle_uses_remembered_apps(gateway, capsys):
    c, rec = gateway
    c.hooks["pre_gateway_dispatch"]()
    p = __import__("argparse").ArgumentParser()
    setup_fn, run = c.cli["recorder"]
    setup_fn(p)
    assert run(p.parse_args(["toggle"])) == 1  # no apps anywhere yet
    assert "apps" in capsys.readouterr().out
    assert run(p.parse_args(["on", "us.zoom.xos"])) == 0
    assert "running · Zoom" in capsys.readouterr().out
    assert c.state.get(runtime.LAST_APPS_KEY) == ["us.zoom.xos"]
    assert run(p.parse_args(["toggle"])) == 0 and "stopped" in capsys.readouterr().out
    assert run(p.parse_args(["toggle"])) == 0 and "running · Zoom" in capsys.readouterr().out  # remembered
    assert run(p.parse_args(["off"])) == 0


def test_cli_hint_when_gateway_not_listening(short_dir, monkeypatch, capsys):
    monkeypatch.setattr(runtime, "data_dir", lambda: short_dir)
    assert cli._control("toggle", None) == 1
    assert "Send Hermes any message" in capsys.readouterr().out


def test_existing_served_socket_is_not_stolen(gateway, short_dir):
    other = control.ControlServer(short_dir, lambda op, apps: {"state": "other"})
    other.start()
    try:
        gateway[0].hooks["pre_gateway_dispatch"]()
        assert runtime._control is None
        assert control.request(short_dir, "status") == {"state": "other"}
    finally:
        other.stop()


def test_hotkey_fallback_used_only_when_no_apps_known(gateway, short_dir):
    c, rec = gateway
    c.hooks["pre_gateway_dispatch"]()
    r = control.request(short_dir, "toggle", fallback_apps=["us.zoom.xos"])
    assert r["state"] == "running" and r["apps"] == ["Zoom"]
    control.request(short_dir, "off")
    r = control.request(short_dir, "toggle", fallback_apps=["com.microsoft.edgemac"])
    assert r["apps"] == ["Zoom"]  # remembered apps win over the frontmost-app fallback
    control.request(short_dir, "off")


def test_resolve_apps_friendly_names():
    from hermes_recorder.runtime import resolve_apps
    assert resolve_apps(["FaceTime", "zoom", "Microsoft Edge", "com.example.App", "facetime"]) == [
        "com.apple.FaceTime", "us.zoom.xos", "com.microsoft.edgemac", "com.example.App"]
    assert resolve_apps(["phone", "WhatsApp"]) == ["com.apple.mobilephone", "net.whatsapp.WhatsApp"]
