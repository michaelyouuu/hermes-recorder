from __future__ import annotations

import json
import os
import time

import pytest

from hermes_recorder import commands, digest, notes, registration, runtime
from hermes_recorder.buffer import Entry, RingBuffer
from test_tools import FakeCtx

NOTE = {"title": "Learning rate warmup in transformers", "summary": "The lecture explained warmup.",
        "key_points": ["Warmup avoids early divergence", "Peak lr was 3e-4"],
        "details": [{"heading": "Why warm up", "text": "Adam's early updates are noisy."}],
        "action_items": [], "open_questions": ["How long should warmup be?"], "tags": ["ml", "Optimization"]}


class LLM:
    def __init__(self, parsed=NOTE):
        self.parsed, self.calls = parsed, []

    def complete_structured(self, **kw):
        self.calls.append(kw)
        return type("R", (), {"parsed": self.parsed, "content_type": "json"})()

    def complete(self, messages, **kw):
        return type("R", (), {"text": "older part summary"})()


def run(f, *a, **k):
    return f(*a, **k)


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    c = FakeCtx({"notes_dir": str(tmp_path / "Notes")})
    registration.register_all(c)
    monkeypatch.setenv("HOME", str(tmp_path))  # the fallback folder lands in tmp too
    digest.clear_cache()
    yield c
    runtime.reset_for_tests(None)


def recorder(minutes=6):
    b = RingBuffer(90)
    now = time.time()
    for i in range(int(minutes * 10)):
        b.add(Entry("transcript", now - minutes * 60 + 5 + i * 6, "com.microsoft.edgemac", "them",
                    f"point {i}: secret transcript words"))
    b.add(Entry("transcript", now - 2, None, "me", "I should try warmup"))
    return type("R", (), {"buffer": b, "started_at": None, "last_started_at": now - 120})()


def test_save_writes_a_markdown_note_without_the_transcript(ctx, tmp_path):
    rec, llm = recorder(), LLM()
    r = notes.save(None, "CS lecture", rec=rec, llm=llm, runner=run)
    assert "error" not in r, r
    p = tmp_path / "Notes" / os.path.basename(r["path"])
    assert p.exists() and oct(p.stat().st_mode & 0o777) == "0o600"
    text = p.read_text()
    assert text.startswith("---\ntitle: \"Learning rate warmup in transformers\"\n")
    assert "generator: hermes-recorder" in text and "tags: [ml, optimization]" in text
    assert "sources: [\"Microsoft Edge\", \"You\"]" in text
    assert "## Key points\n\n- Warmup avoids early divergence" in text and "## Open questions" in text
    assert "## Action items" not in text  # empty sections are left out
    assert "secret transcript words" not in text  # derived notes only
    # default window = the last session (2 min), not the whole buffer
    sent = llm.calls[0]["input"][0]["text"]
    assert "point 59:" in sent and "point 30:" not in sent and "CS lecture" in sent
    assert llm.calls[0]["json_schema"] is notes.NOTE_SCHEMA


def test_never_overwrites_and_reports_errors(ctx):
    rec = recorder()
    a = notes.save(5, rec=rec, llm=LLM(), runner=run)
    b = notes.save(5, rec=rec, llm=LLM(), runner=run)
    assert a["path"] != b["path"] and b["path"].endswith("(2).md")
    assert "not enough" in notes.save(5, rec=type("R", (), {"buffer": RingBuffer(30)})(), llm=LLM())["error"]
    assert "expected shape" in notes.save(5, rec=rec, llm=LLM(parsed=None), runner=run)["error"]

    class Boom(LLM):
        def complete_structured(self, **kw):
            raise RuntimeError("provider down")

    assert "provider down" in notes.save(5, rec=rec, llm=Boom(), runner=run)["error"]


def test_unwritable_folder_falls_back(ctx, tmp_path):
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    ctx.config["notes_dir"] = str(ro / "Notes")
    try:
        r = notes.save(5, rec=recorder(), llm=LLM(), runner=run)
        assert r["path"].startswith(str(tmp_path / "Hermes Notes")) and "instead" in r["note"]
    finally:
        ro.chmod(0o700)


def test_search_and_read(ctx, tmp_path):
    rec = recorder()
    notes.save(5, rec=rec, llm=LLM(), runner=run)
    other = dict(NOTE, title="Budget call with Sam", tags=["finance"], summary="We agreed on 3 PM Thursday.",
                 key_points=["Ship by Friday"], details=[], open_questions=[])
    notes.save(5, rec=rec, llm=LLM(other), runner=run)
    (tmp_path / "Notes" / "mine.md").write_text("# my own file\nwarmup")  # not ours: ignored

    r = notes.search("what did I learn about warmup?")
    assert [n["title"] for n in r["notes"]] == ["Learning rate warmup in transformers"]
    assert "Adam's early updates" in r["notes"][0]["body"]  # single hit comes back in full
    assert len(notes.search("")["notes"]) == 2
    assert "No saved notes matched" in notes.search("kubernetes")["note"]

    assert notes.read(r["notes"][0]["path"])["title"].startswith("Learning rate")
    assert "isn't a note" in notes.read(str(tmp_path / "elsewhere.md"))["error"]
    assert "isn't a note" in notes.read(str(tmp_path / "Notes" / ".." / "x.md"))["error"]
    assert "isn't a hermes-recorder note" in notes.read(str(tmp_path / "Notes" / "mine.md"))["error"]


def test_tools_and_command_never_raise(ctx, monkeypatch):
    rec = recorder()
    monkeypatch.setattr(runtime, "get", lambda: rec)
    ctx.llm = LLM()
    out = json.loads(ctx.tools["recorder_save_notes"]({"title_hint": "lecture"}))
    assert out["title"].startswith("Learning rate")
    found = json.loads(ctx.tools["recorder_notes"]({"query": "warmup"}))
    assert found["notes"][0]["path"] == out["path"]
    assert json.loads(ctx.tools["recorder_notes"]({"path": "/etc/passwd"}))["error"]
    assert json.loads(ctx.tools["recorder_notes"]({"days": "soon"}))["notes"]
    assert commands.recorder("notes 5").startswith("Saved notes: Learning rate")
    assert commands.recorder("notes abc").startswith("Usage")
    monkeypatch.setattr(runtime, "get", lambda: 1 / 0)
    assert "ZeroDivisionError" in json.loads(ctx.tools["recorder_save_notes"]({}))["error"]


MEETING_NOTE = dict(NOTE, title="Q3 launch sync", summary="We set the launch plan.",
                    decisions=["Launch moves to Nov 20"],
                    action_items=[{"task": "File the onboarding bug in Linear", "owner": "you", "due": "Friday"},
                                  {"task": "Send pricing deck", "owner": "Sam", "due": ""}, "Book the venue"],
                    open_questions=[])


def test_calls_get_meeting_notes_that_lead_with_actions(ctx, tmp_path):
    b = RingBuffer(90)
    now = time.time()
    for i in range(6):
        b.add(Entry("transcript", now - 100 + i * 10, "us.zoom.xos", "them", f"item {i}: let's ship"))
    rec = type("R", (), {"buffer": b, "started_at": now - 110, "last_started_at": now - 110})()
    llm = LLM(MEETING_NOTE)
    r = notes.save(None, rec=rec, llm=llm, runner=run)
    assert r["kind"] == "meeting" and "meeting or call" in llm.calls[0]["instructions"]
    assert r["action_items"][0] == {"task": "File the onboarding bug in Linear", "owner": "you", "due": "Friday"}
    assert r["action_items"][2] == {"task": "Book the venue", "owner": "", "due": ""}
    text = open(r["path"]).read()
    assert "kind: meeting" in text
    assert text.index("## Decisions") < text.index("## Action items") < text.index("## Key points")
    assert "- [ ] File the onboarding bug in Linear (owner: you, due: Friday)" in text
    assert "- [ ] Send pricing deck (owner: Sam)" in text
    assert notes.search("onboarding bug")["notes"][0]["kind"] == "meeting"


def test_meeting_detection():
    e = lambda app, sp: Entry("transcript", 0, app, sp, "x")
    assert notes.is_meeting([e("com.apple.FaceTime", "them")])
    assert notes.is_meeting([e("com.google.Chrome", "them")] * 10 + [e(None, "me")] * 3)  # Meet in a browser
    assert not notes.is_meeting([e("com.google.Chrome", "them")] * 30 + [e(None, "me")] * 3)  # a video
