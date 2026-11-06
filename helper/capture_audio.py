"""ScreenCaptureKit audio capture: the microphone (macOS 15+) plus apps that can't use a process tap.

Callbacks arrive on ScreenCaptureKit's queues; ``on_audio(kind, samples, rate)`` must be quick.
kind 1 = app audio ("them"), 2 = microphone ("me").
"""
from __future__ import annotations

import threading
import time
from typing import Callable

import numpy as np
import objc
from Foundation import NSDate, NSObject, NSRunLoop
import CoreMedia
import ScreenCaptureKit as SCK

APP_AUDIO, MIC = 1, 2
_FLAG_NON_INTERLEAVED = 1 << 5


def pump(seconds: float) -> None:
    end = time.time() + seconds
    rl = NSRunLoop.currentRunLoop()
    while time.time() < end:
        rl.runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.05))


def _await(start: Callable[[Callable], None], timeout: float = 10.0):
    box: dict = {}
    ev = threading.Event()

    def cb(*args):
        box["args"] = args
        ev.set()

    start(cb)
    end = time.time() + timeout
    while not ev.is_set() and time.time() < end:
        if threading.current_thread() is threading.main_thread():
            pump(0.05)
        else:
            ev.wait(0.05)
    if not ev.is_set():
        raise TimeoutError("ScreenCaptureKit did not respond")
    return box["args"]


def shareable_apps() -> dict[str, object]:
    """bundle id -> SCRunningApplication, plus the first display under key ''."""
    content, err = _await(lambda cb: SCK.SCShareableContent
                          .getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_(
                              False, False, cb))
    if err is not None:
        raise PermissionError(str(err.localizedDescription()))
    apps = {str(a.bundleIdentifier()): a for a in content.applications() if a.bundleIdentifier()}
    apps[""] = content.displays()[0]
    return apps


def _samples(sbuf) -> tuple[np.ndarray, float]:
    fmt = CoreMedia.CMSampleBufferGetFormatDescription(sbuf)
    asbd = CoreMedia.CMAudioFormatDescriptionGetStreamBasicDescription(fmt)
    a = asbd[0] if isinstance(asbd, tuple) else asbd
    blk = CoreMedia.CMSampleBufferGetDataBuffer(sbuf)
    if blk is None:
        return np.zeros(0, np.float32), float(a.mSampleRate)
    n = CoreMedia.CMBlockBufferGetDataLength(blk)
    res = CoreMedia.CMBlockBufferCopyDataBytes(blk, 0, n, None)
    data = res[1] if isinstance(res, tuple) else res
    x = np.frombuffer(bytes(data or b""), dtype=np.float32)
    ch = max(1, int(a.mChannelsPerFrame))
    if ch > 1:
        x = x[: len(x) // ch] if a.mFormatFlags & _FLAG_NON_INTERLEAVED else x[::ch]
    return x, float(a.mSampleRate)


SCStreamOutput = objc.protocolNamed("SCStreamOutput")


class _Output(NSObject, protocols=[SCStreamOutput]):
    def initWithHandler_(self, handler):
        self = objc.super(_Output, self).init()
        if self is None:
            return None
        self.handler = handler
        return self

    def stream_didOutputSampleBuffer_ofType_(self, stream, sbuf, otype):
        if otype not in (APP_AUDIO, MIC):
            return
        try:
            x, rate = _samples(sbuf)
            if len(x):
                self.handler(int(otype), x, rate)
        except Exception:  # never raise into ObjC
            pass


def mic_supported() -> bool:
    return SCK.SCStreamConfiguration.alloc().init().respondsToSelector_("setCaptureMicrophone:")


class AudioCapture:
    """SCK stream for the microphone plus any apps that can't use a process tap (Safari, macOS < 14.2).

    With no SCK apps it runs mic-only (``capturesAudio`` off, whole-display filter, 2x2 frames ignored).
    """

    def __init__(self, on_audio: Callable[[int, np.ndarray, float], None], mic: bool):
        self.on_audio = on_audio
        self.mic = mic and mic_supported()
        self.stream = None
        self._out = _Output.alloc().initWithHandler_(on_audio)
        self.capturing: list[str] = []
        self._app_audio = False

    def _filter(self, bundle_ids: list[str]):
        """(filter, captures_app_audio) or (None, False) when there is nothing to capture."""
        apps = shareable_apps()
        chosen = [apps[b] for b in bundle_ids if b in apps]
        self.capturing = [b for b in bundle_ids if b in apps]
        if chosen:
            return SCK.SCContentFilter.alloc().initWithDisplay_includingApplications_exceptingWindows_(
                apps[""], chosen, []), True
        if self.mic:
            return SCK.SCContentFilter.alloc().initWithDisplay_excludingWindows_(apps[""], []), False
        return None, False

    def start(self, bundle_ids: list[str]) -> list[str]:
        """Start capturing; returns the bundle ids that are running and captured (may be empty)."""
        self.stop()
        filt, app_audio = self._filter(bundle_ids)
        if filt is None:
            return []
        cfg = SCK.SCStreamConfiguration.alloc().init()
        cfg.setCapturesAudio_(app_audio)
        cfg.setExcludesCurrentProcessAudio_(True)
        cfg.setSampleRate_(16000)
        cfg.setChannelCount_(1)
        cfg.setWidth_(2)
        cfg.setHeight_(2)
        cfg.setMinimumFrameInterval_(CoreMedia.CMTimeMake(1, 1))
        if self.mic:
            cfg.setCaptureMicrophone_(True)
        stream = SCK.SCStream.alloc().initWithFilter_configuration_delegate_(filt, cfg, None)
        kinds = ([APP_AUDIO] if app_audio else []) + ([MIC] if self.mic else [])
        for t in kinds:
            ok, err = stream.addStreamOutput_type_sampleHandlerQueue_error_(self._out, t, None, None)
            if not ok:
                raise RuntimeError(f"addStreamOutput({t}) failed: {err}")
        # A (2x2, 1 fps) screen output is required or SCK logs dropped frames; the handler ignores it.
        stream.addStreamOutput_type_sampleHandlerQueue_error_(self._out, 0, None, None)
        (err,) = _await(stream.startCaptureWithCompletionHandler_)
        if err is not None:
            raise RuntimeError(f"startCapture failed: {err.localizedDescription()}")
        self.stream = stream
        self._app_audio = app_audio
        return list(self.capturing)

    def update_apps(self, bundle_ids: list[str]) -> list[str]:
        if self.stream is None:
            return self.start(bundle_ids)
        filt, app_audio = self._filter(bundle_ids)
        if filt is None:
            self.stop()
            return []
        if app_audio != self._app_audio:  # switching between app+mic and mic-only needs a new configuration
            return self.start(bundle_ids)
        (err,) = _await(lambda cb: self.stream.updateContentFilter_completionHandler_(filt, cb))
        if err is not None:
            raise RuntimeError(f"updateContentFilter failed: {err.localizedDescription()}")
        return list(self.capturing)

    def stop(self) -> None:
        s, self.stream = self.stream, None
        if s is not None:
            try:
                _await(s.stopCaptureWithCompletionHandler_, timeout=5)
            except Exception:
                pass
