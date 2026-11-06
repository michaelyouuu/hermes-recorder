"""Load the plugin through the real Hermes loader in a temporary HERMES_HOME.

Run with Hermes's own interpreter:
  ~/.hermes/tools/python-3.14.*/bin/python3 -I tests/integration/run_in_hermes.py -q
Skipped when Hermes isn't importable (e.g. plain unit-test runs).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
pytest.importorskip("hermes_constants", reason="needs Hermes (run tests/integration/run_in_hermes.py)")

# Only reached inside Hermes's runtime: point Hermes at a throwaway home before importing its loader.
HOME = Path(tempfile.mkdtemp(prefix="hrec-home-"))
os.environ["HERMES_HOME"] = str(HOME)
(HOME / "plugins").mkdir(parents=True)
(HOME / "plugins" / "hermes-recorder").symlink_to(ROOT, target_is_directory=True)  # same as the dev install
(HOME / "config.yaml").write_text(
    "plugins:\n  enabled: [hermes-recorder]\n  entries:\n    hermes-recorder:\n      settings:\n"
    "        apps: [us.zoom.xos]\n        buffer_minutes: 20\n")
os.environ["HERMES_RECORDER_DEV_FIXTURE"] = str(ROOT / "tests" / "fixtures" / "call.jsonl")
hermes_cli = pytest.importorskip("hermes_cli.plugins")


@pytest.fixture(scope="module")
def loaded():
    from hermes_cli import plugins
    plugins.discover_plugins(force=True)
    from tools.registry import registry
    yield plugins, registry
    mgr = plugins.get_plugin_manager()
    for name in ("hermes-recorder",):
        try:
            mgr.unload(name)
        except Exception:
            pass


def _call(registry, name, args):
    out = registry.dispatch(name, args)
    return json.loads(out) if isinstance(out, str) else out


def test_tools_registered(loaded):
    _, registry = loaded
    for name in ("screen_recall", "recorder_status", "recorder_control", "recorder_draft_skill", "recorder_save_notes", "recorder_notes"):
        assert registry.get_entry(name) is not None, name


def test_end_to_end_with_fake_helper(loaded):
    plugins, registry = loaded
    assert _call(registry, "recorder_status", {})["state"] == "stopped"
    # nothing injected while off
    assert not [r for r in plugins.invoke_hook("pre_llm_call", session_id="s", user_message="hi") if r]

    st = _call(registry, "recorder_control", {"op": "start"})  # apps come from settings
    assert st["state"] == "running", st
    assert st["buffer"]["max_minutes"] == 20  # config override read via ctx.get_config
    end = time.time() + 10
    while time.time() < end and _call(registry, "recorder_status", {})["buffer"]["transcript_lines"] < 7:
        time.sleep(0.1)
    recall = _call(registry, "screen_recall", {"minutes": 5, "speaker": "them"})
    assert any("3 PM" in line["text"] for line in recall["lines"])
    notes = [r for r in plugins.invoke_hook("pre_llm_call", session_id="s", user_message="hi") if r]
    assert notes and "Zoom" in notes[0]["context"]

    data = HOME / "plugin-data" / "hermes-recorder"
    assert (data / "status.json").exists(), list(HOME.rglob("*"))[:20]
    assert _call(registry, "recorder_control", {"op": "stop"})["state"] == "stopped"


def test_unload_cleans_up(loaded):
    plugins, registry = loaded
    import threading
    assert _call(registry, "recorder_control", {"op": "start"})["state"] == "running"
    assert plugins.get_plugin_manager().unload("hermes-recorder")
    end = time.time() + 5
    while time.time() < end and [t for t in threading.enumerate() if t.name.startswith("hermes-recorder")]:
        time.sleep(0.05)
    assert not [t for t in threading.enumerate() if t.name.startswith("hermes-recorder")]
