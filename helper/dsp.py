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
