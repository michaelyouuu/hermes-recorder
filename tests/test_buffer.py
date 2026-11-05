from __future__ import annotations

import threading
import time

import pytest

from hermes_recorder import protocol
from hermes_recorder.buffer import Entry, RingBuffer, strip_overlap
from conftest import load_fixture


def fill(buf, clock, name, base=None):
    base = clock.t - 60 if base is None else base
    for obj in load_fixture(name):
        obj = dict(obj)
        obj["ts"] = base + obj["ts"]
        import json
        buf.add_message(protocol.decode(json.dumps(obj)))


@pytest.mark.parametrize("prev,new,expected", [
    ("eliminated at this stage because we don't want them in our",
     "because we don't want them in our dataset. The second part is text extraction.",
     "dataset. The second part is text extraction."),
    ("this is the raw HTML of these web pages that are being",
     "these web pages that are being saved by these crawlers.", "saved by these crawlers."),
    ("all this kind of stuff. So this is um", "So this is computer code almost", "computer code almost"),
    ("So when I go to inspect here,", "here. This is what the raw HTML", "This is what the raw HTML"),
    ("I said the", "the end", "the end"),  # a lone short word is not treated as overlap
    ("completely different", "new sentence here", "new sentence here"),
])
def test_strip_overlap_real_s2_windows(prev, new, expected):
    assert strip_overlap(prev, new) == expected


def test_youtube_fixture_merges_overlaps(clock):
    buf = RingBuffer(30, clock=clock)
    fill(buf, clock, "youtube.jsonl")
    text = " ".join(e.text for e in buf.recent(5, ("transcript",)))
    assert text.count("because we don't want them in our") == 1
    assert text.count("these web pages that are being") == 1
    assert "dataset. The second part is text extraction." in text


def test_trim_by_age(clock):
    buf = RingBuffer(1, clock=clock)
    buf.add(Entry("transcript", clock.t - 120, "a", "them", "old words"))
    buf.add(Entry("transcript", clock.t - 10, "a", "them", "new words"))
    assert [e.text for e in buf.recent(10)] == ["new words"]
    assert buf.dropped == 1


def test_max_entries_cap(clock):
    buf = RingBuffer(30, max_entries=100, clock=clock)
    for i in range(250):
        buf.add(Entry("ui_event", clock.t - 250 + i, "a", data={"i": i}))
    assert buf.stats()["counts"]["ui_event"] == 100


def test_out_of_order_insert_and_slice(clock):
    buf = RingBuffer(30, clock=clock)
    buf.add(Entry("ui_event", clock.t - 5, "a", data={"n": 2}))
    buf.add(Entry("ui_event", clock.t - 9, "a", data={"n": 1}))
    assert [e.data["n"] for e in buf.slice(clock.t - 10, clock.t)] == [1, 2]
    assert [e.data["n"] for e in buf.slice(clock.t - 6, clock.t)] == [2]


def test_search_filters_speaker_app_and_terms(clock):
    buf = RingBuffer(30, clock=clock)
    fill(buf, clock, "call.jsonl")
    fill(buf, clock, "youtube.jsonl")
    hits = buf.search("what time thursday", 5)
    assert hits and all("Thursday" in e.text or "thursday" in e.text.lower() for e in hits)
    assert all(e.speaker == "them" for e in buf.search("thursday", 5, speaker="them"))
    assert all(e.app == "us.zoom.xos" for e in buf.search("", 5, app="us.zoom.xos"))
    assert buf.search("zebra", 5) == []


def test_clear_and_span(clock):
    buf = RingBuffer(30, clock=clock)
    assert buf.span() is None
    fill(buf, clock, "call.jsonl")
    t0, t1 = buf.span()
    assert t1 - t0 == pytest.approx(30)
    buf.clear()
    assert buf.span() is None and buf.recent(30) == []


def test_thread_safety():
    buf = RingBuffer(30)

    def writer(k):
        for i in range(500):
            buf.add(Entry("ui_event", time.time(), f"app{k}", data={"i": i}))

    threads = [threading.Thread(target=writer, args=(k,)) for k in range(4)]
    for t in threads:
        t.start()
    for _ in range(200):
        buf.recent(5)
        buf.stats()
    for t in threads:
        t.join()
    assert buf.stats()["counts"]["ui_event"] == 2000


def test_memory_budget_30_minutes(clock):
    """Section 12: buffer under 50 MB for 30 minutes of transcript + events (generous synthetic load)."""
    buf = RingBuffer(30, clock=clock)
    line = "this is a fairly long transcript line with about twenty words in it to be realistic ok"
    for i in range(30 * 60 // 4):  # one transcript line every 4 s, both speakers
        for sp in ("me", "them"):
            buf.add(Entry("transcript", clock.t - 1800 + i * 4, "us.zoom.xos", sp, f"{line} {i}"))
    for i in range(30 * 60 * 2):  # two UI events per second
        buf.add(Entry("ui_event", clock.t - 1800 + i / 2, "com.apple.Notes",
                      data={"kind": "click", "target": {"role": "AXButton", "desc": "New Note"}}))
    assert buf.stats()["approx_bytes"] < 50 * 1024 * 1024


def test_recent_is_fast_on_full_buffer(clock):
    buf = RingBuffer(30, clock=clock)
    for i in range(20000):
        buf.add(Entry("transcript", clock.t - 1800 + i * 0.09, "a", "them", f"word{i} alpha beta gamma"))
    t = time.perf_counter()
    buf.search("alpha", 30)
    assert (time.perf_counter() - t) < 0.05 * 4  # 50 ms budget; slack for slow CI
