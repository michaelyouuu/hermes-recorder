"""Pure transcript post-processing (no macOS or model imports)."""
from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass

NO_SPEECH_MAX = 0.6
LOGPROB_MIN = -1.0
FIRST_WINDOW_MIN_WORDS = 3
ECHO_WINDOW_S = 6.0
ECHO_MIN_WORDS = 3
ECHO_RATIO = 0.6
_WORD = re.compile(r"[a-z0-9']+")


@dataclass
class Segment:
    text: str
    avg_logprob: float = 0.0
    no_speech_prob: float = 0.0


def words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def keep(seg: Segment, first_window: bool) -> bool:
    """Drop hallucinated or low-confidence segments.

    S2 showed the first window of a stream inventing a short filler ("Hi.", "All right.") when capture
    starts mid-word, so short segments in the first window are dropped too.
    """
    text = seg.text.strip()
    if not text:
        return False
    if seg.no_speech_prob > NO_SPEECH_MAX or seg.avg_logprob < LOGPROB_MIN:
        return False
    if first_window and len(words(text)) < FIRST_WINDOW_MIN_WORDS:
        return False
    return True


class EchoFilter:
    """Drop mic ("me") lines that repeat recent app audio ("them") bleeding from the speakers."""

    def __init__(self, window_s: float = ECHO_WINDOW_S):
        self.window_s = window_s
        self._them: deque[tuple[float, set[str]]] = deque()

    def add_them(self, ts: float, text: str) -> None:
        self._them.append((ts, set(words(text))))
        while self._them and self._them[0][0] < ts - 4 * self.window_s:
            self._them.popleft()

    def is_echo(self, ts: float, text: str) -> bool:
        w = words(text)
        if len(w) < ECHO_MIN_WORDS:
            return False
        near: set[str] = set()
        for t, ws in self._them:
            if abs(t - ts) <= self.window_s:
                near |= ws
        if not near:
            return False
        return sum(1 for x in w if x in near) / len(w) >= ECHO_RATIO
