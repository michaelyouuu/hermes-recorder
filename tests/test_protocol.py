from __future__ import annotations

import json

import pytest

from hermes_recorder import protocol as p
from conftest import load_fixture


def test_round_trip():
    m = p.Message("transcript", 12.5, {"app": "us.zoom.xos", "speaker": "them", "text": "héllo"})
    back = p.decode(p.encode(m))
    assert back.type == "transcript" and back.ts == 12.5 and back.get("text") == "héllo"
    assert p.encode(m).endswith(b"\n") and p.encode(m).count(b"\n") == 1


@pytest.mark.parametrize("name", ["youtube.jsonl", "call.jsonl", "teach_notes.jsonl"])
def test_fixtures_are_valid(name):
    for obj in load_fixture(name):
        p.decode(json.dumps(obj))


@pytest.mark.parametrize("line,err", [
    (b"not json", "not json"),
    (b"[1,2]", "not an object"),
    (json.dumps({"v": 2, "type": "status", "ts": 1}), "version"),
    (json.dumps({"v": 1, "type": "nope", "ts": 1}), "unknown type"),
    (json.dumps({"v": 1, "type": "status", "ts": "x"}), "ts must"),
    (json.dumps({"v": 1, "type": "status", "ts": True}), "ts must"),
    (json.dumps({"v": 1, "type": "transcript", "ts": 1, "app": "a", "speaker": "x", "text": "t"}), "speaker"),
    (json.dumps({"v": 1, "type": "transcript", "ts": 1, "app": "a"}), "missing"),
    (json.dumps({"v": 1, "type": "control", "ts": 1, "op": "rm -rf"}), "control op"),
    (b"\xff\xfe", "utf-8"),
    (b"x" * (p.MAX_LINE_BYTES + 1), "too long"),
])
def test_rejects_bad_lines(line, err):
    with pytest.raises(p.ProtocolError, match=err):
        p.decode(line)


def test_control_builder():
    assert p.decode(p.encode(p.control("set_apps", 1.0, ["com.google.Chrome"]))).get("apps") == ["com.google.Chrome"]
    with pytest.raises(p.ProtocolError):
        p.control("explode", 1.0)
