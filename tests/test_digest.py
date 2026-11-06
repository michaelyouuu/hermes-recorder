from __future__ import annotations

import json
import time

from hermes_recorder import ask, digest, runtime, tools
from hermes_recorder.buffer import Entry, RingBuffer


def speech(minutes=30, every=6.0, now=None):
    """~150 words per minute: one 15-word line every 6 s, on Zoom."""
    now = now or time.time()
    b = RingBuffer(120)
    n = int(minutes * 60 / every)
    for i in range(n):
        b.add(Entry("transcript", now - minutes * 60 + 5 + i * every, "us.zoom.xos", "them",
                    f"line {i} " + "word " * 15))
    return b, now


class LLM:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def complete(self, messages, **kw):
        self.calls.append((messages, kw))
        if self.fail:
            raise RuntimeError("provider down")
        first = messages[-1]["content"].split("\n")[0]
        return type("R", (), {"text": f"summary of {first[:30]}"})()


def setup_function():
    digest.clear_cache()


def test_short_window_is_verbatim_and_makes_no_calls():
    b, now = speech(minutes=3)
    llm = LLM()
    lines, note = digest.build(b.search("", 3), 24000, now, llm=llm, runner=lambda f, *a, **k: f(*a, **k))
    assert note is None and llm.calls == [] and all(ln["speaker"] == "them" for ln in lines)
    assert len(lines) == len(b.search("", 3))


def test_long_window_covers_the_start_with_cached_summaries():
    b, now = speech(minutes=30)
    llm = LLM()
    run = lambda f, *a, **k: f(*a, **k)
    lines, note = digest.build(b.search("", 30), 24000, now, llm=llm, runner=run)
    summaries = [ln for ln in lines if ln["speaker"] == "summary"]
    assert summaries and "summarized" in note
    assert summaries[0]["t"].startswith("-30:00") or summaries[0]["t"].startswith("-29:")
    assert "line 0 " in llm.calls[0][0][1]["content"]  # the very first line was summarized, not dropped
    assert sum(len(ln["text"]) + 40 for ln in lines) < 24000 * 1.2
    assert lines.index(summaries[-1]) == len(summaries) - 1  # summaries first, then verbatim recent lines
    assert all(k["purpose"].startswith("hermes-recorder") for _, k in llm.calls)
    n = len(llm.calls)
    digest.build(b.search("", 30), 24000, now, llm=llm, runner=run)
    assert len(llm.calls) == n  # cached


def test_long_window_without_a_model_says_what_it_covers():
    b, now = speech(minutes=30)
    lines, note = digest.build(b.search("", 30), 24000, now, llm=None)
    assert "Only the last" in note and lines
    lines, note = digest.build(b.search("", 30), 24000, now, llm=LLM(fail=True), runner=lambda f, *a, **k: f(*a, **k))
    assert "provider down" in note and all(ln["speaker"] != "summary" for ln in lines)


def test_screen_recall_digests_long_windows(monkeypatch):
    b, now = speech(minutes=30)
    rec = type("R", (), {"buffer": b, "maybe_autostart": lambda self: None,
                         "manager": type("M", (), {"state": "running"})()})()
    monkeypatch.setattr(runtime, "get", lambda: rec)
    llm = LLM()
    monkeypatch.setattr(runtime, "llm", lambda: llm)
    out = json.loads(tools.screen_recall({"minutes": 30}))
    assert out["truncated"] and "summarized" in out["note"]
    assert out["lines"][0]["speaker"] == "summary" and "line 0 " in llm.calls[0][0][1]["content"]


def test_panel_ask_sends_follow_up_history():
    b, now = speech(minutes=2)
    rec = type("R", (), {"buffer": b})()
    llm = LLM()
    hist = [{"q": "who is speaking?", "a": "The host."}, {"q": "", "a": "skipped"}, "junk"]
    ask.answer("what did he mean by that?", 2, rec=rec, llm=llm, history=hist)
    msgs = llm.calls[-1][0]
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "user"]
    assert msgs[1]["content"] == "who is speaking?" and "Question: what did he mean by that?" in msgs[-1]["content"]
