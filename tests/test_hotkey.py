from __future__ import annotations

import plistlib
import subprocess

import pytest

from hermes_recorder import hotkey_agent, hotkeys, setup
from test_setup import FakeRun, fake_python  # noqa: F401  (fixture)


@pytest.mark.parametrize("combo,expected", [
    ("ctrl+opt+r", (15, 0x1000 | 0x0800)),
    ("cmd+shift+F5", (96, 0x0100 | 0x0200)),
    ("Control-Option-Space", (49, 0x1000 | 0x0800)),
])
def test_parse_combo(combo, expected):
    assert hotkeys.parse_combo(combo) == expected


@pytest.mark.parametrize("bad", ["", "r", "shift+r", "ctrl+opt+€", "hyper+r"])
def test_parse_combo_rejects(bad):
    with pytest.raises(ValueError):
        hotkeys.parse_combo(bad)


def test_pretty():
    assert hotkeys.pretty("ctrl+opt+r") == "⌃⌥R" and hotkeys.pretty("cmd+shift+f5") == "⌘⇧F5"


def test_plist_runs_helper_app_in_hotkey_mode(tmp_path, fake_python):
    paths = setup.Paths(tmp_path)
    setup.build_runtime(paths, python=fake_python, run=FakeRun(), say=lambda s: None)
    pl = plistlib.loads(hotkey_agent.plist_bytes(paths, "ctrl+opt+r"))
    args = pl["ProgramArguments"]
    assert pl["Label"] == hotkey_agent.LABEL and pl["RunAtLoad"] is True
    assert args[0].endswith("Contents/MacOS/" + setup.HELPER_EXE) and args[1].endswith("helper/main.py")
    assert args[args.index("--hotkey") + 1] == "ctrl+opt+r" and args[args.index("--data-dir") + 1] == str(tmp_path)
    assert pl["KeepAlive"] is True and pl["ThrottleInterval"] == 5  # comes back after macOS quits it
    assert pl["EnvironmentVariables"]["HF_HUB_OFFLINE"] == "1"


def test_install_and_uninstall(tmp_path, fake_python, monkeypatch):
    monkeypatch.setattr(hotkey_agent, "plist_path", lambda: tmp_path / "LaunchAgents" / "x.plist")
    paths = setup.Paths(tmp_path / "data")
    setup.build_runtime(paths, python=fake_python, run=FakeRun(), say=lambda s: None)
    run = FakeRun()
    p = hotkey_agent.install(paths, "ctrl+opt+r", run=run)
    assert p.exists()
    verbs = [c[1] for c in run.calls if c[0] == "/bin/launchctl"]
    assert verbs == ["bootout", "bootstrap"]  # replace any previous agent, then load
    with pytest.raises(ValueError):
        hotkey_agent.install(paths, "r", run=run)  # validated before touching launchd
    hotkey_agent.uninstall(run=run)
    assert not p.exists()
