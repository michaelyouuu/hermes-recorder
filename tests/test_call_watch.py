from helper.call_watch import CallTracker, CallWatcher, active_calls, call_app


def test_which_processes_count_as_calls():
    assert call_app("com.apple.avconferenced") == "com.apple.FaceTime"
    assert call_app("us.zoom.caphost") == "us.zoom.xos"
    assert call_app("com.google.Chrome.helper") == "com.google.Chrome"  # Meet: the mic is read in a helper
    assert call_app("com.google.Chromeish") is None
    assert call_app("io.github.michaelyouuu.hermes-recorder.helper") is None  # our own mic capture
    assert call_app("com.apple.VoiceMemos") is None
    assert active_calls(["us.zoom.xos", "us.zoom.caphost", "com.apple.Music"]) == {"us.zoom.xos"}


def test_tracker_debounces_start_and_end():
    t = CallTracker(start_s=3, end_s=8)
    assert t.update(0, {"us.zoom.xos"}) == []
    assert t.update(2, {"us.zoom.xos"}) == []
    assert t.update(3, {"us.zoom.xos"}) == [("start", "us.zoom.xos")]
    assert t.update(5, set()) == []           # muted for a moment
    assert t.update(7, {"us.zoom.xos"}) == []  # back: still the same call
    assert t.update(14, set()) == []
    assert t.update(15, set()) == [("end", "us.zoom.xos")]
    assert t.update(16, {"com.google.Chrome"}) == [] and t.update(17, set()) == []  # a 1 s blip is not a call
    assert t.update(30, set()) == [] and t.calls == set()


def test_watcher_reports_events_from_a_fake_input_source():
    seq = iter([["us.zoom.xos"]] * 3 + [[]] * 6)
    events = []
    w = CallWatcher(lambda k, a: events.append((k, a)), inputs=lambda: next(seq, []), every=0.01)
    w.tracker = CallTracker(start_s=0.015, end_s=0.03)
    w.start()
    import time
    end = time.time() + 2
    while time.time() < end and len(events) < 2:
        time.sleep(0.01)
    w.stop()
    assert events == [("start", "us.zoom.xos"), ("end", "us.zoom.xos")]
