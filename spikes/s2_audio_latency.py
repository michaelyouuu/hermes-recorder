"""S2: per-app audio capture + live transcription latency/CPU.

Phase 1 (live): capture the given app's audio for --seconds, transcribing 5 s windows with
~1 s overlap using --live-model on a worker thread, while measuring lag and process CPU.
Raw 16 kHz float32 audio is saved to spikes/out/s2_capture.f32 (gitignored).

Phase 2 (offline, --compare): replay the saved audio through tiny.en, base.en and small.en with
the same windowing, reporting real-time factor, CPU-seconds per audio-second, peak RSS, and a
rough WER of each model against small.en as a pseudo-reference.

Usage:
  python spikes/s2_audio_latency.py --app com.google.Chrome --seconds 180
  python spikes/s2_audio_latency.py --compare
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import re
import resource
import sys
import threading
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

RATE = 16000
WIN_S, HOP_S = 5.0, 4.0  # 5 s windows, 1 s overlap
RAW = os.path.join(common.OUT_DIR, "s2_capture.f32")


def load_model(name: str):
    from faster_whisper import WhisperModel
    return WhisperModel(name, device="cpu", compute_type="int8", cpu_threads=4)


def transcribe(model, audio: np.ndarray) -> str:
    segs, _ = model.transcribe(audio, language="en", beam_size=1, vad_filter=True,
                               condition_on_previous_text=False, without_timestamps=True)
    return " ".join(s.text.strip() for s in segs).strip()


def cpu_s() -> float:
    r = resource.getrusage(resource.RUSAGE_SELF)
    return r.ru_utime + r.ru_stime


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024)  # bytes on macOS


def live(app: str, seconds: float, model_name: str) -> dict:
    model = load_model(model_name)
    chunks: list[np.ndarray] = []
    lock = threading.Lock()
    total = {"n": 0}
    t0 = {"t": None}
    windows: "queue.Queue[tuple[float, np.ndarray] | None]" = queue.Queue()
    results = []

    def h(kind, data, info):
        if kind != 1 or not data:
            return
        a = np.frombuffer(data, dtype=np.float32)
        with lock:
            if t0["t"] is None:
                t0["t"] = time.time() - len(a) / RATE
            chunks.append(a)
            total["n"] += len(a)

    def worker():
        while True:
            item = windows.get()
            if item is None:
                return
            end_wall, audio = item
            start = time.time()
            text = transcribe(model, audio)
            done = time.time()
            results.append({"window_end_wall": end_wall, "proc_s": done - start,
                            "lag_s": done - end_wall, "rms": float(np.sqrt(np.mean(audio ** 2))),
                            "text": text})
            print(f"[{done - t0['t']:6.1f}s] lag={done - end_wall:4.2f}s proc={done - start:4.2f}s  {text}",
                  flush=True)

    th = threading.Thread(target=worker, daemon=True)
    th.start()
    cap = common.AudioCapture([app], h)
    print(f"capturing {cap.matched_apps} for {seconds:.0f}s with {model_name}...", flush=True)
    c0, w0 = cpu_s(), time.time()
    cap.start()
    next_end = WIN_S
    end_at = time.time() + seconds
    try:
        while time.time() < end_at:
            common.pump(0.2)
            with lock:
                n = total["n"]
            while n >= next_end * RATE:
                with lock:
                    buf = np.concatenate(chunks) if chunks else np.zeros(0, np.float32)
                s = int((next_end - WIN_S) * RATE)
                e = int(next_end * RATE)
                windows.put((t0["t"] + next_end, buf[s:e].copy()))
                next_end += HOP_S
    except KeyboardInterrupt:
        print("stopped early; saving what was captured", flush=True)
    cap.stop()
    windows.put(None)
    th.join(timeout=60)
    wall, cpu = time.time() - w0, cpu_s() - c0
    audio = np.concatenate(chunks) if chunks else np.zeros(0, np.float32)
    audio.tofile(RAW)
    lags = [r["lag_s"] for r in results]
    summary = {
        "mode": "live", "app": app, "model": model_name, "audio_s": len(audio) / RATE, "wall_s": wall,
        "cpu_pct_avg_one_core": 100 * cpu / wall, "peak_rss_mb": rss_mb(), "windows": len(results),
        "lag_p50_s": float(np.median(lags)) if lags else None,
        "lag_max_s": float(np.max(lags)) if lags else None,
        "worst_case_speech_end_to_text_s": (float(np.max(lags)) + WIN_S) if lags else None,
        "silent_windows": sum(1 for r in results if r["rms"] < 1e-4),
    }
    with open(os.path.join(common.OUT_DIR, f"s2_live_{model_name}.json"), "w") as f:
        json.dump({"summary": summary, "windows": results}, f, indent=2)
    return summary


def _words(t: str) -> list[str]:
    return re.sub(r"[^a-z0-9' ]", " ", t.lower()).split()


def wer(ref: list[str], hyp: list[str]) -> float:
    d = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        prev, d[0] = d[0], i
        for j, h in enumerate(hyp, 1):
            cur = min(d[j] + 1, d[j - 1] + 1, prev + (r != h))
            prev, d[j] = d[j], cur
    return d[-1] / max(1, len(ref))


def compare() -> list[dict]:
    audio = np.fromfile(RAW, dtype=np.float32)
    dur = len(audio) / RATE
    # non-overlapping 5 s chunks for the accuracy comparison (overlap would double-count words)
    out, texts = [], {}
    for name in ["tiny.en", "base.en", "small.en"]:
        model = load_model(name)
        transcribe(model, np.zeros(RATE, np.float32))  # warm up
        c0, w0, procs, parts = cpu_s(), time.time(), [], []
        n_win = int(dur // WIN_S)
        for i in range(n_win):
            seg = audio[int(i * WIN_S * RATE):int((i + 1) * WIN_S * RATE)]
            s = time.time()
            parts.append(transcribe(model, seg))
            procs.append(time.time() - s)
        wall, cpu = time.time() - w0, cpu_s() - c0
        texts[name] = " ".join(parts)
        out.append({"model": name, "audio_s": dur, "rtf": wall / dur, "cpu_s_per_audio_s": cpu / dur,
                    "est_cpu_pct_one_core_live": 100 * cpu / dur * (WIN_S / HOP_S),
                    "proc_p50_s": float(np.median(procs)), "proc_max_s": float(np.max(procs)),
                    "peak_rss_mb_cumulative": rss_mb(), "words": len(_words(texts[name]))})
        print(json.dumps(out[-1]), flush=True)
        del model
    ref = _words(texts["small.en"])
    for r in out:
        r["wer_vs_small"] = wer(ref, _words(texts[r["model"]]))
    with open(os.path.join(common.OUT_DIR, "s2_compare.json"), "w") as f:
        json.dump({"results": out, "texts": texts}, f, indent=2)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--app", default="com.google.Chrome")
    ap.add_argument("--seconds", type=float, default=180)
    ap.add_argument("--live-model", default="base.en")
    ap.add_argument("--compare", action="store_true")
    a = ap.parse_args()
    if a.compare:
        res = compare()
        print(json.dumps(res, indent=2))
    else:
        print(json.dumps(live(a.app, a.seconds, a.live_model), indent=2))


if __name__ == "__main__":
    main()
