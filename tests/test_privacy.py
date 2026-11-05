from __future__ import annotations

from hermes_recorder import privacy
from hermes_recorder.protocol import Message

ALLOW = ["com.apple.Notes", "us.zoom.xos", "com.1password.1password"]


def test_mask_patterns():
    assert privacy.mask("card 4111 1111 1111 1111 ok") == "card {CARD} ok"
    assert privacy.mask("key sk-abcdefghijklmnop1234") == "key {API_KEY}"
    assert "{TOKEN}" in privacy.mask("gs_lcrp=EgZjaHJvbWUyBggAEEUYOTIGCAEQRRhA0gEIMTIzNGowajeoAgCwAgA")
    assert privacy.mask("482913") == "{CODE}"
    assert privacy.mask("meet at 3 PM Thursday") == "meet at 3 PM Thursday"


def test_denylist_wins_over_allowlist():
    assert privacy.clean_apps(["us.zoom.xos", "com.1password.1password", " ", "us.zoom.xos"]) == ["us.zoom.xos"]
    m = Message("ui_event", 1, {"app": "com.1password.1password", "kind": "click"})
    assert privacy.sanitize(m, ALLOW) is None


def test_only_allowlisted_apps_pass():
    assert privacy.sanitize(Message("transcript", 1, {"app": "com.spotify.client", "speaker": "them", "text": "x"}), ALLOW) is None
    assert privacy.sanitize(Message("transcript", 1, {"app": "us.zoom.xos", "speaker": "them", "text": "x"}), ALLOW)


def test_dock_kept_as_context_for_events_only():
    click = Message("ui_event", 1, {"app": "com.apple.dock", "kind": "click", "target": {"title": "Notes"}})
    assert privacy.sanitize(click, ALLOW) is not None
    audio = Message("transcript", 1, {"app": "com.apple.dock", "speaker": "them", "text": "x"})
    assert privacy.sanitize(audio, ALLOW) is None


def test_secure_field_never_keeps_input():
    m = Message("ui_event", 1, {"app": "com.apple.Notes", "kind": "typed", "inserted": "hunter2",
                                "target": {"role": "AXTextField", "subrole": "AXSecureTextField"}})
    out = privacy.sanitize(m, ALLOW)
    assert out.get("value") == privacy.SECRET and "inserted" not in out.fields
    assert "hunter2" not in repr(out.fields)


def test_transcript_and_typed_values_are_masked():
    t = privacy.sanitize(Message("transcript", 1, {"app": "us.zoom.xos", "speaker": "me",
                                                   "text": "my card is 4111 1111 1111 1111"}), ALLOW)
    assert "4111" not in t.get("text")
    u = privacy.sanitize(Message("ui_event", 1, {"app": "com.apple.Notes", "kind": "typed", "inserted": "4111111111111111"}), ALLOW)
    assert u.get("inserted") == "{CARD}"
