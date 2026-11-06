"""Save notes: turn a stretch of the buffer into a markdown note the user keeps, and find notes later.

A note is derived text only (summary, key points, details, action items, open questions); the raw transcript is never
written to disk. Notes are plain ``.md`` files with YAML frontmatter in ``notes_dir`` (default
``~/Documents/Hermes Notes``), so they open in Finder, Obsidian or any editor. Writing a note sends the window's lines
to the user's Hermes model once, the same path as asking about it.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from . import digest, runtime
from .buffer import STOPWORDS, _norm_words

log = logging.getLogger(__name__)

GENERATOR = "hermes-recorder"
FALLBACK_DIR = "~/Hermes Notes"
BUDGET = 40000          # transcript characters given to the note writer (older parts summarized to fit)
MIN_LINES = 3
READ_CAP = 8000
MAX_SCAN = 2000         # newest files considered by search

NOTE_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Specific title, at most 80 characters"},
        "summary": {"type": "string", "description": "One plain paragraph: what this was and what mattered"},
        "key_points": {"type": "array", "items": {"type": "string"}},
        "details": {"type": "array", "items": {
            "type": "object",
            "properties": {"heading": {"type": "string"}, "text": {"type": "string"}},
            "required": ["heading", "text"], "additionalProperties": False}},
        "decisions": {"type": "array", "items": {"type": "string"}},
        "action_items": {"type": "array", "items": {
            "type": "object",
            "properties": {"task": {"type": "string"},
                           "owner": {"type": "string", "description": "Who does it: a name, 'you' (the user), or ''"},
                           "due": {"type": "string", "description": "When, as said ('Friday', 'Nov 14'), or ''"}},
            "required": ["task", "owner", "due"], "additionalProperties": False}},
        "open_questions": {"type": "array", "items": {"type": "string"}},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "summary", "key_points", "details", "decisions", "action_items", "open_questions", "tags"],
    "additionalProperties": False,  # strict structured-output APIs (Anthropic) require it on every object
}

# Apps whose audio is a call: a session with any of them gets meeting notes.
CALL_APPS = frozenset({"us.zoom.xos", "com.apple.FaceTime", "com.apple.mobilephone", "com.microsoft.teams2",
                       "com.tinyspeck.slackmacgap", "com.hnc.Discord", "Cisco-Systems.Spark", "net.whatsapp.WhatsApp",
                       "org.whispersystems.signal-desktop"})

MEETING = (
    "This was a meeting or call. Write meeting notes someone can act on: put every decision in 'decisions', and every "
    "commitment or request in 'action_items' with its owner (a name as said, 'you' for the user speaking into the "
    "microphone, or '' if unclear) and due date as said ('Friday', 'next sprint', or ''). Key points and details "
    "cover the discussion itself.\n")
MEDIA = (
    "This was a video, lecture, podcast or stream. Write study notes: key points and details carry the content; "
    "decisions and action_items are usually empty (fill action_items only for things the user said they'd do).\n")

INSTRUCTIONS = (
    "Write notes from an automatic transcript of what the user watched, listened to or discussed on their Mac. "
    "Each line is '[time ago] source: text'; the source is an app (a video, podcast, the other people on a call), "
    "'you (microphone)' for the user, or 'summary' for a condensed older stretch.\n"
    "- title: specific (the topic, not 'Notes'), at most 80 characters.\n"
    "- summary: one plain paragraph.\n"
    "- key_points: 3 to 8 short, self-contained statements of the most important ideas, facts, numbers or decisions.\n"
    "- details: 1 to 6 sections with a heading and a few plain sentences each (explanations, definitions, examples, "
    "steps), in the order they came up.\n"
    "- decisions: what was agreed. action_items: {task, owner, due}. open_questions: things left unclear. Each may "
    "be empty.\n"
    "- tags: 2 to 6 lowercase topic words.\n"
    "Plain text inside fields, no markdown. Quietly fix obvious mishearings. Don't invent anything that isn't in the "
    "transcript, and don't mention the transcript, timestamps or its quality.")


# ---------------------------------------------------------------------------------------------------------- paths
def notes_dir() -> Path:
    return Path(os.path.expanduser(str(runtime.config("notes_dir") or "~/Documents/Hermes Notes")))


def _writable_dir() -> tuple[Path, str | None]:
    """The notes folder, or the fallback (with a note) when the configured one can't be written."""
    d = notes_dir()
    for cand in (d, Path(os.path.expanduser(FALLBACK_DIR))):
        try:
            cand.mkdir(parents=True, exist_ok=True)
            probe = cand / ".hermes-recorder-write-test"
            probe.write_text("")
            probe.unlink()
            return cand, None if cand == d else f"Couldn't write to {d}, so the note went to {cand} instead."
        except OSError as e:
            log.info("hermes-recorder: notes folder %s not writable: %s", cand, e)
    raise OSError(f"Couldn't write notes to {d} or {FALLBACK_DIR}")


def _slug(title: str) -> str:
    s = re.sub(r"[^\w\s-]", "", title, flags=re.UNICODE).strip()
    return re.sub(r"\s+", " ", s)[:70].strip() or "Notes"


def _unique(d: Path, stem: str) -> Path:
    p = d / f"{stem}.md"
    n = 2
    while p.exists():
        p = d / f"{stem} ({n}).md"
        n += 1
    return p


# ---------------------------------------------------------------------------------------------------------- render
def _clean(v: Any, limit: int = 2000) -> str:
    return " ".join(str(v or "").replace("\r", " ").split())[:limit]


def _items(v: Any, limit: int = 20) -> list[str]:
    return [t for t in (_clean(x, 600) for x in (v if isinstance(v, list) else [])[:limit]) if t]


def is_meeting(entries: list) -> bool:
    """A call app took part, or the user talked a fair share (e.g. Google Meet in a browser)."""
    if any(e.app in CALL_APPS for e in entries):
        return True
    mine = sum(1 for e in entries if e.speaker == "me")
    return mine >= 3 and mine >= 0.15 * len(entries)


def action_items(v: Any) -> list[dict[str, str]]:
    out = []
    for a in (v if isinstance(v, list) else [])[:30]:
        if isinstance(a, str):
            a = {"task": a}
        if isinstance(a, dict) and _clean(a.get("task")):
            out.append({k: _clean(a.get(k), 300) for k in ("task", "owner", "due")})
    return out


def _action_line(a: dict[str, str]) -> str:
    extra = ", ".join(x for x in (f"owner: {a['owner']}" if a["owner"] else "",
                                   f"due: {a['due']}" if a["due"] else "") if x)
    return f"- [ ] {a['task']}" + (f" ({extra})" if extra else "")


def _yaml_str(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)  # JSON strings are valid YAML scalars


def render(note: dict[str, Any], *, started: float, ended: float, sources: list[str], kind: str = "media") -> str:
    title = _clean(note.get("title"), 120) or "Notes"
    tags = [re.sub(r"[^\w-]", "", t.lower().replace(" ", "-")) for t in _items(note.get("tags"), 8)]
    tags = [t for t in tags if t]
    when = datetime.fromtimestamp(started)
    fm = [
        "---",
        f"title: {_yaml_str(title)}",
        f"date: {when.strftime('%Y-%m-%dT%H:%M')}",
        f"sources: [{', '.join(_yaml_str(s) for s in sources)}]",
        f"duration_min: {max(1, round((ended - started) / 60))}",
        f"tags: [{', '.join(tags)}]",
        f"kind: {kind}",
        f"generator: {GENERATOR}",
        "---",
    ]
    out = fm + ["", f"# {title}", "", _clean(note.get("summary"), 4000) or "(no summary)", ""]
    decisions, actions = _items(note.get("decisions")), action_items(note.get("action_items"))
    questions, kp = _items(note.get("open_questions")), _items(note.get("key_points"))
    sections: list[tuple[str, list[str]]] = []
    content: list[str] = []
    if kp:
        content += ["## Key points", "", *[f"- {t}" for t in kp], ""]
    for d in [d for d in (note.get("details") or []) if isinstance(d, dict)][:8]:
        h, t = _clean(d.get("heading"), 120), _clean(d.get("text"), 4000)
        if h and t:
            content += [f"## {h}", "", t, ""]
    if decisions:
        sections.append(("Decisions", [f"- {t}" for t in decisions]))
    if actions:
        sections.append(("Action items", [_action_line(a) for a in actions]))
    if questions:
        sections.append(("Open questions", [f"- {t}" for t in questions]))
    actionable = [x for h, items in sections for x in [f"## {h}", "", *items, ""]]
    # meetings lead with what to do; study notes lead with the content
    out += actionable + content if kind == "meeting" else content + actionable
    out += ["---", f"*Saved by hermes-recorder from {', '.join(sources) or 'recorded audio'} on "
                   f"{when.strftime('%b %-d, %Y at %-I:%M %p')}. Generated from a transcript; check anything important.*",
            ""]
    return "\n".join(out)


# ---------------------------------------------------------------------------------------------------------- save
def save(minutes: Any = None, title_hint: str | None = None, rec: Any = None, llm: Any = None,
         runner: Any = None) -> dict[str, Any]:
    """Write a note for the last ``minutes`` (default: the current or last session). Never raises."""
    t0 = time.time()
    try:
        rec = rec or runtime.get()
        now = time.time()
        max_min = rec.buffer.max_age_s / 60
        if minutes in (None, "", 0):
            started = getattr(rec, "started_at", None) or getattr(rec, "last_started_at", None)
            minutes = (now - started) / 60 + 0.1 if started else max_min
        minutes = min(max(float(minutes), 0.5), max_min)
        entries = rec.buffer.search("", minutes)
        if len(entries) < MIN_LINES:
            return {"error": f"There's not enough in the last {minutes:g} min to make notes from "
                             f"({len(entries)} line{'s' if len(entries) != 1 else ''})."}
        llm = llm or runtime.llm()
        if llm is None:
            return {"error": "Hermes didn't give the plugin model access (ctx.llm is unavailable)."}
        runner = runner or runtime.run_in_turn_context
        lines, dnote = digest.build(entries, BUDGET, now, llm=llm, runner=runner)
        from .ask import _format
        kind = "meeting" if is_meeting(entries) else "media"
        hint = f"\nThe user describes it as: {_clean(title_hint, 200)}" if title_hint else ""
        text = f"Transcript ({minutes:.0f} min){' — ' + dnote if dnote else ''}:{hint}\n{_format(lines)}"
        res = runner(llm.complete_structured, instructions=(MEETING if kind == "meeting" else MEDIA) + INSTRUCTIONS, input=[{"type": "text", "text": text}],
                     json_schema=NOTE_SCHEMA, max_tokens=2000, timeout=150,
                     purpose="hermes-recorder: write notes from a recording")
        note = getattr(res, "parsed", None)
        if not isinstance(note, dict):
            return {"error": "The model didn't return notes in the expected shape; try again."}
        sources = sorted({"You" if e.speaker == "me" else runtime.app_name(e.app) for e in entries})
        md = render(note, started=entries[0].ts, ended=entries[-1].ts, sources=sources, kind=kind)
        d, moved = _writable_dir()
        stem = f"{datetime.fromtimestamp(entries[0].ts).strftime('%Y-%m-%d %H%M')} {_slug(_clean(note.get('title'), 120))}"
        path = _unique(d, stem)
        tmp = path.with_name(f".{path.name}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(md)
        os.link(tmp, path)  # fails instead of overwriting if the name was just taken
        tmp.unlink()
        out = {"path": str(path), "title": _clean(note.get("title"), 120), "summary": _clean(note.get("summary"), 600),
               "minutes": round(minutes, 1), "seconds": round(time.time() - t0, 1), "kind": kind,
               "decisions": _items(note.get("decisions")), "action_items": action_items(note.get("action_items"))}
        if moved:
            out["note"] = moved
        log.info("hermes-recorder: saved notes (%d lines, %.1fs)", len(entries), out["seconds"])
        return out
    except Exception as e:
        log.warning("hermes-recorder: save notes failed: %s", e)
        if type(e).__name__ == "UnscopedSecretError" and not runtime.has_turn_context():
            return {"error": "Hermes restarted and hasn't seen a message yet, so the recorder can't reach your "
                             "model. Send Hermes any message, then try again."}
        return {"error": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------------------------------------------------- find
_FM = re.compile(r"\A---\n(.*?)\n---\n", re.S)


def _parse(path: Path) -> dict[str, Any] | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = _FM.match(text)
    if not m or f"generator: {GENERATOR}" not in m.group(1):
        return None
    meta: dict[str, Any] = {}
    for line in m.group(1).splitlines():
        k, _, v = line.partition(":")
        v = v.strip()
        if k in ("title",):
            try:
                v = json.loads(v)
            except ValueError:
                pass
        elif k in ("sources", "tags"):
            inner = v.strip("[]")
            try:
                v = json.loads(f"[{inner}]")
            except ValueError:
                v = [s.strip().strip('"') for s in inner.split(",") if s.strip()]
        meta[k] = v
    body = text[m.end():]
    summary = next((p.strip() for p in body.split("\n\n") if p.strip() and not p.lstrip().startswith("#")), "")
    return {"title": meta.get("title") or path.stem, "date": meta.get("date", ""), "sources": meta.get("sources", []),
            "tags": meta.get("tags", []), "kind": meta.get("kind", "media"), "summary": summary[:600],
            "path": str(path), "body": body}


def _inside(p: Path, d: Path) -> bool:
    try:
        rp, rd = p.resolve(), d.resolve()
    except OSError:
        return False
    return rd in rp.parents


def _dirs() -> list[Path]:
    out = []
    for d in (notes_dir(), Path(os.path.expanduser(FALLBACK_DIR))):
        if d.is_dir() and d not in out:
            out.append(d)
    return out


def read(path: str) -> dict[str, Any]:
    p = Path(os.path.expanduser(str(path)))
    if p.suffix != ".md" or not any(_inside(p, d) for d in _dirs()):
        return {"error": "That isn't a note in the notes folder."}
    n = _parse(p)
    if n is None:
        return {"error": "That file isn't a hermes-recorder note."}
    n["body"] = n["body"][:READ_CAP]
    return n


def search(query: str = "", days: Any = 30, limit: int = 10) -> dict[str, Any]:
    """Newest-first notes from the last ``days`` days, ranked by keyword matches when there's a query."""
    try:
        days = min(max(float(days or 30), 0.1), 3650)
    except (TypeError, ValueError):
        days = 30.0
    cutoff = time.time() - days * 86400
    files = sorted((p for d in _dirs() for p in d.glob("*.md") if p.stat().st_mtime >= cutoff),
                   key=lambda p: p.stat().st_mtime, reverse=True)[:MAX_SCAN]
    terms = [t for t in _norm_words(query) if t not in STOPWORDS]
    scored = []
    for p in files:
        n = _parse(p)
        if n is None:
            continue
        if terms:
            head = set(_norm_words(f"{n['title']} {' '.join(n['tags'])}"))
            body = set(_norm_words(n["body"]))
            score = sum(3 if t in head else 1 if t in body or any(w.startswith(t) for w in body if len(t) > 3) else 0
                        for t in terms)
            if not score:
                continue
        else:
            score = 0
        scored.append((score, n))
    scored.sort(key=lambda x: x[0], reverse=True)  # stable: newest first among equal scores
    hits = [n for _, n in scored[:max(1, int(limit))]]
    out: dict[str, Any] = {"notes": [{k: n[k] for k in ("title", "date", "kind", "sources", "tags", "summary", "path")}
                                     for n in hits],
                           "folder": str(notes_dir())}
    if len(hits) == 1:
        out["notes"][0]["body"] = hits[0]["body"][:READ_CAP]
    if not hits:
        out["note"] = (f"No saved notes matched '{query}' in the last {days:g} days." if terms
                       else f"No notes saved in the last {days:g} days.")
    return out
