from __future__ import annotations

from hermes_recorder import cli, guide, setup


def perms(**kw):
    p = {"screen_capture": True, "microphone": "authorized", "accessibility": False, "input_monitoring": False}
    p.update(kw)
    return p


class Mac:
    """Fake helper preflight: the user switches each permission on after its Settings page opens."""

    def __init__(self, p, flip_after=2, never=()):
        self.p, self.flip_after, self.never = p, flip_after, set(never)
        self.calls, self.opened, self.polls = [], [], 0

    def preflight(self, paths, request, events):
        self.calls.append((request, events))
        if self.opened and not request:
            self.polls += 1
            key = self.opened[-1]
            if self.polls >= self.flip_after and key not in self.never:
                self.p[key] = True
        return {"pid": 1, "permissions": dict(self.p)}

    def open(self, url):
        key = next(k for k, u in guide.SETTINGS.items() if u == url)
        self.opened.append(key)
        self.polls = 0


def test_walks_through_each_missing_permission(tmp_path):
    mac, said = Mac(perms()), []
    ok = guide.ensure(setup.Paths(tmp_path), guide.TEACH, preflight=mac.preflight, open_url=mac.open,
                      out=said.append, sleep=lambda s: None)
    assert ok and mac.opened == ["accessibility", "input_monitoring"]
    assert mac.calls[0] == (True, True)  # asks macOS first, so the app is listed in Settings
    assert "  ✓ Accessibility" in said and "  ✓ Input Monitoring" in said


def test_nothing_to_do_when_granted(tmp_path):
    mac = Mac(perms(accessibility=True, input_monitoring=True))
    assert guide.ensure(setup.Paths(tmp_path), guide.TEACH, preflight=mac.preflight, open_url=mac.open,
                        out=lambda s: None, sleep=lambda s: None)
    assert mac.opened == []


def test_gives_up_with_a_clear_next_step(tmp_path):
    mac, said = Mac(perms(), never={"accessibility"}), []
    ok = guide.ensure(setup.Paths(tmp_path), guide.TEACH, preflight=mac.preflight, open_url=mac.open,
                      out=said.append, sleep=lambda s: None, wait_s=0.05)
    assert not ok and mac.opened == ["accessibility"]
    assert any("hermes recorder doctor" in s for s in said)


def test_teach_on_sets_config_only_after_permissions(tmp_path, monkeypatch):
    saved = {}
    ctx = type("C", (), {"set_config": lambda self, k, v: saved.__setitem__(k, v)})()
    monkeypatch.setattr(cli.runtime, "_ctx", ctx)
    monkeypatch.setattr(cli.setup, "build_is_current", lambda *a: (True, "ok"))
    monkeypatch.setattr(cli.guide, "ensure", lambda paths, keys: False)
    assert cli._teach("on", setup.Paths(tmp_path)) == 1 and saved == {}
    monkeypatch.setattr(cli.guide, "ensure", lambda paths, keys: keys == guide.TEACH)
    assert cli._teach("on", setup.Paths(tmp_path)) == 0 and saved == {"capture_events": True}
    assert cli._teach("off", setup.Paths(tmp_path)) == 0 and saved == {"capture_events": False}


def test_duplicate_helper_apps_are_found(tmp_path):
    paths = setup.Paths(tmp_path)
    out = f"{paths.app}\n/Users/x/old/Hermes Recorder Helper.app\n"
    run = lambda *a, **k: type("R", (), {"stdout": out})()
    assert setup.duplicate_apps(paths, run=run) == ["/Users/x/old/Hermes Recorder Helper.app"]


def test_audio_process_labels():
    from hermes_recorder import audio_procs
    assert audio_procs.app_label("/Applications/zoom.us.app/Contents/Frameworks/caphost.app/Contents/MacOS/caphost") \
        == "zoom.us (caphost)"
    assert audio_procs.app_label("/usr/libexec/avconferenced") == "avconferenced"
    assert isinstance(audio_procs.active(), list)
