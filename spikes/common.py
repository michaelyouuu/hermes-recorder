"""Shared throwaway helpers for M0 spikes (not plugin code)."""
from __future__ import annotations

import ctypes
import ctypes.util
import os
import threading
import time
from typing import Callable

import objc
from Foundation import NSObject, NSRunLoop, NSDate
import Quartz
import HIServices
import AVFoundation
import CoreMedia
import ScreenCaptureKit as SCK

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
os.makedirs(OUT_DIR, exist_ok=True)


# ---------------------------------------------------------------- process attribution
_libc = ctypes.CDLL(ctypes.util.find_library("c"))
_libproc = ctypes.CDLL("/usr/lib/libproc.dylib")


def _pid_path(pid: int) -> str:
    buf = ctypes.create_string_buffer(4096)
    n = _libproc.proc_pidpath(pid, buf, 4096)
    return buf.value[:n].decode() if n > 0 else "?"


def responsible_process() -> dict:
    """Which process TCC attributes our permissions to (private but stable libsystem API)."""
    try:
        fn = _libc.responsibility_get_pid_responsible_for_pid
        fn.argtypes = [ctypes.c_int]
        fn.restype = ctypes.c_int
        rpid = fn(os.getpid())
    except AttributeError:
        rpid = -1
    return {"self_pid": os.getpid(), "self_path": _pid_path(os.getpid()),
            "responsible_pid": rpid, "responsible_path": _pid_path(rpid) if rpid > 0 else "?"}


def pump(seconds: float) -> None:
    """Run the main run loop so callbacks/event taps fire."""
    end = time.time() + seconds
    rl = NSRunLoop.currentRunLoop()
    while time.time() < end:
        rl.runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.1))


# ---------------------------------------------------------------- permissions
def mic_status() -> str:
    s = AVFoundation.AVCaptureDevice.authorizationStatusForMediaType_(AVFoundation.AVMediaTypeAudio)
    return {0: "not_determined", 1: "restricted", 2: "denied", 3: "authorized"}.get(s, str(s))


def permission_status() -> dict:
    return {
        "screen_capture": bool(Quartz.CGPreflightScreenCaptureAccess()),
        "microphone": mic_status(),
        "accessibility": bool(HIServices.AXIsProcessTrustedWithOptions(None)),
        "input_monitoring": bool(Quartz.CGPreflightListenEventAccess()),
    }


def request_all() -> None:
    Quartz.CGRequestScreenCaptureAccess()
    if mic_status() == "not_determined":
        done = threading.Event()
        AVFoundation.AVCaptureDevice.requestAccessForMediaType_completionHandler_(
            AVFoundation.AVMediaTypeAudio, lambda granted: done.set())
        t = time.time()
        while not done.is_set() and time.time() - t < 60:
            pump(0.2)
    HIServices.AXIsProcessTrustedWithOptions({HIServices.kAXTrustedCheckOptionPrompt: True})
    Quartz.CGRequestListenEventAccess()


# ---------------------------------------------------------------- ScreenCaptureKit audio
def shareable_content(timeout: float = 10.0):
    box: dict = {}
    ev = threading.Event()

    def cb(content, err):
        box["c"], box["e"] = content, err
        ev.set()

    SCK.SCShareableContent.getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_(
        False, False, cb)
    t = time.time()
    while not ev.is_set() and time.time() - t < timeout:
        pump(0.1)
    if not ev.is_set():
        raise TimeoutError("getShareableContent timed out")
    if box["e"] is not None:
        raise RuntimeError(f"getShareableContent error: {box['e']}")
    return box["c"]


def _sample_to_bytes(sbuf) -> tuple[bytes, dict]:
    fmt = CoreMedia.CMSampleBufferGetFormatDescription(sbuf)
    asbd = CoreMedia.CMAudioFormatDescriptionGetStreamBasicDescription(fmt)
    info = {}
    if asbd is not None:
        a = asbd[0] if isinstance(asbd, tuple) else asbd
        try:
            info = {"rate": a.mSampleRate, "ch": a.mChannelsPerFrame, "bits": a.mBitsPerChannel,
                    "flags": a.mFormatFlags, "bpf": a.mBytesPerFrame}
        except Exception:
            info = {"asbd": repr(a)}
    blk = CoreMedia.CMSampleBufferGetDataBuffer(sbuf)
    if blk is None:
        return b"", info
    n = CoreMedia.CMBlockBufferGetDataLength(blk)
    res = CoreMedia.CMBlockBufferCopyDataBytes(blk, 0, n, None)
    data = res[1] if isinstance(res, tuple) else res
    return bytes(data or b""), info


SCStreamOutput = objc.protocolNamed("SCStreamOutput")


class _Output(NSObject, protocols=[SCStreamOutput]):
    def initWithHandler_(self, handler):
        self = objc.super(_Output, self).init()
        if self is None:
            return None
        self.handler = handler
        return self

    def stream_didOutputSampleBuffer_ofType_(self, stream, sbuf, otype):
        if otype == 0:  # screen frames: ignore
            return
        try:
            data, info = _sample_to_bytes(sbuf)
            self.handler(int(otype), data, info)
        except Exception as e:  # never raise into ObjC
            self.handler(-1, b"", {"error": repr(e)})


class AudioCapture:
    """App-filtered system audio (+ optional mic) via SCStream.

    handler(kind, pcm_bytes, fmt) with kind 1 = app audio, 2 = microphone.
    """

    def __init__(self, bundle_ids: list[str] | None, handler: Callable, mic: bool = False,
                 rate: int = 16000):
        content = shareable_content()
        display = content.displays()[0]
        apps = [a for a in content.applications() if bundle_ids is None or a.bundleIdentifier() in bundle_ids]
        self.matched_apps = [a.bundleIdentifier() for a in apps]
        if bundle_ids is not None and not apps:
            raise RuntimeError(f"none of {bundle_ids} running/shareable")
        if bundle_ids is None:
            filt = SCK.SCContentFilter.alloc().initWithDisplay_excludingWindows_(display, [])
        else:
            filt = SCK.SCContentFilter.alloc().initWithDisplay_includingApplications_exceptingWindows_(
                display, apps, [])
        cfg = SCK.SCStreamConfiguration.alloc().init()
        cfg.setCapturesAudio_(True)
        cfg.setExcludesCurrentProcessAudio_(True)
        cfg.setSampleRate_(rate)
        cfg.setChannelCount_(1)
        cfg.setWidth_(2)
        cfg.setHeight_(2)
        cfg.setMinimumFrameInterval_(CoreMedia.CMTimeMake(1, 1))
        if mic:
            cfg.setCaptureMicrophone_(True)
        self.stream = SCK.SCStream.alloc().initWithFilter_configuration_delegate_(filt, cfg, None)
        self.out = _Output.alloc().initWithHandler_(handler)
        for t in ([1, 2] if mic else [1]):
            ok, err = self.stream.addStreamOutput_type_sampleHandlerQueue_error_(self.out, t, None, None)
            if not ok:
                raise RuntimeError(f"addStreamOutput type {t} failed: {err}")
        # SCK requires a screen output too or it may complain; add one that we ignore
        self.stream.addStreamOutput_type_sampleHandlerQueue_error_(self.out, 0, None, None)

    def _call(self, sel, timeout=10.0):
        ev = threading.Event()
        box = {}

        def cb(err):
            box["e"] = err
            ev.set()

        sel(cb)
        t = time.time()
        while not ev.is_set() and time.time() - t < timeout:
            pump(0.1)
        if box.get("e") is not None:
            raise RuntimeError(str(box["e"]))

    def start(self):
        self._call(self.stream.startCaptureWithCompletionHandler_)

    def stop(self):
        self._call(self.stream.stopCaptureWithCompletionHandler_)
