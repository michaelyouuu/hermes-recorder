# M0 spike results

Machine: macOS 26.6.2 (25G83), Apple Silicon (arm64). Hermes v0.22.7 at `b200f5bc60e3`
(note: CLAUDE.md Section 4 was verified at `3093bc31`; re-check the plugin smoke tests in M1).
Python: Hermes's bundled 3.14.9 (`~/.hermes/tools/python-3.14.9+20261021-darwin-arm64`), venv via uv
with faster-whisper 1.2.1 + pyobjc 13.

## S1 Permissions

### (a) Launched from a terminal (VS Code integrated terminal, via Claude Code) — PASS
- TCC "responsible process" (`responsibility_get_pid_responsible_for_pid`) = **Visual Studio Code.app**,
  not python. All four permissions were already granted to VS Code: screen capture, microphone,
  accessibility, input monitoring.
- Proof capture (3 s): app audio via SCStream = 203,520 bytes = 3.2 s of **16 kHz mono float32**
  (format flags 41: float, packed, non-interleaved) — the requested format is honored.
- **Microphone via `captureMicrophone` ignores the requested sample rate: arrives 44.1 kHz mono float32.**
  The helper must resample mic audio to 16 kHz.
- Event tap (listen-only): 137 events in 3 s.

### Attribution observations (no new process needed)
- The currently running gateway (pid 41810, `hermes gateway restart` run from a VS Code terminal on Nov 3)
  is reparented to launchd but **TCC still attributes it and all its children to VS Code**. Attribution
  follows who launched the gateway, not who is connected to it.
- Hermes Desktop (`Hermes.app`, bundle `com.nousresearch.hermes`) is its own responsible process. Its
  Info.plist has `NSMicrophoneUsageDescription`, `NSAudioCaptureUsageDescription`,
  `NSScreenCaptureUsageDescription`; entitlement `device.audio-input`. So prompts naming "Hermes" can work.
- **Risk:** this Hermes.app build is **ad-hoc signed** (`Signature=adhoc`, no TeamIdentifier). TCC keys
  ad-hoc grants to the code hash, so grants may reset every time Desktop is rebuilt/updated. Check on
  a release-signed build and document in `hermes recorder doctor`.
- A gateway run as a launchd agent (`com.nousresearch.hermes.runtime.*`, currently not running) would be
  its own responsible process: a bare `python3` binary with no bundle/usage strings. Expect that path to
  be the hardest (Screen Recording would need a manual add in System Settings). TODO: verify.

### (b) Child of a gateway launched by Hermes Desktop — PENDING (needs user)

## S2 Audio + latency — PENDING (needs Chrome playing a lecture)
Models downloaded from Hugging Face on first load: tiny.en 149 MB, base.en 282 MB, small.en 927 MB
(cold load 4.1 s / 3.7 s / 9.9 s).

## S3 Events — PENDING (needs a demo task)
Script smoke-tested (tap created, processing loop under 0.1 ms when idle).
