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
  **No keystrokes from it reached the tap at all**, even though Michael confirmed he typed a password.
  macOS Secure Event Input withholds key events from every event tap while a secure field is focused,
  so password characters are unreachable at the OS level. The AXSecureTextField check stays as a second
  guard (for apps that draw their own password fields), and the step is recorded as "entered secret into
  field X" from the click on the secure field.
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

### Compiler check: Notes slice -> SKILL.md draft — PASS with gaps
`spikes/s3_normalize.py` turns 22 raw events into 12 steps (Dock launch, New Note, typing deltas, Format,
Highlight, Checklist). Drafting from that slice (done by Claude in-session, standing in for `ctx.llm`) gave
`spikes/s3_draft_SKILL.md`: a recognisable, replayable skill with 3 inputs and 3 edge-case questions.
Gaps that need fixing in M3:
- **Text selection isn't captured.** We only see mouse-down, so "select the line, then Highlight" had to
  be guessed. Capture drags/shift-selection, or read `AXSelectedText` (a range, not the content, for
  privacy) when a format control is clicked.
- **Unlabeled menu rows** (`_NS:38`) still occur. The static-text fix should label them; otherwise ask about them as an
  edge-case question, which the draft already does.
- **Intent is guessed without narration.** With nonsense test text the draft can't tell what the note is
  for. Narration (mic, already working in S1/S2) and the "why" questions carry the intent.
- Clicks that only refocus the field (steps 4, 8, 12) are noise; drop them in normalization.

### Run 4: Edge re-check (WSJ sign-in), 60 s
- Ancestor climb works: a click on link text now records `AXLink "Sign In"`.
- Single-line field value is right: `AXTextField "Email or username"` -> "michaelmi" (no navigation side effects).
- **New gap: div-heavy sites give unlabeled `AXGroup` hits** (4 of 9 Edge clicks on WSJ, incl. the
  sign-in form's continue control). Fallbacks for M3: read `AXDOMIdentifier` / `AXDOMClassList`, the
  nearest labeled descendant, or text near the point. Snapshot on click stays optional. Keep demo sites known-good.
- Not covered by this run: the address-bar search fix (no query typed) and a secure field (the sign-in
  flow didn't reach the password step). The password result from Run 3 stands.

## S1(b) Child of the real gateway — FAIL as designed; FIXED by a helper .app (S1c)

### Setup
Stopped the VS Code-launched gateway. Hermes relaunched it automatically as a one-off launchd job via
`/usr/bin/osascript` (`hermes_cli/gateway_launchd_runtime.py`). Desktop chats ran their tools **in that
gateway** (probe's parent pid = gateway pid) and no `hermes serve` was running, so Telegram/other
messaging behave the same. A throwaway plugin `~/.hermes/plugins/s1-probe` ran the S1 script.

### Result with the helper as a plain child: unusable
- TCC responsible process = **`/usr/bin/osascript`**. The mic prompt named "osascript" and was allowed.
- Screen & System Audio was not granted -> SCK error `-3801 user declined TCCs`. Accessibility and
  Input Monitoring were not granted either.
- Even if granted, giving osascript Screen Recording, Accessibility and Input Monitoring would hand them to
  every AppleScript run by any program. **Not acceptable to ship.**
- Caution: running `hermes gateway restart` from a terminal ran the gateway in the foreground of that
  terminal (attributed to VS Code). `hermes gateway stop` then let Hermes relaunch it via osascript.

## S1(c) Helper as its own generated .app — PASS
`spikes/s1c_build_helper_app.sh` builds `Hermes Recorder Helper.app` locally: Info.plist with bundle id
`io.github.michaelyouuu.hermes-recorder.helper`, `LSUIElement`, mic/audio/screen usage strings,
`LSEnvironment PYTHONHOME`; the executable is a **copy of Hermes's own python3.14** (python-build-standalone,
only system dylibs); ad-hoc signed with `codesign -s -`. Launched with
`open -n -W --env PYTHONPATH=<venv site-packages> -a <app> --args <script>`. Nothing compiled is committed.

| Run (launched from gateway via probe) | Responsible | Screen | Mic | App audio + mic capture |
| --- | --- | --- | --- | --- |
| 1, with prompts | helper itself | no | granted ("Hermes Recorder Helper") | fail (-3801) |
| 2, after enabling in System Settings | helper itself | yes | yes | **PASS**: 3.2 s app audio + mic |
| 3, fresh launch from terminal, no prompts | helper itself | yes | yes | **PASS**: grants persist |

- Launching through LaunchServices makes the helper responsible for itself **no matter who launches it**
  (osascript gateway, VS Code, Desktop). Attribution no longer depends on how the user started Hermes.
- The fourth run (from the gateway, `request false`) wrote no result, and the probe returned the previous
  line (a probe bug). Cause unknown. `open --stdout` reproducibly fails with LaunchServices `-10810`.
  **The helper manager must confirm the helper's `hello` over the socket, and retry or report a clear error.**
- Accessibility and Input Monitoring don't prompt usefully: they must be switched on in System Settings
  (the helper had to be added with "+" / found in the list). After that, a fresh launch (run 4, `final-check`)
  had **all four granted**: app audio 3.1 s + mic captured, event tap saw 301 events. The doctor should open
  `x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility` and `?Privacy_ListenEvent`.
- Risk: ad-hoc grants are tied to the code hash. Rebuilding the .app with the same python binary keeps the
  hash; a Hermes python update changes it, so the user re-grants. `hermes recorder doctor` must detect this.
- Process model changes: no stdin/stdout pipes through `open`, so the Unix socket is the only channel
  (already the design). Parent-death detection must use socket close, not stdin. `open -W` blocks, so
  the manager should launch without `-W` and track the pid from the `hello` message.
- Catalog rule 17 (no compiled binaries in the repo) is respected: the bundle is generated at
  `hermes recorder setup` from the user's existing interpreter. Disclose it in README/PR.

## M0 verdict: all three spikes PASS (S1 needed a design change)

Design changes for M1+ (need Michael's OK before CLAUDE.md is updated):
1. **The helper runs as a locally generated, ad-hoc-signed `Hermes Recorder Helper.app`**, launched through
   LaunchServices (`open -n`), not as a plain `subprocess.Popen` child. It's built by `hermes recorder setup`
   from Hermes's own python; nothing compiled is committed. Permissions are granted to "Hermes Recorder Helper"
   once and persist. Disclose this in README/PR.
2. The socket is the only channel; the helper exits when the socket closes; the manager tracks the pid
   from `hello`, verifies startup, and retries.
3. Mic audio is resampled from 44.1 kHz to 16 kHz in the helper.
4. Transcription: `base.en` default; merge window overlaps; drop first-window/low-confidence fragments.
5. Events: AX look-ups off the tap thread; typed deltas, not whole field values; secure fields are
   unreachable anyway (Secure Event Input); add DOM-id/nearby-text fallbacks for unlabeled AXGroup; capture
   selection ranges; decide on email masking.

Cleanup done: throwaway `s1-probe` plugin disabled and deleted. The gateway is running via osascript as Hermes
normally runs it. Left for Michael: osascript's Microphone grant from S1(b) can be revoked in System Settings ->
Privacy & Security -> Microphone.
