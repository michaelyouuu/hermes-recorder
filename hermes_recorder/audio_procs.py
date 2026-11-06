"""Which processes are playing or recording audio right now (CoreAudio process objects, macOS 14+).

Stdlib ctypes only, so `hermes recorder check` can say where sound is actually coming from when the app it was asked
to record is silent. Reads CoreAudio's per-process "running output/input" flags; never touches audio.
"""
from __future__ import annotations

import ctypes
import re
import sys


def _fourcc(s: str) -> int:
    return int.from_bytes(s.encode(), "big")


class _Addr(ctypes.Structure):
    _fields_ = [("mSelector", ctypes.c_uint32), ("mScope", ctypes.c_uint32), ("mElement", ctypes.c_uint32)]


def app_label(path: str) -> str:
    """'/Applications/zoom.us.app/.../caphost.app/Contents/MacOS/caphost' -> 'zoom.us (caphost)'."""
    apps = re.findall(r"([^/]+)\.app/", path + "/")
    if not apps:
        return path.rsplit("/", 1)[-1] or "?"
    return apps[0] if len(apps) == 1 else f"{apps[0]} ({apps[-1]})"


def active() -> list[dict]:
    """[{pid, path, label, output, input}] for processes using audio. Empty off macOS or on any error."""
    if sys.platform != "darwin":
        return []
    try:
        ca = ctypes.CDLL("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
        glob = _fourcc("glob")

        def get(obj: int, sel: str, ctype):
            v = ctype()
            size = ctypes.c_uint32(ctypes.sizeof(v))
            err = ca.AudioObjectGetPropertyData(obj, ctypes.byref(_Addr(_fourcc(sel), glob, 0)), 0, None,
                                                ctypes.byref(size), ctypes.byref(v))
            return None if err else v.value

        a = _Addr(_fourcc("prs#"), glob, 0)
        size = ctypes.c_uint32(0)
        if ca.AudioObjectGetPropertyDataSize(1, ctypes.byref(a), 0, None, ctypes.byref(size)):
            return []
        arr = (ctypes.c_uint32 * (size.value // 4))()
        if ca.AudioObjectGetPropertyData(1, ctypes.byref(a), 0, None, ctypes.byref(size), arr):
            return []
        out = []
        for obj in arr:
            o, i = get(obj, "piro", ctypes.c_uint32), get(obj, "piri", ctypes.c_uint32)
            if not (o or i):
                continue
            pid = get(obj, "ppid", ctypes.c_int32) or 0
            buf = ctypes.create_string_buffer(4096)
            n = libproc.proc_pidpath(pid, buf, 4096)
            path = buf.value[:n].decode(errors="replace") if n > 0 else "?"
            out.append({"pid": pid, "path": path, "label": app_label(path), "output": bool(o), "input": bool(i)})
        return out
    except Exception:
        return []
