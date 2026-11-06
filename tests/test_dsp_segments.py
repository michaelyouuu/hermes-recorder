from __future__ import annotations

import numpy as np
import pytest

from helper import dsp
from helper.segments import EchoFilter, Segment, keep


def tone(freq, rate, seconds):
    t = np.arange(int(rate * seconds)) / rate
    return np.sin(2 * np.pi * freq * t).astype(np.float32)


def dominant_freq(x, rate):
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    return np.fft.rfftfreq(len(x), 1 / rate)[np.argmax(spec)]


def test_resample_preserves_speech_band_and_length():
    x = tone(440, 44100, 1.0)
    y = dsp.resample(x, 44100)
    assert len(y) == 16000 and y.dtype == np.float32
    assert abs(dominant_freq(y, 16000) - 440) < 5
    assert 0.6 < dsp.rms(y) / dsp.rms(x) < 1.1


def test_resample_suppresses_aliasing():
    # 12 kHz is above the 8 kHz Nyquist of 16 kHz; without a low-pass it would fold to 4 kHz
    y = dsp.resample(tone(12000, 44100, 1.0), 44100)
    assert dsp.rms(y) < 0.1


def test_resample_noop_and_empty():
    x = tone(300, 16000, 0.5)
    assert np.array_equal(dsp.resample(x, 16000), x)
    assert len(dsp.resample(np.zeros(0, np.float32), 44100)) == 0


@pytest.mark.parametrize("chunk", [160, 1024, 16000, 37000])
def test_windower_yields_5s_windows_every_4s(chunk):
    rate = 16000
    w = dsp.Windower(rate)
    x = np.arange(rate * 14, dtype=np.float32)  # sample value == absolute index
    got = []
    for i in range(0, len(x), chunk):
        got += w.push(x[i:i + chunk])
    assert [end for end, _ in got] == [5.0, 9.0, 13.0]
    for end, win in got:
        assert len(win) == 5 * rate
        assert win[0] == (end - 5) * rate and win[-1] == end * rate - 1
    assert len(w._buf) <= 5 * rate + chunk  # bounded memory


def test_first_window_filler_and_confidence():
    assert not keep(Segment("Hi."), first_window=True)
    assert keep(Segment("Hi."), first_window=False)
    assert keep(Segment("Types of websites that are just eliminated"), first_window=True)
    assert not keep(Segment("words here today", no_speech_prob=0.9), False)
    assert not keep(Segment("words here today", avg_logprob=-1.5), False)
    assert not keep(Segment("   "), False)


def test_echo_filter_drops_speaker_bleed_only():
    f = EchoFilter()
    f.add_them(100.0, "So this is computer code almost for these web pages")
    assert f.is_echo(102.0, "this is computer code almost for these pages")
    assert not f.is_echo(102.0, "I think we should ship on Thursday")
    assert not f.is_echo(120.0, "this is computer code almost for these pages")  # too far apart
    assert not f.is_echo(101.0, "this is")  # too short to judge


def test_downmix_interleaved_stereo():
    left = np.array([1.0, 0.0, 0.5], np.float32)
    right = np.array([0.0, 1.0, 0.5], np.float32)
    raw = np.stack([left, right], axis=1).reshape(-1).tobytes()
    assert np.allclose(dsp.downmix(raw, 2), [0.5, 0.5, 0.5])


def test_downmix_mono_and_partial_frame():
    raw = np.array([0.1, 0.2, 0.3], np.float32).tobytes()
    assert np.allclose(dsp.downmix(raw, 1), [0.1, 0.2, 0.3])
    assert len(dsp.downmix(raw, 2)) == 1


def _speechy(rate, spans, total_s):
    """Tone bursts at the given (start, end) seconds, silence elsewhere."""
    t = np.arange(int(total_s * rate)) / rate
    x = np.zeros_like(t, dtype=np.float32)
    for a, b in spans:
        m = (t >= a) & (t < b)
        x[m] = 0.3 * np.sin(2 * np.pi * 220 * t[m])
    return x.astype(np.float32)


def test_pause_chunker_cuts_in_the_gap_without_overlap():
    rate = 16000
    x = _speechy(rate, [(0, 2.5), (3.0, 6.0), (6.4, 12.0)], 12.5)
    c = dsp.PauseChunker(rate, min_s=2, max_s=10)
    out = []
    for i in range(0, len(x), 1600):  # 0.1 s blocks, like live capture
        out += c.push(x[i:i + 1600])
    ends = [round(e, 2) for e, _ in out]
    assert 2.5 < ends[0] < 3.0 and 6.0 < ends[1] < 6.4  # cut inside the pauses
    assert sum(len(w) for _, w in out) == int(round(ends[-1] * rate))  # contiguous, no overlap


def test_pause_chunker_forces_a_cut_without_pauses():
    rate = 16000
    x = _speechy(rate, [(0, 25)], 25)
    c = dsp.PauseChunker(rate, min_s=2, max_s=10)
    out = c.push(x)
    assert len(out) >= 2 and all(7 <= len(w) / rate <= 10 for _, w in out)


def test_pause_chunker_any_rate():
    c = dsp.PauseChunker(48000, min_s=2, max_s=10)
    out = c.push(_speechy(48000, [(0, 2.2), (2.6, 5)], 5.5))
    assert out and 2.2 < out[0][0] < 2.6
