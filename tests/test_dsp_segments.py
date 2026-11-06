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
