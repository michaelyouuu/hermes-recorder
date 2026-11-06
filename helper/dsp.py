"""Pure-numpy audio helpers (no macOS imports, testable anywhere)."""
from __future__ import annotations

import numpy as np

TARGET_RATE = 16000


def _lowpass_taps(cutoff: float, n: int = 101) -> np.ndarray:
    """Windowed-sinc low-pass FIR; ``cutoff`` is a fraction of the sample rate (0..0.5)."""
    m = np.arange(n) - (n - 1) / 2
    h = 2 * cutoff * np.sinc(2 * cutoff * m) * np.hamming(n)
    return (h / h.sum()).astype(np.float32)


def resample(x: np.ndarray, src_rate: float, dst_rate: float = TARGET_RATE) -> np.ndarray:
    """Resample a mono float32 block. Low-passes before downsampling so speech doesn't alias."""
    x = np.asarray(x, dtype=np.float32)
    if len(x) == 0 or abs(src_rate - dst_rate) < 1:
        return x.copy()
    if dst_rate < src_rate:
        x = np.convolve(x, _lowpass_taps(0.45 * dst_rate / src_rate), mode="same").astype(np.float32)
    n_out = int(round(len(x) * dst_rate / src_rate))
    t_src = np.arange(len(x)) / src_rate
    t_dst = np.arange(n_out) / dst_rate
    return np.interp(t_dst, t_src, x).astype(np.float32)


def rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(x, dtype=np.float64)))) if len(x) else 0.0


class Windower:
    """Accumulates samples and yields overlapping windows (default 5 s, 4 s hop).

    ``push`` returns a list of ``(end_offset_s, window)``, where ``end_offset_s`` is the stream time at
    the window's last sample, so the caller can map it to wall-clock time.
    """

    def __init__(self, rate: float, win_s: float = 5.0, hop_s: float = 4.0):
        self.rate = rate
        self.win = int(round(win_s * rate))
        self.hop = int(round(hop_s * rate))
        self.reset()

    def reset(self) -> None:
        self._buf = np.zeros(0, dtype=np.float32)
        self._buf_start = 0  # absolute sample index of self._buf[0]
        self._total = 0      # samples ever pushed
        self._next_end = self.win

    def push(self, samples: np.ndarray) -> list[tuple[float, np.ndarray]]:
        samples = np.asarray(samples, dtype=np.float32)
        self._buf = np.concatenate([self._buf, samples])
        self._total += len(samples)
        out = []
        while self._total >= self._next_end:
            s = self._next_end - self.win - self._buf_start
            out.append((self._next_end / self.rate, self._buf[s:s + self.win].copy()))
            self._next_end += self.hop
        drop = (self._next_end - self.win) - self._buf_start  # history before the next window's start
        if drop > 0:
            self._buf = self._buf[drop:]
            self._buf_start += drop
        return out


class PauseChunker:
    """Cuts a stream into non-overlapping chunks at natural pauses (``min_s``..``max_s`` long).

    Fixed overlapping windows split words at their edges, and Whisper hears the overlap differently in each window,
    which left duplicated or mangled words ("rose roast beef"). Cutting in silence avoids both. A pause is a run of
    ``pause_s`` of 20 ms frames quieter than an adaptive threshold; with no pause by ``max_s`` (music, fast talk),
    the cut goes at the quietest frame of the last ``search_s``. Same ``push`` contract as ``Windower``.
    """

    FRAME_S = 0.02

    def __init__(self, rate: float, min_s: float = 3.0, max_s: float = 15.0, pause_s: float = 0.3,
                 search_s: float = 3.0):
        self.rate = rate
        self.frame = max(1, int(round(self.FRAME_S * rate)))
        self.min_f = int(round(min_s / self.FRAME_S))
        self.max_f = int(round(max_s / self.FRAME_S))
        self.pause_f = max(1, int(round(pause_s / self.FRAME_S)))
        self.search_f = int(round(search_s / self.FRAME_S))
        self.reset()

    def reset(self) -> None:
        self._buf = np.zeros(0, dtype=np.float32)
        self._start = 0  # absolute sample index of self._buf[0]

    def _cut(self, levels: np.ndarray) -> int | None:
        """Frame index to cut at, or None to wait for more audio."""
        n = len(levels)
        if n < self.min_f:
            return None
        loud = levels[levels > 1e-4]
        thr = max(3e-4, 0.12 * float(np.median(loud))) if len(loud) else 3e-4
        quiet = levels < thr
        run = 0
        for i in range(n):
            run = run + 1 if quiet[i] else 0
            if run >= self.pause_f and i + 1 >= self.min_f:
                return i + 1 - run // 2  # middle of the pause
        if n >= self.max_f:
            lo = self.max_f - self.search_f
            return lo + int(np.argmin(levels[lo:self.max_f]))
        return None

    def push(self, samples: np.ndarray) -> list[tuple[float, np.ndarray]]:
        self._buf = np.concatenate([self._buf, np.asarray(samples, dtype=np.float32)])
        out = []
        while True:
            nf = len(self._buf) // self.frame
            if nf < self.min_f:
                break
            frames = self._buf[:nf * self.frame].reshape(nf, self.frame)
            levels = np.sqrt(np.mean(np.square(frames, dtype=np.float64), axis=1))
            cut = self._cut(levels)
            if cut is None:
                break
            k = max(1, cut) * self.frame
            out.append(((self._start + k) / self.rate, self._buf[:k].copy()))
            self._buf = self._buf[k:]
            self._start += k
        return out


def downmix(raw: bytes, channels: int) -> np.ndarray:
    """Interleaved float32 PCM bytes -> mono float32 (mean of channels). Drops a trailing partial frame."""
    x = np.frombuffer(raw, dtype=np.float32)
    ch = max(1, int(channels))
    if ch == 1:
        return x.copy()
    n = len(x) // ch * ch
    return x[:n].reshape(-1, ch).mean(axis=1).astype(np.float32)
