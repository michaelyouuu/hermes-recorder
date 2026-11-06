"""Test-only audio player run inside a generated .app, so ScreenCaptureKit can capture it as an app.

  player.py FILE [--volume 0.2] [--no-window]
"""
from __future__ import annotations

import argparse
import time

from AppKit import NSApplication, NSBackingStoreBuffered, NSMakeRect, NSWindow
from AVFoundation import AVAudioPlayer
from Foundation import NSDate, NSRunLoop, NSURL

PLAYER_BUNDLE_ID = "io.github.michaelyouuu.hermes-recorder.testplayer"

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--volume", type=float, default=0.2)
    ap.add_argument("--delay", type=float, default=1.5)
    ap.add_argument("--no-window", action="store_true", help="play without a window (like avconferenced)")
    a = ap.parse_args()
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(1)  # accessory: no Dock icon
    if not a.no_window:
        # ScreenCaptureKit only lists apps that own a window, so open a 1x1 off-screen one.
        win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(-200, -200, 1, 1), 0, NSBackingStoreBuffered, False)
        win.orderFrontRegardless()
    time.sleep(a.delay)  # give the recorder time to refilter after this app appears
    p, err = AVAudioPlayer.alloc().initWithContentsOfURL_error_(NSURL.fileURLWithPath_(a.file), None)
    if p is None:
        raise SystemExit(f"cannot open {a.file}: {err}")
    p.setVolume_(a.volume)
    p.play()
    end = time.time() + p.duration() + 0.5
    while time.time() < end:
        NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.1))
