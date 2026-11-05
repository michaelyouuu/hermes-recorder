"""faster-whisper wrapper. The model must already be downloaded (setup does it); no network here."""
from __future__ import annotations

import numpy as np

from .segments import Segment


class Transcriber:
    def __init__(self, model: str = "base.en", threads: int = 4):
        from faster_whisper import WhisperModel
        self.model_name = model
        self.model = WhisperModel(model, device="cpu", compute_type="int8", cpu_threads=threads,
                                  local_files_only=True)

    def run(self, audio: np.ndarray) -> list[Segment]:
        segs, _ = self.model.transcribe(audio, language="en", beam_size=1, vad_filter=True,
                                        condition_on_previous_text=False, without_timestamps=True)
        return [Segment(s.text.strip(), float(s.avg_logprob), float(s.no_speech_prob)) for s in segs]
