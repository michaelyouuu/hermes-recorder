"""S7 (throwaway): WER of the helper pipeline on concatenated LibriSpeech clips (hf-internal-testing/librispeech_asr_dummy).

argv: <repo> <workdir with ls/clips/*.flac + ls/refs.json>. Env: CFGS="name,model,win|min_s,hop|max_s,beam,ctx;...",
PAUSE=1 (PauseChunker instead of Windower), N, NOISE, NOVAD, NOKEEP, WHOLE=model, SHOW=1.
"""
import json, re, sys, time, glob, os
import numpy as np
sys.path.insert(0, sys.argv[1])
from faster_whisper import WhisperModel
import av
def decode_audio(f, sampling_rate=16000):
    out = []
    with av.open(f) as c:
        rs = av.AudioResampler(format="flt", layout="mono", rate=sampling_rate)
        for fr in c.decode(audio=0):
            for r in rs.resample(fr):
                out.append(r.to_ndarray().reshape(-1))
    return np.concatenate(out).astype(np.float32)
from helper import dsp
from helper.segments import Segment, keep
from hermes_recorder.buffer import RingBuffer, Entry

S = sys.argv[2]
refs = json.load(open(f"{S}/ls/refs.json"))
N = int(os.environ.get("N", "40"))
clips = [decode_audio(f, sampling_rate=16000) for f in sorted(glob.glob(f"{S}/ls/clips/*.flac"))[:N]]
gap = np.zeros(int(0.4 * 16000), np.float32)
audio = np.concatenate([np.concatenate([c, gap]) for c in clips]).astype(np.float32)
rng = np.random.default_rng(0)
noise = float(os.environ.get("NOISE", "0.0"))
if noise:
    audio = audio + rng.normal(0, noise, len(audio)).astype(np.float32)
ref = " ".join(refs[:N])
dur = len(audio) / 16000

def norm(t): return re.sub(r"[^a-z0-9' ]", " ", t.lower().replace("-", " ")).split()

def wer(r, h):
    r, h = norm(r), norm(h)
    d = list(range(len(h) + 1))
    for i in range(1, len(r) + 1):
        prev, d[0] = d[0], i
        for j in range(1, len(h) + 1):
            cur = d[j]
            d[j] = min(d[j] + 1, d[j - 1] + 1, prev + (r[i - 1] != h[j - 1]))
            prev = cur
    return d[len(h)] / len(r)

models = {}
def run(name, model, win, hop, beam, prompt):
    m = models.get(model) or models.setdefault(model, WhisperModel(model, device="cpu", compute_type="int8",
                                                                   cpu_threads=4, local_files_only=True))
    w = dsp.PauseChunker(16000, min_s=win, max_s=hop) if os.environ.get('PAUSE') else dsp.Windower(16000, win, hop)
    buf = RingBuffer(600, clock=lambda: 1e12)
    ctx, proc, t = "", 0.0, 1e12 - dur - 10
    first = True
    for end, x in w.push(audio):
        t0 = time.time()
        segs, _ = m.transcribe(x, language="en", beam_size=beam, vad_filter=not os.environ.get("NOVAD"), condition_on_previous_text=False,
                               without_timestamps=True, initial_prompt=(ctx[-200:] or None) if prompt else None)
        segs = [Segment(s.text.strip(), float(s.avg_logprob), float(s.no_speech_prob)) for s in segs]
        proc += time.time() - t0
        text = " ".join(s.text for s in segs if os.environ.get("NOKEEP") or keep(s, first)).strip(); first = False
        if text:
            e = buf.add(Entry("transcript", t + end, "app", "them", text))
            if e: ctx = (ctx + " " + e.text)[-400:]
    hyp = " ".join(e.text for e in buf.recent(1e9))
    print(f"{name:34s} WER {wer(ref, hyp):.3f}  RTF {proc / dur:.3f}  words {len(norm(hyp))}/{len(norm(ref))}", flush=True)
    if os.environ.get("SHOW"):
        print("REF:", " ".join(norm(ref))[:900]); print("HYP:", " ".join(norm(hyp))[:900])
    return hyp

print(f"{N} clips, {dur:.0f} s, noise {noise}")
for cfg in [l.split(",") for l in os.environ["CFGS"].split(";")]:
    name, model, win, hop, beam, prompt = cfg
    run(name, model, float(win), float(hop), int(beam), prompt == "1")

if os.environ.get("WHOLE"):
    for model in os.environ["WHOLE"].split(","):
        m = WhisperModel(model, device="cpu", compute_type="int8", cpu_threads=4, local_files_only=True)
        hyp = []
        for c in clips:
            segs, _ = m.transcribe(c, language="en", beam_size=5, vad_filter=True, without_timestamps=True)
            hyp.append(" ".join(s.text.strip() for s in segs))
        print(f"whole clips {model:22s} WER {wer(ref, ' '.join(hyp)):.3f}")
