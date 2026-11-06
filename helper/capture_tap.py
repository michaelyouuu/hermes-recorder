"""Per-app audio via CoreAudio process taps (macOS 14.2+). Production version of spikes/s5_process_tap.py.

ScreenCaptureKit can only include apps that own a window and misses audio played by helper or system processes
(FaceTime and iPhone calls play through avconferenced; Zoom uses caphost). A process tap captures exactly the
processes we name, while the user keeps hearing them (unmuted, private tap). One tap per chosen app, so every
transcript line is attributed to the right app. Uses the same Screen & System Audio Recording grant as SCK.

``on_audio(1, mono_float32, rate, app=bundle_id)`` is called from one drain thread, never the CoreAudio IO thread.
"""
from __future__ import annotations

import collections
import ctypes
import logging
import os
import platform
import threading
from typing import Callable

import objc
from Foundation import NSDictionary, NSUUID

from helper import dsp
from helper.proc_match import Proc, resolve, route_daemons

log = logging.getLogger("hermes_recorder_helper")

APP_AUDIO = 1
CA = ctypes.CDLL("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
LIBPROC = ctypes.CDLL("/usr/lib/libproc.dylib")


def _fourcc(s: str) -> int:
    return int.from_bytes(s.encode(), "big")


SYSTEM, GLOBAL, MAIN = 1, _fourcc("glob"), 0
PROCESS_LIST, P_PID, P_BUNDLE = _fourcc("prs#"), _fourcc("ppid"), _fourcc("pbid")
TAP_FORMAT, DEFAULT_OUT, DEVICE_UID = _fourcc("tfmt"), _fourcc("dOut"), _fourcc("uid ")
CHUNKS_MAX = 600  # ~6 s of 10 ms IO buffers per tap; drop oldest beyond that


class _Addr(ctypes.Structure):
    _fields_ = [("mSelector", ctypes.c_uint32), ("mScope", ctypes.c_uint32), ("mElement", ctypes.c_uint32)]


class _ASBD(ctypes.Structure):
    _fields_ = [("mSampleRate", ctypes.c_double), ("mFormatID", ctypes.c_uint32), ("mFormatFlags", ctypes.c_uint32),
                ("mBytesPerPacket", ctypes.c_uint32), ("mFramesPerPacket", ctypes.c_uint32),
                ("mBytesPerFrame", ctypes.c_uint32), ("mChannelsPerFrame", ctypes.c_uint32),
                ("mBitsPerChannel", ctypes.c_uint32), ("mReserved", ctypes.c_uint32)]


class _AudioBuffer(ctypes.Structure):
    _fields_ = [("mNumberChannels", ctypes.c_uint32), ("mDataByteSize", ctypes.c_uint32), ("mData", ctypes.c_void_p)]


class _AudioBufferList(ctypes.Structure):
    _fields_ = [("mNumberBuffers", ctypes.c_uint32), ("mBuffers", _AudioBuffer * 8)]


_IOPROC = ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(_AudioBufferList),
                           ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)

CA.AudioObjectGetPropertyDataSize.argtypes = [ctypes.c_uint32, ctypes.POINTER(_Addr), ctypes.c_uint32,
                                              ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
CA.AudioObjectGetPropertyData.argtypes = [ctypes.c_uint32, ctypes.POINTER(_Addr), ctypes.c_uint32, ctypes.c_void_p,
                                          ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
_LISTENER = ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(_Addr),
                             ctypes.c_void_p)
CA.AudioObjectAddPropertyListener.argtypes = [ctypes.c_uint32, ctypes.POINTER(_Addr), _LISTENER, ctypes.c_void_p]
CA.AudioObjectRemovePropertyListener.argtypes = [ctypes.c_uint32, ctypes.POINTER(_Addr), _LISTENER, ctypes.c_void_p]
CA.AudioHardwareCreateAggregateDevice.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
CA.AudioHardwareDestroyAggregateDevice.argtypes = [ctypes.c_uint32]
CA.AudioDeviceCreateIOProcID.argtypes = [ctypes.c_uint32, _IOPROC, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
CA.AudioDeviceDestroyIOProcID.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
CA.AudioDeviceStart.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
CA.AudioDeviceStop.argtypes = [ctypes.c_uint32, ctypes.c_void_p]


def taps_supported() -> bool:
    try:
        major, minor = (int(x) for x in (platform.mac_ver()[0].split(".") + ["0"])[:2])
    except ValueError:
        return False
    if (major, minor) < (14, 2):
        return False
    try:
        fn = CA.AudioHardwareCreateProcessTap
    except AttributeError:
        return False
    fn.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    CA.AudioHardwareDestroyProcessTap.argtypes = [ctypes.c_uint32]
    return True


def _get(obj: int, sel: int, ctype):
    v = ctype()
    size = ctypes.c_uint32(ctypes.sizeof(v))
    err = CA.AudioObjectGetPropertyData(obj, ctypes.byref(_Addr(sel, GLOBAL, MAIN)), 0, None, ctypes.byref(size),
                                        ctypes.byref(v))
    return err, v


def _get_str(obj: int, sel: int) -> str | None:
    err, v = _get(obj, sel, ctypes.c_void_p)
    if err or not v.value:
        return None
    return str(objc.objc_object(c_void_p=v.value))


def _pid_path(pid: int) -> str:
    buf = ctypes.create_string_buffer(4096)
    n = LIBPROC.proc_pidpath(pid, buf, 4096)
    return buf.value[:n].decode(errors="replace") if n > 0 else ""


class ProcessList:
    """CoreAudio process objects, cached by object id (they're stable for a process's lifetime)."""

    def __init__(self) -> None:
        self._cache: dict[int, Proc] = {}

    def snapshot(self) -> list[Proc]:
        a = _Addr(PROCESS_LIST, GLOBAL, MAIN)
        size = ctypes.c_uint32(0)
        if CA.AudioObjectGetPropertyDataSize(SYSTEM, ctypes.byref(a), 0, None, ctypes.byref(size)):
            return list(self._cache.values())
        arr = (ctypes.c_uint32 * (size.value // 4))()
        if CA.AudioObjectGetPropertyData(SYSTEM, ctypes.byref(a), 0, None, ctypes.byref(size), arr):
            return list(self._cache.values())
        objs = set(arr)
        self._cache = {o: p for o, p in self._cache.items() if o in objs}
        for o in objs - set(self._cache):
            err, pid = _get(o, P_PID, ctypes.c_int32)
            if err:
                continue
            self._cache[o] = Proc(o, pid.value, _get_str(o, P_BUNDLE) or "", _pid_path(pid.value))
        return list(self._cache.values())


def _bundle_path(bid: str) -> str | None:
    try:
        from AppKit import NSWorkspace
        url = NSWorkspace.sharedWorkspace().URLForApplicationWithBundleIdentifier_(bid)
        return str(url.path()) if url is not None else None
    except Exception:
        return None


def _app_running(bid: str) -> bool:
    try:
        from AppKit import NSRunningApplication
        return len(NSRunningApplication.runningApplicationsWithBundleIdentifier_(bid)) > 0
    except Exception:
        return False


def _plist_keys(d):
    """pyobjc exposes the kAudioAggregateDevice*/kAudioSubTap* keys as bytes; CoreAudio segfaults on non-str keys."""
    k = lambda v: v.decode() if isinstance(v, bytes) else v  # noqa: E731
    if isinstance(d, dict):
        return {k(a): _plist_keys(b) for a, b in d.items()}
    if isinstance(d, list):
        return [_plist_keys(x) for x in d]
    return d


class ProcessTap:
    """One private, unmuted tap over some processes, read through a private aggregate device."""

    def __init__(self, objs: list[int]):
        import CoreAudio as C
        self.chunks: collections.deque[bytes] = collections.deque(maxlen=CHUNKS_MAX)
        self.dropped = 0
        self.tap_id = ctypes.c_uint32(0)
        self.agg_id = ctypes.c_uint32(0)
        self.proc_id = ctypes.c_void_p()
        desc = C.CATapDescription.alloc().initStereoMixdownOfProcesses_(list(objs))
        uuid = NSUUID.UUID()
        desc.setUUID_(uuid)
        desc.setPrivate_(True)
        desc.setMuteBehavior_(0)  # CATapUnmuted: the user still hears it
        self._desc = desc
        err = CA.AudioHardwareCreateProcessTap(objc.pyobjc_id(desc), ctypes.byref(self.tap_id))
        if err:
            raise OSError(f"AudioHardwareCreateProcessTap failed ({err})")
        try:
            err, fmt = _get(self.tap_id.value, TAP_FORMAT, _ASBD)
            if err:
                raise OSError(f"tap format unavailable ({err})")
            self.rate = float(fmt.mSampleRate) or 48000.0
            self.channels = max(1, int(fmt.mChannelsPerFrame))
            _, dev = _get(SYSTEM, DEFAULT_OUT, ctypes.c_uint32)
            out_uid = _get_str(dev.value, DEVICE_UID)
            agg = {
                C.kAudioAggregateDeviceNameKey: "Hermes Recorder Tap",
                C.kAudioAggregateDeviceUIDKey: NSUUID.UUID().UUIDString(),
                C.kAudioAggregateDeviceIsPrivateKey: True,
                C.kAudioAggregateDeviceIsStackedKey: False,
                C.kAudioAggregateDeviceTapAutoStartKey: True,
                C.kAudioAggregateDeviceTapListKey: [{C.kAudioSubTapDriftCompensationKey: True,
                                                     C.kAudioSubTapUIDKey: uuid.UUIDString()}],
            }
            if out_uid:
                agg[C.kAudioAggregateDeviceMainSubDeviceKey] = out_uid
                agg[C.kAudioAggregateDeviceSubDeviceListKey] = [{C.kAudioSubDeviceUIDKey: out_uid}]
            d = NSDictionary.dictionaryWithDictionary_(_plist_keys(agg))
            err = CA.AudioHardwareCreateAggregateDevice(objc.pyobjc_id(d), ctypes.byref(self.agg_id))
            if err:
                raise OSError(f"AudioHardwareCreateAggregateDevice failed ({err})")
            self._cb = _IOPROC(self._io)  # keep a reference: CoreAudio calls it on its IO thread
            err = CA.AudioDeviceCreateIOProcID(self.agg_id.value, self._cb, None, ctypes.byref(self.proc_id))
            if err:
                raise OSError(f"AudioDeviceCreateIOProcID failed ({err})")
            err = CA.AudioDeviceStart(self.agg_id.value, self.proc_id)
            if err:
                raise OSError(f"AudioDeviceStart failed ({err})")
        except Exception:
            self.close()
            raise

    def _io(self, dev, now, inp, in_time, outp, out_time, client):
        try:
            abl = inp.contents
            if abl.mNumberBuffers and abl.mBuffers[0].mData:
                b = abl.mBuffers[0]
                if len(self.chunks) == CHUNKS_MAX:
                    self.dropped += 1
                self.chunks.append(ctypes.string_at(b.mData, b.mDataByteSize))
                self.buf_channels = int(b.mNumberChannels) or self.channels
        except Exception:
            pass
        return 0

    def drain(self):
        """Mono float32 samples captured since the last call (may be empty)."""
        parts = []
        while True:
            try:
                parts.append(self.chunks.popleft())
            except IndexError:
                break
        if not parts:
            return None
        return dsp.downmix(b"".join(parts), getattr(self, "buf_channels", self.channels))

    def close(self) -> None:
        if self.proc_id.value:
            CA.AudioDeviceStop(self.agg_id.value, self.proc_id)
            CA.AudioDeviceDestroyIOProcID(self.agg_id.value, self.proc_id)
            self.proc_id = ctypes.c_void_p()
        if self.agg_id.value:
            CA.AudioHardwareDestroyAggregateDevice(self.agg_id.value)
            self.agg_id = ctypes.c_uint32(0)
        if self.tap_id.value:
            CA.AudioHardwareDestroyProcessTap(self.tap_id.value)
            self.tap_id = ctypes.c_uint32(0)


class TapCapture:
    """Keeps one ProcessTap per chosen app in sync with the processes that carry its audio."""

    def __init__(self, on_audio: Callable[..., None]):
        self.on_audio = on_audio
        self.apps: list[str] = []
        self.capturing: list[str] = []
        self.taps: dict[str, ProcessTap] = {}
        self._objs: dict[str, frozenset[int]] = {}
        self._procs = ProcessList()
        self._paths: dict[str, str | None] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Set by CoreAudio when processes start or stop using audio, so a new tap is built right away
        # (polling alone would lose the first ~2 s of an app that just started playing).
        self.changed = threading.Event()
        self._listener = _LISTENER(self._on_process_list)
        self._listening = False

    def _on_process_list(self, obj, n, addrs, client):
        self.changed.set()
        return 0

    def _listen(self, on: bool) -> None:
        if on == self._listening:
            return
        a = _Addr(PROCESS_LIST, GLOBAL, MAIN)
        fn = CA.AudioObjectAddPropertyListener if on else CA.AudioObjectRemovePropertyListener
        err = fn(SYSTEM, ctypes.byref(a), self._listener, None)
        if err:
            log.warning("process list listener %s failed: %s", "add" if on else "remove", err)
        else:
            self._listening = on

    def start(self, bundle_ids: list[str]) -> list[str]:
        self.stop()
        self._stop.clear()
        self._thread = threading.Thread(target=self._drain_loop, name="tap-drain", daemon=True)
        self._thread.start()
        self._listen(True)
        return self.update_apps(bundle_ids)

    def update_apps(self, bundle_ids: list[str]) -> list[str]:
        self.apps = list(bundle_ids)
        return self.refresh()

    def refresh(self) -> list[str]:
        """Re-resolve processes; (re)build only the taps whose process set changed. Returns apps captured."""
        for b in self.apps:
            if b not in self._paths:
                self._paths[b] = _bundle_path(b)
        procs = self._procs.snapshot()
        want = resolve(self.apps, procs, self._paths, os.getpid())
        with self._lock:
            for bid in [b for b in self.taps if b not in want]:
                self.taps.pop(bid).close()
                self._objs.pop(bid, None)
            for bid in [b for b in self._objs if b not in want]:
                self._objs.pop(bid, None)
            for bid, ps in want.items():
                objs = frozenset(p.obj for p in ps)
                if objs == self._objs.get(bid):
                    continue
                old = self.taps.pop(bid, None)
                if old is not None:
                    old.close()
                self._objs[bid] = objs
                if objs:
                    try:
                        self.taps[bid] = ProcessTap(sorted(objs))
                        log.info("tap %s -> pids %s", bid, sorted(p.pid for p in ps))
                    except Exception as e:
                        log.warning("tap for %s failed: %s", bid, e)
        present = {p.bundle_id for p in procs}
        self.capturing = [b for b in want
                          if b in self.taps or _app_running(b) or any(d in present for d in route_daemons(b))]
        return list(self.capturing)

    def _drain_loop(self) -> None:
        while not self._stop.wait(0.1):
            with self._lock:
                items = list(self.taps.items())
            for bid, tap in items:
                x = tap.drain()
                if x is not None and len(x):
                    try:
                        self.on_audio(APP_AUDIO, x, tap.rate, app=bid)
                    except Exception as e:
                        log.warning("tap audio handler failed: %s", e)

    def stop(self) -> None:
        self._listen(False)
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        with self._lock:
            for t in self.taps.values():
                t.close()
            self.taps.clear()
            self._objs.clear()
        self.capturing = []
