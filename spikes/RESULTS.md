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

## S2 Audio + latency — PASS (base.en)
Source: Microsoft Edge (`com.microsoft.edgemac`) playing a YouTube lecture (Karpathy, "Deep Dive into
LLMs", from ~4:00), 60 s run (shortened from 3 min). Apple M4, 10 cores. 5 s windows, 4 s hop, VAD on,
int8, beam 1, 4 CPU threads.

Live with base.en (while capturing):
- Lag after each window closes: **p50 0.40 s, max 0.88 s** (the max was the first window, warm-up).
  Worst case from speech to text ≈ 5.9 s (window length + processing). Typical ≈ 2.5 s + 0.4 s.
- CPU: **34% of one core averaged** ≈ 3.4% of the whole 10-core machine. Peak RSS 679 MB (model + runtime).
- 0 silent windows: per-app filtering captured Edge audio correctly with nothing else playing.

Offline on the same audio (non-overlapping 5 s windows):

| Model | RTF | Est. live CPU (% of one core) | Proc p50 / max per window | WER vs small.en |
| --- | --- | --- | --- | --- |
| tiny.en | 0.031 | 16% | 0.14 / 0.29 s | 0.09 |
| base.en | 0.063 | 32% | 0.30 / 0.53 s | 0.03 |
| small.en | 0.192 | 96% | 0.95 / 1.14 s | (reference) |

Accuracy spot check against the video: base.en is near-verbatim. Errors were proper nouns ("FineWeb" heard as
"find web") and once "heuristics" heard as "he wrist sticks" in the live run.

Design implications:
- **Keep `base.en` as the default.** `small.en` is usable on an M4 but costs about a full core.
  mlx-whisper fallback not needed.
- **The 1 s overlap duplicates phrases** at window edges ("...want them in our / because we don't want them
  in our dataset"). The buffer must merge overlaps by finding the longest common word run at the boundary,
  or use VAD-based segmentation instead of fixed windows.
- **The first window hallucinates** a short filler word ("Hi.", "All right.", "to hide") when it starts
  mid-word. Drop very short first-window segments and segments with low avg_logprob or high no_speech_prob.
- **Memory:** helper RSS is ~0.7 GB because of the model. The 50 MB budget in Section 12 applies to the
  plugin's buffer, not the helper. State the helper's RAM use in the README.
- The per-app filter (`includingApplications`) works with a browser, so "capture Edge only" is real.

Models downloaded from Hugging Face on first load: tiny.en 149 MB, base.en 282 MB, small.en 927 MB
(cold load 4.1 s / 3.7 s / 9.9 s).

## S3 Events — IN PROGRESS

### Run 2: Notes (native app) — element naming PASS
- Clicks name controls well: `AXButton "New Note"`, `AXButton "Format"`, `AXCheckBox "Highlight"`,
  `AXButton "Checklist"`, plus the Dock item used to open Notes. The window title is included.
- One unlabeled hit: a Format-menu style row came back as `AXStaticText` with no title. Fixed by reading
  the static text's AXValue and climbing to an actionable ancestor.
- **Bug, fixed:** typed steps carried the *entire* note body (cumulative AXValue). That is wrong for skills
  and a privacy problem (typing one word in a long email would capture the whole email). Now multi-line
  fields emit only a delta (`inserted`, `deleted_chars`) against a snapshot taken before the first keystroke.
  Single-line fields keep their full (short, masked) value.
- **Bug, fixed:** Return split note typing into fragments. Return now ends a step only in single-line fields.

### Run 3: Edge (Chromium) — web naming PASS, secure field PASS (no leak)
- `AXManualAccessibility` makes Edge expose page elements: `AXButton "Log in"`, `AXTextField` placeholder
  "Email", the address bar (`"Address and search bar"`), and a search-result link's text.
- Password field correctly identified as `AXTextField/AXSecureTextField` (placeholder "Password").
  **No keystrokes from it reached the tap at all**: zero typed events while it was focused. Likely
  cause: macOS Secure Event Input withholds key events from event taps while a secure field is focused.
  (To confirm with Michael that he actually typed there.) Either way, the AXSecureTextField check stays as
  a second guard.
- Masking worked: the `gs_lcrp` token in a Google URL became `{TOKEN}`.
- **Bug, fixed:** the address bar's typed value was read after navigation, so it showed the resulting URL
  rather than the query typed. Now the last value is kept while focus is still on the field.
- **Bug, fixed:** the Return that submitted the search was attributed to the new focus (the web area) as
  an empty typed step. A Return/Tab within 1 s of a focus change now attaches to the previous step.
- Search-result click hit `AXStaticText` inside the link; now resolved to the `AXLink` ancestor.
- **Privacy note:** an email address typed into a login form is recorded verbatim. It isn't a secret, but it is
  PII. Decide in M3 whether to mask emails by default and let the compiler mark them as inputs.
- Clicks in other apps (Dock, Notification Center, VS Code) are captured too. The real helper enforces
  the per-app allowlist; system UI like the Dock should be kept as context ("opened Notes from the Dock").

### Performance
Idle loop p50 0.07 ms, but a single AX call can block up to the 0.25 s messaging timeout (one 261 ms
tick observed). **In the helper, run AX look-ups on a worker thread, not the run-loop thread that owns the tap.**
