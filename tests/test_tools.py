from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

from hermes_recorder import commands, hooks, registration, runtime, tools
from hermes_recorder.helper_manager import AppLauncher, SubprocessLauncher
from conftest import FIXTURES
from test_manager import FAKE, wait_for


class FakeCtx:
    def __init__(self, config=None):
        self.config = config or {}
        self.tools, self.hooks, self.commands, self.cli, self.unload = {}, {}, {}, {}, []

    def get_config(self, key, default=None):
        return self.config.get(key, default)

    def register_tool(self, name, toolset, schema, handler, **kw):
        assert schema["name"] == name
        self.tools[name] = handler

    def register_hook(self, name, cb):
        self.hooks[name] = cb

    def register_command(self, name, handler, description="", args_hint=""):
        self.commands[name] = handler

    def register_cli_command(self, name, help, setup_fn, handler_fn=None):
        self.cli[name] = (setup_fn, handler_fn)

    def on_unload(self, cb):
        self.unload.append(cb)

    def register_skill(self, name, path, description="", frontmatter=None):
        assert Path(path).exists(), path
        self.skills = {**getattr(self, "skills", {}), name: Path(path)}


def fake_launcher(fixture, *extra):
    return SubprocessLauncher(lambda s, t, o: [sys.executable, FAKE, "--socket", s, "--token", t,
                                               "--fixture", str(FIXTURES / fixture), "--delay", "0.005", "--hold",
                                               *extra])


@pytest.fixture
def ctx(tmp_path):
    c = FakeCtx({"apps": ["us.zoom.xos"], "capture_mic": True})
    registration.register_all(c)
    yield c
    runtime.shutdown()
    runtime.bind_context(None)


@pytest.fixture
def live(ctx, tmp_path):
    rec = runtime.Recorder(tmp_path / "d", fake_launcher("call.jsonl"))
    runtime.reset_for_tests(rec)
    out = json.loads(ctx.tools["recorder_control"]({"op": "start"}))
    assert out["state"] == "running", out
    assert wait_for(lambda: rec.manager.counters["stored"] == 7)
    return rec


def test_register_is_side_effect_free(tmp_path):
    c = FakeCtx()
    registration.register_all(c)
    assert set(c.tools) == {"screen_recall", "recorder_status", "recorder_control", "recorder_draft_skill", "recorder_save_notes", "recorder_notes"}
    assert set(c.hooks) == {"pre_llm_call", "on_session_end", "pre_gateway_dispatch"}
    assert set(c.commands) == {"recorder", "teach"} and "recorder" in c.cli and c.unload
    assert runtime.peek() is None  # nothing created by register()


def test_recall_returns_lines_with_relative_times(ctx, live):
    out = json.loads(ctx.tools["screen_recall"]({"minutes": 2}))
    assert len(out["lines"]) == 7 and out["truncated"] is False
    first = out["lines"][0]
    assert set(first) == {"t", "app", "speaker", "text"} and first["app"] == "Zoom"
    assert first["t"].startswith("-00:")


def test_recall_query_and_speaker(ctx, live):
    out = json.loads(ctx.tools["screen_recall"]({"query": "thursday", "speaker": "them"}))
    assert out["lines"] and all(l["speaker"] == "them" and "Thursday" in l["text"] for l in out["lines"])
    assert "note" not in out


def test_recall_paraphrased_query_falls_back_to_window(ctx, live):
    out = json.loads(ctx.tools["screen_recall"]({"query": "what time did we agree on"}))
    assert any("3 PM" in l["text"] for l in out["lines"]) and "all lines" in out["note"]


def test_recall_masks_secrets(ctx, live):
    text = json.dumps(json.loads(ctx.tools["screen_recall"]({"minutes": 5})))
    assert "4111" not in text and "{CARD}" in text


def test_recall_caps_output(ctx, tmp_path):
    rec = runtime.Recorder(tmp_path / "d", AppLauncher(tmp_path / "d"))
    runtime.reset_for_tests(rec)
    from hermes_recorder.buffer import Entry
    for i in range(400):
        rec.buffer.add(Entry("transcript", time.time() - 400 + i, "us.zoom.xos", "them",
                             f"sentence number {i} with a fair amount of filler text to take up space"))
    out = json.loads(ctx.tools["screen_recall"]({"minutes": 30}))
    assert out["truncated"] is True and len(json.dumps(out)) < 9000
    assert out["lines"][-1]["text"].startswith("sentence number 399")  # newest kept


def test_recall_when_off_explains(ctx, tmp_path):
    runtime.reset_for_tests(runtime.Recorder(tmp_path / "d", AppLauncher(tmp_path / "d")))
    out = json.loads(ctx.tools["screen_recall"]({}))
    assert out["lines"] == [] and "Capture is off" in out["note"]


@pytest.mark.parametrize("args", [None, "x", {"minutes": "abc"}, {"minutes": -5}, {"speaker": "bob"}, {"app": 7}])
def test_tools_never_raise_on_bad_args(ctx, tmp_path, args):
    runtime.reset_for_tests(runtime.Recorder(tmp_path / "d", AppLauncher(tmp_path / "d")))
    for name, handler in ctx.tools.items():
        json.loads(handler(args))  # always valid JSON


def test_control_errors_are_json(ctx, tmp_path):
    runtime.reset_for_tests(runtime.Recorder(tmp_path / "d", AppLauncher(tmp_path / "d")))
    assert "hermes recorder setup" in json.loads(ctx.tools["recorder_control"]({"op": "start"}))["error"]
    assert "unknown op" in json.loads(ctx.tools["recorder_control"]({"op": "explode"}))["error"]
    assert "list" in json.loads(ctx.tools["recorder_control"]({"op": "start", "apps": "us.zoom.xos"}))["error"]
    assert "Teach mode is off" in json.loads(ctx.tools["recorder_draft_skill"]({}))["error"]


def test_status_shape(ctx, live):
    st = json.loads(ctx.tools["recorder_status"]({}))
    assert st["running"] and st["apps"] == [{"bundle_id": "us.zoom.xos", "name": "Zoom"}]
    assert st["buffer"]["transcript_lines"] == 7 and st["helper"]["permissions"]["microphone"] == "authorized"
    assert (live.data_dir / "status.json").exists()


def test_pause_resume_stop_via_tool(ctx, live):
    assert json.loads(ctx.tools["recorder_control"]({"op": "pause"}))["state"] == "paused"
    assert json.loads(ctx.tools["recorder_control"]({"op": "resume"}))["state"] == "running"
    assert json.loads(ctx.tools["recorder_control"]({"op": "stop"}))["state"] == "stopped"


def test_hook_off_returns_none(ctx):
    assert ctx.hooks["pre_llm_call"](session_id="s", user_message="hi") is None


def test_hook_live_one_line_and_fast(ctx, live):
    t = time.perf_counter()
    for _ in range(100):
        out = ctx.hooks["pre_llm_call"](session_id="s", user_message="hi", platform="cli")
    per_call_ms = (time.perf_counter() - t) * 1000 / 100
    note = out["context"]
    assert "\n" not in note and len(note) < 300 and "Zoom" in note and "screen_recall" in note
    assert per_call_ms < 5
    assert ctx.hooks["on_session_end"](session_id="s", completed=True) is None


def test_commands(ctx, live):
    assert "running" in ctx.commands["recorder"]("status")
    assert "paused" in ctx.commands["recorder"]("pause")
    assert "running" in ctx.commands["recorder"]("resume")
    assert "cleared" in ctx.commands["recorder"]("forget") and live.buffer.recent(30) == []
    assert "stopped" in ctx.commands["recorder"]("off")
    assert "Usage" in ctx.commands["recorder"]("dance")
    assert "Teach mode is off" in ctx.commands["teach"]("5") and "Usage" in ctx.commands["teach"]("abc")


def test_unload_stops_helper_and_threads(ctx, live):
    import threading
    proc = live.manager.launcher.proc
    for cb in ctx.unload:
        cb()
    assert runtime.peek() is None and proc.poll() is not None
    assert wait_for(lambda: not [t for t in threading.enumerate() if t.name.startswith("hermes-recorder")])


def test_autostart_on_first_tool_call(tmp_path):
    c = FakeCtx({"apps": ["us.zoom.xos"], "autostart": True})
    registration.register_all(c)
    try:
        rec = runtime.Recorder(tmp_path / "d", fake_launcher("call.jsonl"))
        runtime.reset_for_tests(rec)
        st = json.loads(c.tools["recorder_status"]({}))
        assert st["state"] == "running"
    finally:
        runtime.shutdown()
        runtime.bind_context(None)


def test_cli_status_reads_snapshot(ctx, live, capsys, monkeypatch):
    monkeypatch.setattr(runtime, "data_dir", lambda: live.data_dir)
    import argparse
    p = argparse.ArgumentParser()
    setup, run = ctx.cli["recorder"]
    setup(p)
    assert run(p.parse_args(["status"])) == 0
    assert "state: running" in capsys.readouterr().out


def test_dev_fixture_env_is_confined(monkeypatch, tmp_path):
    monkeypatch.setenv(runtime.DEV_FIXTURE_ENV, str(tmp_path / "evil.jsonl"))
    (tmp_path / "evil.jsonl").write_text("")
    assert runtime._dev_launcher() is None
    monkeypatch.setenv(runtime.DEV_FIXTURE_ENV, str(FIXTURES / "call.jsonl"))
    assert runtime._dev_launcher() is not None
