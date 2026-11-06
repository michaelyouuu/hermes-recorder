"""Which CoreAudio processes carry a chosen app's audio (pure: no macOS imports, testable anywhere).

ScreenCaptureKit can only include apps that own a window, and misses audio played by helper or system processes.
A CoreAudio process tap targets processes directly, so for each chosen app we name the processes to tap:
- apps whose audio runs in a system daemon (FaceTime and iPhone calls play through avconferenced) use a route;
- otherwise any process whose executable lives inside the app's bundle (Zoom's caphost, Chromium/Electron helpers),
  or whose bundle id is the app's id or starts with it plus a dot.
"""
from __future__ import annotations

from dataclasses import dataclass

AVCONFERENCED = "com.apple.avconferenced"
# Apps whose call audio is played by a system daemon instead of the app itself (verified for FaceTime in S5).
ROUTES: dict[str, tuple[str, ...]] = {
    "com.apple.FaceTime": (AVCONFERENCED,),
    "com.apple.mobilephone": (AVCONFERENCED,),  # macOS 26 Phone app (iPhone calls relayed to the Mac)
}
# Apps whose audio runs in a shared system process we can't attribute (Safari -> com.apple.WebKit.GPU).
SCK_ONLY = frozenset({"com.apple.Safari"})


@dataclass(frozen=True)
class Proc:
    obj: int          # CoreAudio process object id
    pid: int
    bundle_id: str
    path: str         # executable path


def matches(bundle_id: str, app_path: str | None, p: Proc) -> bool:
    if p.bundle_id and (p.bundle_id == bundle_id or p.bundle_id.startswith(bundle_id + ".")):
        return True
    if app_path:
        root = app_path.rstrip("/") + "/"
        return p.path.startswith(root)
    return False


def resolve(chosen: list[str], procs: list[Proc], bundle_paths: dict[str, str | None],
            own_pid: int) -> dict[str, list[Proc]]:
    """Map each tappable chosen app to its processes (possibly empty if it isn't playing audio yet).

    Apps in ``SCK_ONLY`` are left out; the caller captures them with ScreenCaptureKit.
    """
    out: dict[str, list[Proc]] = {}
    live = [p for p in procs if p.pid != own_pid]
    for bid in chosen:
        if bid in SCK_ONLY:
            continue
        route = ROUTES.get(bid)
        if route:
            out[bid] = [p for p in live if p.bundle_id in route]
        else:
            out[bid] = [p for p in live if matches(bid, bundle_paths.get(bid), p)]
    return out


def route_daemons(bundle_id: str) -> tuple[str, ...]:
    return ROUTES.get(bundle_id, ())
