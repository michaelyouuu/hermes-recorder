from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_recorder import doctor, helper_manager, runtime, setup


class FakeRun:
    """Records commands; simulates venv creation and codesign (CDHash = hash of the copied binary)."""

    def __init__(self):
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kw):
        self.calls.append([str(a) for a in argv])
        if argv[0] == "/usr/bin/codesign" and "-dv" in argv:
            exe = Path(argv[-1]) / "Contents" / "MacOS" / setup.HELPER_EXE
            return subprocess.CompletedProcess(argv, 0, "", f"CDHash={setup.sha256(exe)[:40]}\n")
        if "-m" in argv and "venv" in argv:
            venv = Path(argv[-1])
            (venv / "bin").mkdir(parents=True, exist_ok=True)
            (venv / "bin" / "python").write_text("")
            (venv / "lib" / "python3.14" / "site-packages").mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(argv, 0, "", "")


@pytest.fixture
def fake_python(tmp_path):
    root = tmp_path / "py"
    (root / "bin").mkdir(parents=True)
    exe = root / "bin" / "python3.14"
    exe.write_bytes(b"\xcf\xfa\xed\xfe fake mach-o")
    return exe


def test_info_plist_has_identity_usage_strings_and_home():
    pl = plistlib.loads(setup.info_plist(setup.HELPER_BUNDLE_ID, setup.HELPER_NAME, setup.HELPER_EXE,
                                         setup.USAGE, {"PYTHONHOME": "/x"}))
    assert pl["CFBundleIdentifier"] == "io.github.michaelyouuu.hermes-recorder.helper"
    assert pl["LSUIElement"] is True and pl["LSEnvironment"] == {"PYTHONHOME": "/x"}
    for key in ("NSMicrophoneUsageDescription", "NSAudioCaptureUsageDescription", "NSScreenCaptureUsageDescription"):
        assert "locally" in pl[key]


def test_build_runtime_creates_and_is_idempotent(tmp_path, fake_python):
    paths, run = setup.Paths(tmp_path / "data"), FakeRun()
    b = setup.build_runtime(paths, python=fake_python, run=run, say=lambda s: None)
    exe = paths.app / "Contents" / "MacOS" / setup.HELPER_EXE
    assert exe.read_bytes() == fake_python.read_bytes()
    assert b["python_sha256"] == setup.sha256(fake_python) and b["requirements"] == setup.requirements()
    assert any(c[0] == "/usr/bin/codesign" and "--sign" in c and "-" in c for c in run.calls)
    assert any("pip" in c and "install" in c for c in run.calls)
    assert any(c[-2:] == ["pip", "check"] for c in run.calls)
    assert all("-I" in c for c in run.calls if "pip" in c or "venv" in c)
    resets = [c for c in run.calls if c[0] == "/usr/bin/tccutil"]
    assert {c[2] for c in resets} == set(setup.TCC_SERVICES) and all(c[3] == setup.HELPER_BUNDLE_ID for c in resets)
    assert setup.build_is_current(paths, fake_python) == (True, "up to date")
    n = len(run.calls)
    setup.build_runtime(paths, python=fake_python, run=run, say=lambda s: None)
    assert len(run.calls) == n  # nothing rebuilt


def test_python_change_triggers_rebuild_and_warning(tmp_path, fake_python):
    paths, run, said = setup.Paths(tmp_path / "data"), FakeRun(), []
    setup.build_runtime(paths, python=fake_python, run=run, say=said.append)
    fake_python.write_bytes(b"\xcf\xfa\xed\xfe updated python")
    ok, why = setup.build_is_current(paths, fake_python)
    assert not ok and "Python changed" in why
    setup.build_runtime(paths, python=fake_python, run=run, say=said.append)
    assert any("ask for its permissions again" in s for s in said)


def test_identical_rebuild_keeps_permissions(tmp_path, fake_python):
    paths, run = setup.Paths(tmp_path / "data"), FakeRun()
    setup.build_runtime(paths, python=fake_python, run=run, say=lambda s: None)
    first = [c for c in run.calls if c[0] == "/usr/bin/tccutil"]
    run.calls.clear()
    setup.build_runtime(paths, python=fake_python, run=run, say=lambda s: None, force=True)
    assert [c for c in run.calls if c[0] == "/usr/bin/tccutil"] == []  # same CDHash: grants still valid
    assert first and setup.read_build(paths)["cdhash"]


def test_helper_env_and_open_argv(tmp_path, fake_python):
    paths = setup.Paths(tmp_path / "data")
    setup.build_runtime(paths, python=fake_python, run=FakeRun(), say=lambda s: None)
    env = setup.helper_env(paths)
    assert env["HF_HUB_OFFLINE"] == "1" and env["PYTHONHOME"] == str(fake_python.parent.parent.resolve())
    assert str(runtime.PLUGIN_ROOT) in env["PYTHONPATH"]
    argv = setup.open_argv(paths.app, env, ["main.py", "--x"])
    assert argv[:3] == ["/usr/bin/open", "-n", "-g"] and "-W" not in argv and "--stdout" not in argv
    assert argv[argv.index("-a") + 1] == str(paths.app) and argv[-2:] == ["main.py", "--x"]


def test_app_launcher_refuses_until_built(tmp_path):
    with pytest.raises(helper_manager.LaunchError, match="hermes recorder setup"):
        helper_manager.AppLauncher(tmp_path).launch("/s", "t", {"apps": ["a"]})


def test_app_launcher_launches_with_open(tmp_path, fake_python, monkeypatch):
    paths = setup.Paths(tmp_path)
    setup.build_runtime(paths, python=fake_python, run=FakeRun(), say=lambda s: None)
    monkeypatch.setattr(setup, "host_python", lambda: fake_python)
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(helper_manager.subprocess, "run", fake_run)
    helper_manager.AppLauncher(tmp_path).launch("/sock", "tok", {"apps": ["us.zoom.xos"], "capture_mic": False})
    a = seen["argv"]
    assert a[0] == "/usr/bin/open" and "--socket" in a and a[a.index("--apps") + 1] == "us.zoom.xos"
    assert a[a.index("--mic") + 1] == "0" and a[a.index("--model") + 1] == "base.en"


def test_terminate_only_kills_our_helper(tmp_path, monkeypatch):
    killed = []
    monkeypatch.setattr(helper_manager.os, "kill", lambda pid, sig: killed.append(pid))
    launcher = helper_manager.AppLauncher(tmp_path)
    exe = setup.Paths(tmp_path).app / "Contents" / "MacOS" / setup.HELPER_EXE
    monkeypatch.setattr(helper_manager, "pid_path", lambda pid: str(exe) if pid == 111 else "/usr/bin/other")
    launcher.terminate(222)  # some other process now owns this pid
    launcher.terminate(None)
    assert killed == []
    launcher.terminate(111)
    assert killed == [111]


def test_doctor_reports_missing_runtime_with_fix(tmp_path, monkeypatch):
    monkeypatch.setattr(setup, "platform_problems", lambda: [])
    rows = doctor.check(setup.Paths(tmp_path), run_preflight=lambda *a, **k: {})
    text, code = doctor.render(rows)
    assert code == 1 and "hermes recorder setup" in text


def test_doctor_permission_rows(tmp_path, fake_python, monkeypatch):
    paths = setup.Paths(tmp_path)
    setup.build_runtime(paths, python=fake_python, run=FakeRun(), say=lambda s: None)
    snap = paths.models / "hub" / "models--Systran--faster-whisper-base.en" / "snapshots" / "abc"
    snap.mkdir(parents=True)
    (snap / "model.bin").write_bytes(b"x")
    monkeypatch.setattr(setup, "platform_problems", lambda: [])
    monkeypatch.setattr(setup, "host_python", lambda: fake_python)
    pf = {"pid": 5, "permissions": {"screen_capture": False, "microphone": "authorized",
                                    "accessibility": False, "input_monitoring": False},
          "mic_supported": True, "screen_capture_works": False}
    text, code = doctor.render(doctor.check(paths, run_preflight=lambda *a, **k: pf))
    assert code == 1 and "✗ Permission: Screen & System Audio Recording" in text
    assert "walk you through it" in text and "✓ Permission: Microphone" in text
    assert "· Permission: Accessibility" in text  # teach-mode only: not counted as a failure
    pf["permissions"]["screen_capture"] = True
    pf["screen_capture_works"] = True
    assert doctor.render(doctor.check(paths, run_preflight=lambda *a, **k: pf))[1] == 0


def test_model_download_skipped_when_present(tmp_path):
    paths, run, said = setup.Paths(tmp_path), FakeRun(), []
    setup.download_model(paths, "base.en", run=run, say=said.append)
    assert run.calls and "Downloading" in said[0]
    snap = paths.models / "hub" / "models--Systran--faster-whisper-base.en" / "snapshots" / "abc"
    snap.mkdir(parents=True)
    (snap / "model.bin").write_bytes(b"x")
    run2 = FakeRun()
    setup.download_model(paths, "base.en", run=run2, say=said.append)
    assert run2.calls == [] and "already downloaded" in said[-1]


def test_clean_env_drops_python_and_pip_settings(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/hermes/env/site-packages")
    monkeypatch.setenv("PIP_NO_DEPS", "1")
    monkeypatch.setenv("VIRTUAL_ENV", "/hermes/env")
    env = setup.clean_env(HF_HOME="/m")
    assert "PYTHONPATH" not in env and "PIP_NO_DEPS" not in env and "VIRTUAL_ENV" not in env
    assert env["HF_HOME"] == "/m" and env.get("HOME")
