"""macOS permission preflight and requests, evaluated for the process they run in (the helper app)."""
from __future__ import annotations

import threading
import time

SETTINGS = {
    "screen_capture": "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture",
    "microphone": "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone",
    "accessibility": "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
    "input_monitoring": "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent",
}


def _mic_status() -> str:
    import AVFoundation
    s = AVFoundation.AVCaptureDevice.authorizationStatusForMediaType_(AVFoundation.AVMediaTypeAudio)
    return {0: "not_determined", 1: "restricted", 2: "denied", 3: "authorized"}.get(s, str(s))


def status() -> dict:
    import HIServices
    import Quartz
    return {
        "screen_capture": bool(Quartz.CGPreflightScreenCaptureAccess()),
        "microphone": _mic_status(),
        "accessibility": bool(HIServices.AXIsProcessTrustedWithOptions(None)),
        "input_monitoring": bool(Quartz.CGPreflightListenEventAccess()),
    }


def request(include_events: bool, pump) -> None:
    """Trigger the prompts. ``pump(seconds)`` runs the run loop so completion handlers fire."""
    import AVFoundation
    import Quartz
    Quartz.CGRequestScreenCaptureAccess()
    if _mic_status() == "not_determined":
        done = threading.Event()
        AVFoundation.AVCaptureDevice.requestAccessForMediaType_completionHandler_(
            AVFoundation.AVMediaTypeAudio, lambda granted: done.set())
        end = time.time() + 60
        while not done.is_set() and time.time() < end:
            pump(0.2)
    if include_events:
        import HIServices
        HIServices.AXIsProcessTrustedWithOptions({HIServices.kAXTrustedCheckOptionPrompt: True})
        Quartz.CGRequestListenEventAccess()
