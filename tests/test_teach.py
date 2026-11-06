from __future__ import annotations

import json
import time

import pytest

from hermes_recorder import registration, runtime, teach
from hermes_recorder.buffer import Entry, RingBuffer
from hermes_recorder.privacy import mask
from test_tools import FakeCtx

EDGE = "com.microsoft.edgemac"


def ui(ts, kind, target, **kw):
    return Entry("ui_event", ts, EDGE, data={"kind": kind, "target": target, "window": "Linear — Issues", **kw})


def linear_demo(now=None):
    """Filing a Linear issue from a meeting's action item, with narration."""
    now = now or time.time()
    t = now - 90
    return [
        Entry("app_switch", t, EDGE, data={"app_name": "Microsoft Edge"}),
        Entry("transcript", t + 1, None, "me", "I'll file the onboarding bug from the call, it's blocking launch"),
        ui(t + 3, "click", {"role": "AXButton", "title": "New issue"}),
        ui(t + 5, "click", {"role": "AXTextField", "placeholder": "Issue title"}),
        ui(t + 6, "typed", {"role": "AXTextField", "placeholder": "Issue title"}, value="Onboarding fails on SSO"),
        ui(t + 12, "typed", {"role": "AXTextArea", "placeholder": "Add description"},
           inserted="From the Q3 sync, owner sam@acme.com", deleted_chars=0),
        ui(t + 20, "click", {"role": "AXButton", "title": "Priority"}),
        Entry("transcript", t + 21, None, "me", "high priority because the client named a date"),
        ui(t + 22, "click", {"role": "AXStaticText", "inner": {"text": "High"}}),
        ui(t + 30, "key", {"role": "AXTextArea"}, key="cmd+return"),
        Entry("transcript", t + 31, "us.zoom.xos", "them", "unrelated call audio"),
    ]


def test_normalize_merges_noise_and_attaches_narration():
    entries = linear_demo()
    norm = teach.normalize(entries, entries[0].ts)
    acts = [(s["action"], s.get("target")) for s in norm["steps"]]
    assert acts == [("switch to app", None), ("click", '"New issue" button'), ("type", '"Issue title" field'),
                    ("type", '"Add description" text area'), ("click", '"Priority" button'),
                    ("click", '"High" text'), ("press", "cmd+return")]
    title = norm["steps"][2]
    assert title["text"] == "Onboarding fails on SSO" and title["window"] == "Linear — Issues"
    assert "onboarding bug" in norm["steps"][1]["said"]  # narration before the first click
    assert "high priority" in norm["steps"][5]["said"]
    assert norm["apps"] == ["Microsoft Edge"] and [s["n"] for s in norm["steps"]] == list(range(1, 8))
    assert all("unrelated" not in json.dumps(s) for s in norm["steps"])  # other people's audio isn't narration


def test_emails_are_masked_before_the_buffer():
    assert mask("owner sam@acme.com today") == "owner {EMAIL} today"


DRAFT = {
    "name": "File Meeting Action Items in Linear", "description": "File a meeting action item as a Linear issue",
    "goal": "File each meeting action item as a Linear issue", "when_to_use": ["After a meeting, to file its action items"],
    "inputs": [{"name": "issue title", "description": "The action item", "example": "Onboarding fails on SSO"}],
    "steps": [{"n": 1, "app": "Microsoft Edge", "action": "click", "target": "New issue button", "value_ref": "",
               "why": "start an issue"},
              {"n": 2, "app": "Microsoft Edge", "action": "type", "target": "Title field", "value_ref": "{issue_title}",
               "why": ""},
              {"n": 3, "app": "Microsoft Edge", "action": "press", "target": "cmd+return", "value_ref": "",
               "why": "create it"}],
    "verify": ["The new issue appears in the list"], "edge_case_questions": ["What if no owner was named?"],
    "approval_points": [3], "tags": ["linear", "meetings"]}


class LLM:
    def __init__(self):
        self.calls = []

    def complete_structured(self, **kw):
        self.calls.append(kw)
        return type("R", (), {"parsed": DRAFT})()


@pytest.fixture
def ctx(tmp_path):
    c = FakeCtx({"capture_events": True, "notes_dir": str(tmp_path / "notes")})
    registration.register_all(c)
    yield c
    runtime.reset_for_tests(None)


def test_draft_renders_a_lint_clean_skill(ctx):
    b = RingBuffer(90)
    for e in linear_demo():
        b.add(e)
    rec = type("R", (), {"buffer": b})()
    llm = LLM()
    r = teach.draft(5, "file the action item in Linear", rec=rec, llm=llm, runner=lambda f, *a, **k: f(*a, **k))
    assert "error" not in r, r
    assert r["lint"] == [] and r["name"] == "file-meeting-action-items-in-linear"
    md = r["draft_md"]
    assert md.startswith("---\nname: file-meeting-action-items-in-linear\n")
    assert "related_skills: [hermes-recorder:replay-recorded]" in md
    assert md.index("## When to Use") < md.index("## Inputs") < md.index("## Steps") < md.index("## Approval points")
    assert "3. In Microsoft Edge, press cmd+return — create it **(ask the user to approve first)**" in md
    assert "`issue_title`" in md and "(unanswered: ask the user)" in md
    sent = llm.calls[0]["input"][0]["text"]
    assert "Onboarding fails on SSO" in sent and "file the action item in Linear" in sent
    assert llm.calls[0]["json_schema"] is teach.DRAFT_SCHEMA and r["questions"] == ["What if no owner was named?"]


def test_draft_uses_the_last_meetings_action_items(ctx, tmp_path):
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "m.md").write_text(
        '---\ntitle: "Q3 sync"\ndate: 2026-11-06T10:00\nsources: ["Zoom"]\ntags: []\nkind: meeting\n'
        'generator: hermes-recorder\n---\n\n# Q3 sync\n\nSummary.\n\n## Action items\n\n'
        '- [ ] File the onboarding bug in Linear (owner: you, due: Friday)\n')
    b = RingBuffer(90)
    for e in linear_demo():
        b.add(e)
    llm = LLM()
    teach.draft(5, rec=type("R", (), {"buffer": b})(), llm=llm, runner=lambda f, *a, **k: f(*a, **k))
    assert "File the onboarding bug in Linear (owner: you, due: Friday)" in llm.calls[0]["input"][0]["text"]


def test_draft_explains_what_is_missing(ctx):
    rec = type("R", (), {"buffer": RingBuffer(30)})()
    assert "No clicks or typing" in teach.draft(5, rec=rec, llm=LLM())["error"]
    ctx.config["capture_events"] = False
    assert "Teach mode is off" in teach.draft(5, rec=rec, llm=LLM())["error"]


def test_lint_catches_problems():
    assert teach.lint("no frontmatter") and "description over 60 characters" in teach.lint(
        "---\nname: x\ndescription: " + "y" * 61 + "\nversion: 1\nauthor: a\nlicense: MIT\ntags: []\n"
        "related_skills: []\n---\n## When to Use\n")


def test_bundled_skill_registered_and_lint_clean(ctx):
    path = ctx.skills["replay-recorded"]
    text = path.read_text()
    assert "## When to Use" in text and not [p for p in teach.lint(text) if "When to Use" not in p]
