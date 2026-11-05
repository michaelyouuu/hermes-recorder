"""S4 (throwaway): which processes are playing or recording audio right now, according to CoreAudio.

Uses the macOS 14+ process objects (kAudioHardwarePropertyProcessObjectList) via ctypes, no extra deps.
  python spikes/s4_audio_processes.py [--watch 10]
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import time

CA = ctypes.CDLL("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
LIBPROC = ctypes.CDLL("/usr/lib/libproc.dylib")


def fourcc(s: str) -> int:
    return int.from_bytes(s.encode(), "big")


class Addr(ctypes.Structure):
    _fields_ = [("mSelector", ctypes.c_uint32), ("mScope", ctypes.c_uint32), ("mElement", ctypes.c_uint32)]


SYSTEM = 1
GLOBAL = fourcc("glob")
MAIN = 0
PROCESS_LIST = fourcc("prs#")
PID = fourcc("ppid")
BUNDLE = fourcc("pbid")
RUN_IN = fourcc("piri")
RUN_OUT = fourcc("piro")

CA.AudioObjectGetPropertyDataSize.argtypes = [ctypes.c_uint32, ctypes.POINTER(Addr), ctypes.c_uint32, ctypes.c_void_p,
                                              ctypes.POINTER(ctypes.c_uint32)]
CA.AudioObjectGetPropertyData.argtypes = [ctypes.c_uint32, ctypes.POINTER(Addr), ctypes.c_uint32, ctypes.c_void_p,
                                          ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]


def get(obj: int, sel: int, ctype):
    a = Addr(sel, GLOBAL, MAIN)
    v = ctype()
    size = ctypes.c_uint32(ctypes.sizeof(v))
    err = CA.AudioObjectGetPropertyData(obj, ctypes.byref(a), 0, None, ctypes.byref(size), ctypes.byref(v))
    return None if err else v.value


def processes() -> list[int]:
    a = Addr(PROCESS_LIST, GLOBAL, MAIN)
    size = ctypes.c_uint32(0)
    if CA.AudioObjectGetPropertyDataSize(SYSTEM, ctypes.byref(a), 0, None, ctypes.byref(size)):
        return []
    n = size.value // 4
    arr = (ctypes.c_uint32 * n)()
    if CA.AudioObjectGetPropertyData(SYSTEM, ctypes.byref(a), 0, None, ctypes.byref(size), arr):
        return []
    return list(arr)


def path(pid: int) -> str:
    buf = ctypes.create_string_buffer(4096)
    n = LIBPROC.proc_pidpath(pid, buf, 4096)
    return buf.value[:n].decode() if n > 0 else "?"


def snapshot() -> list[dict]:
    out = []
    for obj in processes():
        pid = get(obj, PID, ctypes.c_int32)
        o, i = get(obj, RUN_OUT, ctypes.c_uint32), get(obj, RUN_IN, ctypes.c_uint32)
        if o or i:
            out.append({"pid": pid, "out": bool(o), "in": bool(i), "path": path(pid) if pid else "?"})
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", type=float, default=0)
    a = ap.parse_args()
    end = time.time() + a.watch
    seen: dict[tuple, dict] = {}
    while True:
        for p in snapshot():
            seen[(p["pid"], p["out"], p["in"])] = p
        if time.time() >= end:
            break
        time.sleep(0.5)
    for p in sorted(seen.values(), key=lambda p: p["pid"]):
        print(f"pid {p['pid']:>6}  out={int(p['out'])} in={int(p['in'])}  {p['path']}")
