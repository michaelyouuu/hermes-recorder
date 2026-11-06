from helper.proc_match import Proc, SCK_ONLY, resolve

# Taken from a real CoreAudio process list during a FaceTime call (S5).
PROCS = [
    Proc(10, 814, "com.apple.avconferenced", "/usr/libexec/avconferenced"),
    Proc(11, 61389, "com.apple.FaceTime", "/System/Applications/FaceTime.app/Contents/MacOS/FaceTime"),
    Proc(12, 46412, "us.zoom.xos", "/Applications/zoom.us.app/Contents/MacOS/zoom.us"),
    Proc(13, 46418, "us.zoom.caphost",
         "/Applications/zoom.us.app/Contents/Frameworks/caphost.app/Contents/MacOS/caphost"),
    Proc(14, 46342, "com.microsoft.edgemac", "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
    Proc(15, 47461, "com.microsoft.edgemac.helper",
         "/Applications/Microsoft Edge.app/Contents/Frameworks/Microsoft Edge Framework.framework/Versions/1/"
         "Helpers/Microsoft Edge Helper.app/Contents/MacOS/Microsoft Edge Helper"),
    Proc(16, 4987, "com.apple.WebKit.GPU",
         "/System/Library/Frameworks/WebKit.framework/Versions/A/XPCServices/com.apple.WebKit.GPU.xpc/Contents/MacOS/"
         "com.apple.WebKit.GPU"),
    Proc(17, 49874, "io.github.michaelyouuu.hermes-recorder.helper",
         "/x/Hermes Recorder Helper.app/Contents/MacOS/hermes-recorder-helper"),
]
PATHS = {"us.zoom.xos": "/Applications/zoom.us.app", "com.microsoft.edgemac": "/Applications/Microsoft Edge.app",
         "com.apple.FaceTime": "/System/Applications/FaceTime.app"}


def pids(r, bid):
    return sorted(p.pid for p in r[bid])


def test_facetime_routes_to_avconferenced_not_the_app():
    r = resolve(["com.apple.FaceTime"], PROCS, PATHS, own_pid=1)
    assert pids(r, "com.apple.FaceTime") == [814]


def test_phone_app_routes_to_avconferenced():
    assert pids(resolve(["com.apple.mobilephone"], PROCS, {}, 1), "com.apple.mobilephone") == [814]


def test_zoom_includes_caphost_by_bundle_path():
    assert pids(resolve(["us.zoom.xos"], PROCS, PATHS, 1), "us.zoom.xos") == [46412, 46418]


def test_bundle_id_prefix_without_known_path():
    r = resolve(["com.microsoft.edgemac"], PROCS, {}, 1)
    assert pids(r, "com.microsoft.edgemac") == [46342, 47461]


def test_prefix_needs_a_dot():
    procs = [Proc(1, 5, "us.zoom.xosother", "/elsewhere/x")]
    assert resolve(["us.zoom.xos"], procs, {}, 1)["us.zoom.xos"] == []


def test_own_process_never_tapped():
    bid = "io.github.michaelyouuu.hermes-recorder.helper"
    assert resolve([bid], PROCS, {}, own_pid=49874)[bid] == []


def test_safari_left_to_screencapturekit():
    assert "com.apple.Safari" in SCK_ONLY
    assert resolve(["com.apple.Safari"], PROCS, {}, 1) == {}


def test_app_not_playing_yet_maps_to_empty_list():
    assert resolve(["com.tinyspeck.slackmacgap"], PROCS, {}, 1) == {"com.tinyspeck.slackmacgap": []}


def test_shared_webkit_gpu_not_claimed_by_other_apps():
    r = resolve(["us.zoom.xos", "com.microsoft.edgemac"], PROCS, PATHS, 1)
    assert all(p.pid != 4987 for v in r.values() for p in v)
