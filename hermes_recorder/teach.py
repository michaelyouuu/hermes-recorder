"""Teach mode: turn what the user just did (clicks, typing, narration) into a draft SKILL.md.

1. ``normalize``: buffer slice → compact steps (open app, click "New issue", type into "Issue title" …), with the
   user's narration attached to the step it explains. Noise is dropped (focus clicks, repeated app switches).
2. ``draft``: one ``ctx.llm.complete_structured`` call generalizes the steps: values that change between runs become
   inputs, irreversible steps become approval points, and 2–3 edge-case questions are asked back.
3. ``render``: a SKILL.md that passes Hermes's skill lint (frontmatter, ``## When to Use`` first).

If the user just saved meeting notes, their action items are passed along, so the skill can be written as "file
each action item from the meeting" and replayed after the next meeting ("Follow up").
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from typing import Any

from . import runtime

log = logging.getLogger(__name__)

MAX_STEPS = 150
NARRATION_GAP_S = 20.0   # narration attaches to the next step within this many seconds
SECRET = "{SECRET}"

DRAFT_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "kebab-case, at most 64 characters"},
        "description": {"type": "string", "description": "At most 60 characters"},
        "goal": {"type": "string", "description": "One sentence: what the task achieves"},
        "when_to_use": {"type": "array", "items": {"type": "string"}},
        "inputs": {"type": "array", "items": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "description": {"type": "string"},
                           "example": {"type": "string"}},
            "required": ["name", "description", "example"], "additionalProperties": False}},
        "steps": {"type": "array", "items": {
            "type": "object",
            "properties": {"n": {"type": "integer"}, "app": {"type": "string"}, "action": {"type": "string"},
                           "target": {"type": "string"}, "value_ref": {"type": "string"},
                           "why": {"type": "string"}},
            "required": ["n", "app", "action", "target", "value_ref", "why"], "additionalProperties": False}},
        "verify": {"type": "array", "items": {"type": "string"}},
        "edge_case_questions": {"type": "array", "items": {"type": "string"}},
        "approval_points": {"type": "array", "items": {"type": "integer"}},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["name", "description", "goal", "when_to_use", "inputs", "steps", "verify", "edge_case_questions",
                 "approval_points", "tags"],
    "additionalProperties": False,
}

INSTRUCTIONS = (
    "You turn a recording of someone doing a task on their Mac into a reusable skill another agent can follow with "
    "computer-use and browser tools. You get the recorded steps (app, action, target element, typed value) and what "
    "the user said while doing it.\n"
    "- Generalize: values that would change next time (titles, names, dates, amounts, descriptions, assignees) "
    "become inputs, referenced in steps as {input_name} in value_ref. Fixed choices (a team, a label they always "
    "use) stay literal. Never put a secret in an input example; {SECRET} means the user typed a password: say "
    "'the user enters their password' and make it an approval point.\n"
    "- Steps: one per meaningful action, in order, describing targets by their visible names ('New issue button', "
    "'Title field'), not coordinates. Merge noise. 'why' carries the user's reasons when they said them, else a "
    "short purpose. Use app names, not bundle ids.\n"
    "- approval_points: the step numbers that create, send, submit, pay, delete or publish something.\n"
    "- verify: how to check it worked (what should be visible afterwards).\n"
    "- edge_case_questions: 2 or 3 short questions about cases the recording didn't show (what if X is missing, "
    "which value when Y), so the skill handles more than the happy path.\n"
    "- when_to_use: concrete triggers, e.g. 'after a meeting, to file its action items in Linear'.\n"
    "- name: kebab-case verb phrase (file-meeting-action-items-in-linear); description at most 60 characters; "
    "tags 2-5 lowercase words.\n"
    "Plain text in every field.")


# ---------------------------------------------------------------------------------------------------------- normalize
def label(t: dict[str, Any] | None) -> str:
    t = t or {}
    role = str(t.get("role") or "element").removeprefix("AX")
    role = {"TextField": "field", "TextArea": "text area", "Button": "button", "Link": "link", "MenuItem": "menu item",
            "CheckBox": "checkbox", "PopUpButton": "menu", "ComboBox": "field", "StaticText": "text",
            "Cell": "cell", "Row": "row", "Tab": "tab", "RadioButton": "option"}.get(role, role.lower())
    for k in ("title", "desc", "placeholder", "text", "label", "identifier"):
        if t.get(k):
            return f'"{t[k]}" {role}'
    inner = t.get("inner") or {}
    for k in ("text", "title", "desc"):
        if inner.get(k):
            return f'"{inner[k]}" {role}'
    return role


def normalize(entries: list, t0: float) -> dict[str, Any]:
    """Buffer entries (any kind) → {"steps": [...], "narration": [...], "apps": [...]}."""
    steps: list[dict[str, Any]] = []
    narration: list[dict[str, Any]] = []
    apps: list[str] = []
    for e in sorted(entries, key=lambda e: e.ts):
        t = round(e.ts - t0, 1)
        if e.kind == "transcript":
            if e.speaker == "me" and e.text:
                narration.append({"t": t, "text": e.text})
            continue
        name = runtime.app_name(e.app) if e.app else ""
        if e.kind == "app_switch":
            if e.app and e.app != "com.apple.dock" and (not steps or steps[-1].get("app") != name):
                steps.append({"t": t, "app": name, "action": "switch to app"})
            continue
        if e.kind != "ui_event":
            continue
        d = e.data
        target = d.get("target") or {}
        if e.app and name not in apps and e.app != "com.apple.dock":
            apps.append(name)
        step: dict[str, Any] = {"t": t, "app": name}
        if d.get("window"):
            step["window"] = d["window"]
        kind = d.get("kind")
        if kind == "click":
            if e.app == "com.apple.dock" and target.get("title"):
                steps.append({"t": t, "app": "Dock", "action": "open app", "target": target["title"]})
                continue
            if target.get("subrole") == "AXSecureTextField":
                steps.append({**step, "action": "enter password", "target": label(target)})
                continue
            steps.append({**step, "action": "click", "target": label(target)})
        elif kind == "typed":
            lab = label(target)
            text = d.get("value") if d.get("value") is not None else d.get("inserted")
            if text is None:
                text = ""
            if steps and steps[-1].get("action") == "click" and steps[-1].get("target") == lab:
                steps.pop()  # the click only focused the field typed into next
            if steps and steps[-1].get("action") == "type" and steps[-1].get("target") == lab and text != SECRET:
                prev = steps[-1]
                prev["text"] = text if "value" in d and d.get("value") is not None else prev.get("text", "") + text
            else:
                steps.append({**step, "action": "type", "target": lab, "text": text})
            if d.get("special"):
                keys = [k for k in d["special"] if k in ("return", "cmd+return", "tab")]
                if keys:
                    steps[-1]["then_press"] = keys[-1]
        elif kind == "key":
            steps.append({**step, "action": "press", "target": d.get("key", "")})
    # attach narration to the step it precedes (people say what they're about to do), else the one before
    for n in narration:
        nxt = next((s for s in steps if 0 <= s["t"] - n["t"] <= NARRATION_GAP_S), None)
        prv = next((s for s in reversed(steps) if 0 <= n["t"] - s["t"] <= NARRATION_GAP_S), None)
        s = nxt or prv
        if s is not None:
            s["said"] = (s.get("said", "") + " " + n["text"]).strip()
    for i, s in enumerate(steps[:MAX_STEPS], 1):
        s["n"] = i
    return {"steps": steps[:MAX_STEPS], "narration": narration, "apps": apps, "truncated": len(steps) > MAX_STEPS}


# ---------------------------------------------------------------------------------------------------------- render
def _clean(v: Any, limit: int = 400) -> str:
    return " ".join(str(v or "").split())[:limit]


def _slug(v: Any) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(v or "").lower()).strip("-")
    return (s or "recorded-task")[:64].rstrip("-")


def render(d: dict[str, Any], *, apps: list[str], duration_s: float, when: float) -> str:
    name = _slug(d.get("name"))
    desc = _clean(d.get("description"), 60) or "Recorded task"
    tags = [t for t in (_slug(x) for x in (d.get("tags") or [])[:5]) if t] or ["recorded"]
    inputs = [i for i in (d.get("inputs") or []) if isinstance(i, dict) and i.get("name")]
    steps = [s for s in (d.get("steps") or []) if isinstance(s, dict)]
    approvals = sorted({int(n) for n in (d.get("approval_points") or []) if isinstance(n, int)})
    out = [
        "---",
        f"name: {name}",
        f"description: {desc}",
        "version: 0.1.0",
        "author: recorded with hermes-recorder",
        "license: MIT",
        "metadata:",
        "  hermes:",
        f"    tags: [{', '.join(tags)}]",
        "    related_skills: [hermes-recorder:replay-recorded]",
        "---",
        "",
        f"# {_clean(d.get('goal'), 200) or desc}",
        "",
        "## When to Use",
        "",
        *[f"- {_clean(w)}" for w in (d.get("when_to_use") or [])[:6]],
        "",
        "## Inputs",
        "",
    ]
    out += [f"- `{_slug(i['name']).replace('-', '_')}`: {_clean(i.get('description'))}"
            + (f" (e.g. {_clean(i.get('example'), 80)})" if i.get("example") else "") for i in inputs] or ["- None"]
    out += ["", "## Steps", ""]
    for i, s in enumerate(steps, 1):
        parts = [f"{i}."]
        if s.get("app"):
            parts.append(f"In {_clean(s['app'], 40)},")
        parts.append(_clean(s.get("action"), 80))
        if s.get("target"):
            parts.append(_clean(s["target"], 120))
        if s.get("value_ref"):
            parts.append(f"→ {_clean(s['value_ref'], 160)}")
        line = " ".join(parts)
        if s.get("why"):
            line += f" — {_clean(s['why'], 200)}"
        if i in approvals:
            line += " **(ask the user to approve first)**"
        out.append(line)
    out += ["", "## Approval points", ""]
    out += [f"- Step {n}: confirm with the user before doing it." for n in approvals] or [
        "- None recorded; still confirm before anything that sends, pays, deletes or publishes."]
    out += ["", "## Verify", ""]
    out += [f"- {_clean(v)}" for v in (d.get("verify") or [])[:6]] or ["- Check the result is visible."]
    out += ["", "## Edge cases", ""]
    out += [f"- {_clean(q)} (unanswered: ask the user)" for q in (d.get("edge_case_questions") or [])[:3]] or ["- None yet."]
    out += ["", "## Recorded from", "",
            f"- {datetime.fromtimestamp(when).strftime('%Y-%m-%d %H:%M')}, {', '.join(apps) or 'unknown apps'}, "
            f"about {max(1, round(duration_s / 60))} min. Steps and narration only; no audio or screenshots kept.", ""]
    return "\n".join(out)


def lint(md: str) -> list[str]:
    """Problems against Hermes's skill lint rules (empty list = clean)."""
    probs = []
    fm = md.split("---", 2)[1] if md.startswith("---") else ""
    for key in ("name:", "description:", "version:", "author:", "license:", "tags:", "related_skills:"):
        if key not in fm:
            probs.append(f"missing {key[:-1]}")
    m = re.search(r"^description: (.*)$", fm, re.M)
    if m and len(m.group(1)) > 60:
        probs.append("description over 60 characters")
    body = md.split("---", 2)[2] if md.count("---") >= 2 else md
    first_h2 = re.search(r"^## (.+)$", body, re.M)
    if not first_h2 or first_h2.group(1).strip() != "When to Use":
        probs.append("'## When to Use' must be the first section")
    return probs


# ---------------------------------------------------------------------------------------------------------- draft
def _recent_meeting_actions() -> str:
    try:
        from . import notes
        found = notes.search("", days=1, limit=5)["notes"]
        meeting = next((n for n in found if n.get("kind") == "meeting"), None)
        if not meeting:
            return ""
        body = notes.read(meeting["path"]).get("body", "")
        items = re.findall(r"^- \[ \] (.+)$", body, re.M)
        if items:
            return (f"The user may have been acting on action items from their meeting '{meeting['title']}':\n"
                    + "\n".join(f"- {i}" for i in items[:15]))
    except Exception as e:
        log.debug("hermes-recorder: no meeting context for teach: %s", e)
    return ""


def draft(minutes: Any = 5, goal_hint: str | None = None, rec: Any = None, llm: Any = None,
          runner: Any = None) -> dict[str, Any]:
    """Never raises. Returns the draft (draft_md, inputs, questions, steps…) or {error}."""
    try:
        rec = rec or runtime.get()
        if not runtime.config("capture_events"):
            return {"error": "Teach mode is off. Turn on 'Capture clicks and typing' in the hermes-recorder settings "
                             "(or set capture_events: true), allow Accessibility and Input Monitoring for Hermes "
                             "Recorder Helper (`hermes recorder doctor`), restart capture, then do the task again."}
        try:
            minutes = min(max(float(minutes or 5), 0.5), rec.buffer.max_age_s / 60)
        except (TypeError, ValueError):
            minutes = 5.0
        now = time.time()
        entries = rec.buffer.recent(minutes)
        events = [e for e in entries if e.kind == "ui_event"]
        if len(events) < 2:
            return {"error": f"No clicks or typing were recorded in the last {minutes:g} min. Teach mode records "
                             "only the apps being captured: add the app you worked in (e.g. `/recorder on chrome`), "
                             "do the task again, then ask me to turn it into a skill."}
        t0 = min(e.ts for e in events) - 5
        norm = normalize([e for e in entries if e.ts >= t0], t0)
        llm = llm or runtime.llm()
        if llm is None:
            return {"error": "Hermes didn't give the plugin model access (ctx.llm is unavailable)."}
        import json
        context = "\n\n".join(x for x in (
            f"The user describes the task as: {_clean(goal_hint, 300)}" if goal_hint else "",
            _recent_meeting_actions(),
            "Recorded steps (JSON):\n" + json.dumps(norm["steps"], ensure_ascii=False),
            "Everything the user said (JSON):\n" + json.dumps(norm["narration"], ensure_ascii=False),
        ) if x)
        runner = runner or runtime.run_in_turn_context
        res = runner(llm.complete_structured, instructions=INSTRUCTIONS, input=[{"type": "text", "text": context}],
                     json_schema=DRAFT_SCHEMA, max_tokens=3000, timeout=150,
                     purpose="hermes-recorder: draft a skill from a recorded demonstration")
        d = getattr(res, "parsed", None)
        if not isinstance(d, dict):
            return {"error": "The model didn't return a draft in the expected shape; try again."}
        md = render(d, apps=norm["apps"], duration_s=now - t0, when=t0)
        problems = lint(md)
        return {
            "name": _slug(d.get("name")), "draft_md": md, "lint": problems,
            "detected_inputs": d.get("inputs") or [], "questions": (d.get("edge_case_questions") or [])[:3],
            "approval_points": d.get("approval_points") or [], "steps_recorded": len(norm["steps"]),
            "next": ("Show the user this draft and ask the questions. Revise the draft with their answers (replace "
                     "'unanswered' edge cases with the answers), show the final version, and only after they "
                     "approve save it with skill_manage (action: create, name, content). Then offer a test run on "
                     "a new input."),
        }
    except Exception as e:
        log.warning("hermes-recorder: draft skill failed: %s", e)
        if type(e).__name__ == "UnscopedSecretError" and not runtime.has_turn_context():
            return {"error": "Hermes restarted and hasn't seen a message yet; send any message and try again."}
        return {"error": f"{type(e).__name__}: {e}"}
