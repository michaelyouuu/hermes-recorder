"""S5 (throwaway): capture call audio with a CoreAudio process tap (macOS 14.2+), for apps SCK can't include.

FaceTime and iPhone calls play through /usr/libexec/avconferenced, which owns no window, so ScreenCaptureKit's
per-app filter can't include it. A process tap targets CoreAudio process objects (by pid) directly.

Run inside the helper app (so TCC attributes it to "Hermes Recorder Helper"), output goes to --out:
  open -n -g -W --env ... -a "Hermes Recorder Helper.app" --args spikes/s5_process_tap.py \
      --match avconferenced --seconds 20 --out /tmp/s5.json
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
import threading
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402
import objc  # noqa: E402
import CoreAudio as C  # noqa: E402
from Foundation import NSUUID  # noqa: E402

from spikes.s4_audio_processes import CA, Addr, GLOBAL, MAIN, fourcc, get, path, processes  # noqa: E402

PID = fourcc("ppid")
RUN_IN = fourcc("piri")
RUN_OUT = fourcc("piro")
TAP_FORMAT = fourcc("tfmt")
DEFAULT_OUT = fourcc("dOut")
DEVICE_UID = fourcc("uid ")
SYSTEM = 1


class ASBD(ctypes.Structure):
    _fields_ = [("mSampleRate", ctypes.c_double), ("mFormatID", ctypes.c_uint32), ("mFormatFlags", ctypes.c_uint32),
                ("mBytesPerPacket", ctypes.c_uint32), ("mFramesPerPacket", ctypes.c_uint32),
                ("mBytesPerFrame", ctypes.c_uint32), ("mChannelsPerFrame", ctypes.c_uint32),
                ("mBitsPerChannel", ctypes.c_uint32), ("mReserved", ctypes.c_uint32)]


class AudioBuffer(ctypes.Structure):
    _fields_ = [("mNumberChannels", ctypes.c_uint32), ("mDataByteSize", ctypes.c_uint32), ("mData", ctypes.c_void_p)]


class AudioBufferList(ctypes.Structure):
    _fields_ = [("mNumberBuffers", ctypes.c_uint32), ("mBuffers", AudioBuffer * 8)]


IOPROC = ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(AudioBufferList),
                          ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)

CA.AudioHardwareCreateProcessTap.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
CA.AudioHardwareDestroyProcessTap.argtypes = [ctypes.c_uint32]
CA.AudioHardwareCreateAggregateDevice.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
CA.AudioHardwareDestroyAggregateDevice.argtypes = [ctypes.c_uint32]
CA.AudioDeviceCreateIOProcID.argtypes = [ctypes.c_uint32, IOPROC, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
CA.AudioDeviceDestroyIOProcID.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
CA.AudioDeviceStart.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
CA.AudioDeviceStop.argtypes = [ctypes.c_uint32, ctypes.c_void_p]


def get_struct(obj: int, sel: int, ctype):
    v = ctype()
    size = ctypes.c_uint32(ctypes.sizeof(v))
    err = CA.AudioObjectGetPropertyData(obj, ctypes.byref(Addr(sel, GLOBAL, MAIN)), 0, None, ctypes.byref(size),
                                        ctypes.byref(v))
    return err, v


def get_cfstring(obj: int, sel: int) -> str | None:
    v = ctypes.c_void_p()
    size = ctypes.c_uint32(ctypes.sizeof(v))
    err = CA.AudioObjectGetPropertyData(obj, ctypes.byref(Addr(sel, GLOBAL, MAIN)), 0, None, ctypes.byref(size),
                                        ctypes.byref(v))
    if err or not v.value:
        return None
    return str(objc.objc_object(c_void_p=v.value))


def find_targets(match: list[str]) -> list[dict]:
    out = []
    for obj in processes():
        pid = get(obj, PID, ctypes.c_int32)
        p = path(pid) if pid else "?"
        if any(m in p for m in match):
            out.append({"obj": obj, "pid": pid, "path": p, "out": bool(get(obj, RUN_OUT, ctypes.c_uint32)),
                        "in": bool(get(obj, RUN_IN, ctypes.c_uint32))})
    return out


def run(match: list[str], seconds: float, transcribe: bool) -> dict:
    res: dict = {"match": match, "macos": os.uname().release}
    targets = find_targets(match)
    res["targets"] = [{k: t[k] for k in ("pid", "path", "out", "in")} for t in targets]
    if not targets:
        res["error"] = "no CoreAudio process matched"
        return res

    desc = C.CATapDescription.alloc().initStereoMixdownOfProcesses_([t["obj"] for t in targets])
    tap_uuid = NSUUID.UUID()
    desc.setUUID_(tap_uuid)
    desc.setPrivate_(True)
    desc.setMuteBehavior_(0)  # CATapUnmuted: the user still hears the call
    tap_id = ctypes.c_uint32(0)
    err = CA.AudioHardwareCreateProcessTap(objc.pyobjc_id(desc), ctypes.byref(tap_id))
    res["create_tap"] = err
    if err:
        res["error"] = f"AudioHardwareCreateProcessTap failed: {err}"
        return res
    agg_id = ctypes.c_uint32(0)
    proc_id = ctypes.c_void_p()
    try:
        err, fmt = get_struct(tap_id.value, TAP_FORMAT, ASBD)
        res["tap_format"] = {"err": err, "rate": fmt.mSampleRate, "channels": fmt.mChannelsPerFrame,
                             "bits": fmt.mBitsPerChannel, "flags": fmt.mFormatFlags}
        err, dev = get_struct(SYSTEM, DEFAULT_OUT, ctypes.c_uint32)
        out_uid = get_cfstring(dev.value, DEVICE_UID)
        res["output_device_uid"] = out_uid
        agg = {
            C.kAudioAggregateDeviceNameKey: "Hermes Recorder Tap",
            C.kAudioAggregateDeviceUIDKey: NSUUID.UUID().UUIDString(),
            C.kAudioAggregateDeviceMainSubDeviceKey: out_uid,
            C.kAudioAggregateDeviceIsPrivateKey: True,
            C.kAudioAggregateDeviceIsStackedKey: False,
            C.kAudioAggregateDeviceTapAutoStartKey: True,
            C.kAudioAggregateDeviceSubDeviceListKey: [{C.kAudioSubDeviceUIDKey: out_uid}],
            C.kAudioAggregateDeviceTapListKey: [{C.kAudioSubTapDriftCompensationKey: True,
                                                C.kAudioSubTapUIDKey: tap_uuid.UUIDString()}],
        }
        from Foundation import NSDictionary
        k = lambda v: v.decode() if isinstance(v, bytes) else v  # pyobjc exposes these #define keys as bytes
        agg = {k(a): ([{k(x): y for x, y in e.items()} for e in b] if isinstance(b, list) else b)
               for a, b in agg.items()}
        d = NSDictionary.dictionaryWithDictionary_(agg)
        err = CA.AudioHardwareCreateAggregateDevice(objc.pyobjc_id(d), ctypes.byref(agg_id))
        res["create_aggregate"] = err
        if err:
            res["error"] = f"AudioHardwareCreateAggregateDevice failed: {err}"
            return res

        chunks: list[bytes] = []
        lock = threading.Lock()
        stats = {"callbacks": 0, "bytes": 0, "buffers": None}

        @IOPROC
        def ioproc(dev, now, inp, in_time, outp, out_time, client):
            try:
                abl = inp.contents
                n = abl.mNumberBuffers
                stats["callbacks"] += 1
                stats["buffers"] = [(abl.mBuffers[i].mNumberChannels, abl.mBuffers[i].mDataByteSize)
                                    for i in range(min(n, 8))]
                if n and abl.mBuffers[0].mData:
                    b = ctypes.string_at(abl.mBuffers[0].mData, abl.mBuffers[0].mDataByteSize)
                    with lock:
                        chunks.append(b)
                        stats["bytes"] += len(b)
            except Exception:
                pass
            return 0

        err = CA.AudioDeviceCreateIOProcID(agg_id.value, ioproc, None, ctypes.byref(proc_id))
        res["create_ioproc"] = err
        if err:
            res["error"] = f"AudioDeviceCreateIOProcID failed: {err}"
            return res
        err = CA.AudioDeviceStart(agg_id.value, proc_id)
        res["start"] = err
        if err:
            res["error"] = f"AudioDeviceStart failed: {err}"
            return res
        t0 = time.time()
        peaks = []
        last = 0
        while time.time() - t0 < seconds:
            time.sleep(1.0)
            with lock:
                new = chunks[last:]
                last = len(chunks)
            x = np.frombuffer(b"".join(new), dtype=np.float32) if new else np.zeros(0, np.float32)
            peaks.append(round(float(np.max(np.abs(x))) if len(x) else 0.0, 4))
        CA.AudioDeviceStop(agg_id.value, proc_id)
        res["stats"] = stats
        res["peaks_per_s"] = peaks
        audio = np.frombuffer(b"".join(chunks), dtype=np.float32)
        ch = max(1, fmt.mChannelsPerFrame)
        if len(audio) >= ch:
            audio = audio[: len(audio) // ch * ch].reshape(-1, ch).mean(axis=1)
        res["seconds_captured"] = round(len(audio) / (fmt.mSampleRate or 48000), 2)
        if transcribe and len(audio):
            from helper.dsp import resample
            from helper.transcribe import Transcriber
            mono = resample(audio, fmt.mSampleRate or 48000)
            segs = Transcriber("base.en").run(mono)
            res["transcript"] = [getattr(s, "text", str(s)) for s in segs]
        return res
    finally:
        if proc_id.value:
            CA.AudioDeviceDestroyIOProcID(agg_id.value, proc_id)
        if agg_id.value:
            CA.AudioHardwareDestroyAggregateDevice(agg_id.value)
        CA.AudioHardwareDestroyProcessTap(tap_id.value)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--match", action="append", default=[], help="substring of the process path (repeatable)")
    ap.add_argument("--seconds", type=float, default=20)
    ap.add_argument("--no-transcribe", action="store_true")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    try:
        result = run(a.match or ["avconferenced"], a.seconds, not a.no_transcribe)
    except Exception:
        result = {"error": traceback.format_exc()}
    with open(a.out, "w") as f:
        json.dump(result, f, indent=2, default=str)
