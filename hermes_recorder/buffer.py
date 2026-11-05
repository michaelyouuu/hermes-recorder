"""Thread-safe, time-trimmed ring buffer of recorder events (RAM only)."""
from __future__ import annotations

import bisect
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .protocol import Message

_WORD = re.compile(r"[a-z0-9']+")
MAX_OVERLAP_WORDS = 15
MERGE_GAP_S = 8.0
# Question words that would otherwise match almost any line ("what did he say about X").
STOPWORDS = frozenset("""a an and are about at be did do does for from he her him his how i in is it its me my
of on or our say said she so that the their them they this to us was we were what when where which who
why will with you your just""".split())  # only merge consecutive lines of one stream that are this close in time


@dataclass
class Entry:
    kind: str  # transcript | ui_event | app_switch
    ts: float
    app: str | None
    speaker: str | None = None
    text: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def approx_bytes(self) -> int:
        return 120 + len(self.text) + sum(len(str(k)) + len(str(v)) for k, v in self.data.items())


def _norm_words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def strip_overlap(prev: str, new: str) -> str:
    """Remove the leading words of ``new`` that repeat the trailing words of ``prev``.

    Windows overlap by ~1 s, so the start of a window often repeats the end of the last one
    (S2: "...want them in our" / "because we don't want them in our dataset").
    """
    pw, nw_raw = _norm_words(prev), new.split()
    nw = [" ".join(_norm_words(w)) for w in nw_raw]
    # The repeated run may end up to 2 words before prev's end, because Whisper sometimes adds a
    # trailing filler ("so this is um" / "so this is computer code"). Single-word matches only count
    # at the exact end and for longer words, so a repeated "the" isn't eaten.
    for k in range(min(MAX_OVERLAP_WORDS, len(nw)), 0, -1):
        for d in range(0, 3):
            end = len(pw) - d
            if end - k < 0:
                continue
            if pw[end - k:end] == nw[:k] and (k >= 2 or (d == 0 and len(nw[0]) > 3)):
                return " ".join(nw_raw[k:])
    return new.strip()


class RingBuffer:
    def __init__(self, max_minutes: float = 30, max_entries: int = 50_000,
                 clock: Callable[[], float] = time.time):
        self.max_age_s = max(1.0, float(max_minutes) * 60)
        self.max_entries = max_entries
        self.clock = clock
        self._lock = threading.Lock()
        self._entries: list[Entry] = []
        self._ts: list[float] = []
        self._last_line: dict[tuple[str | None, str | None], Entry] = {}
        self.dropped = 0

    # ------------------------------------------------------------------ writes
    def add_message(self, msg: Message) -> Entry | None:
        f = msg.fields
        if msg.type == "transcript":
            return self.add(Entry("transcript", msg.ts, f.get("app"), f.get("speaker"), f.get("text", "").strip(),
                                  {k: v for k, v in f.items() if k in ("t_start", "t_end", "avg_logprob")}))
        if msg.type == "ui_event":
            data = {k: v for k, v in f.items() if k not in ("app",)}
            return self.add(Entry("ui_event", msg.ts, f.get("app"), data=data))
        if msg.type == "app_switch":
            return self.add(Entry("app_switch", msg.ts, f.get("app"), data={"app_name": f.get("app_name")}))
        return None

    def add(self, e: Entry) -> Entry | None:
        with self._lock:
            if e.kind == "transcript":
                key = (e.app, e.speaker)
                prev = self._last_line.get(key)
                if prev is not None and 0 <= e.ts - prev.ts <= MERGE_GAP_S:
                    e.text = strip_overlap(prev.text, e.text)
                if not e.text:
                    return None
                self._last_line[key] = e
            i = bisect.bisect_right(self._ts, e.ts)
            self._entries.insert(i, e)
            self._ts.insert(i, e.ts)
            self._trim_locked()
            return e

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._ts.clear()
            self._last_line.clear()

    def _trim_locked(self) -> None:
        cutoff = self.clock() - self.max_age_s
        n = bisect.bisect_left(self._ts, cutoff)
        n = max(n, len(self._entries) - self.max_entries)
        if n > 0:
            self.dropped += n
            del self._entries[:n]
            del self._ts[:n]

    # ------------------------------------------------------------------ reads
    def slice(self, t0: float, t1: float, kinds: tuple[str, ...] | None = None) -> list[Entry]:
        with self._lock:
            self._trim_locked()
            i, j = bisect.bisect_left(self._ts, t0), bisect.bisect_right(self._ts, t1)
            out = self._entries[i:j]
        return [e for e in out if kinds is None or e.kind in kinds]

    def recent(self, minutes: float, kinds: tuple[str, ...] | None = None) -> list[Entry]:
        now = self.clock()
        return self.slice(now - float(minutes) * 60, now + 1, kinds)

    def search(self, query: str, minutes: float, app: str | None = None,
               speaker: str = "any") -> list[Entry]:
        terms = [t for t in _norm_words(query) if t not in STOPWORDS]
        lines = [e for e in self.recent(minutes, ("transcript",))
                 if (app is None or e.app == app) and (speaker in ("any", None) or e.speaker == speaker)]
        if not terms:
            return lines
        scored = []
        for e in lines:
            words = set(_norm_words(e.text))
            hits = sum(1 for t in terms if t in words or any(w.startswith(t) for w in words if len(t) > 3))
            if hits:
                scored.append((hits, e))
        if not scored:
            return []
        top = max(h for h, _ in scored)
        # with several terms, allow lines missing one of them; never keep lines matching only a minority
        floor = top if top < 3 else top - 1
        keep = [e for h, e in scored if h >= max(1, floor)]
        return sorted(keep, key=lambda e: e.ts)

    def span(self) -> tuple[float, float] | None:
        with self._lock:
            self._trim_locked()
            return (self._ts[0], self._ts[-1]) if self._ts else None

    def stats(self) -> dict[str, Any]:
        with self._lock:
            self._trim_locked()
            counts: dict[str, int] = {}
            for e in self._entries:
                counts[e.kind] = counts.get(e.kind, 0) + 1
            size = sum(e.approx_bytes() for e in self._entries)
            span = (self._ts[-1] - self._ts[0]) if self._ts else 0.0
        return {"counts": counts, "span_s": round(span, 1), "approx_bytes": size, "dropped": self.dropped,
                "max_minutes": self.max_age_s / 60}
