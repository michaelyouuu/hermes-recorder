"""Tool schemas. Descriptions tell the model exactly when to use each tool."""
from __future__ import annotations

RECALL_SCHEMA = {
    "name": "screen_recall",
    "description": (
        "Read the local transcript of what the user just heard or watched on their Mac (videos, calls, "
        "streams, podcasts) and what they said out loud. Use when the user asks about something they just "
        "heard, watched or discussed ('what did he just say about X', 'summarize the last 10 minutes', "
        "'what time did we agree on'). Lines are oldest first; 't' is how long ago (e.g. '-01:12'); "
        "speaker 'me' is the user's microphone, 'them' is app audio."),
    "parameters": {
        "type": "object",
        "properties": {
            "minutes": {"type": "number", "description": "How far back to look (default 5)."},
            "query": {"type": "string", "description": (
                "Optional exact words to search for, e.g. 'learning rate'. Matching is by keyword, so for "
                "questions about meaning ('what did we agree on') leave it empty and read the lines.")},
            "app": {"type": "string", "description": "Optional bundle id to limit to one app."},
            "speaker": {"type": "string", "enum": ["me", "them", "any"], "description": "Default 'any'."},
        },
        "required": [],
    },
}

STATUS_SCHEMA = {
    "name": "recorder_status",
    "description": (
        "Report whether local capture is running, which apps are captured, how many minutes are in the "
        "buffer, permission status and helper health. Use when the user asks if recording is on, or "
        "before relying on screen_recall when unsure."),
    "parameters": {"type": "object", "properties": {}, "required": []},
}

CONTROL_SCHEMA = {
    "name": "recorder_control",
    "description": (
        "Start, stop, pause or resume local capture, or change which apps are captured. Use only when the "
        "user asks to turn capture on/off or change apps. Nothing is captured until started with at least "
        "one app. Apps are macOS bundle ids, e.g. com.google.Chrome, com.microsoft.edgemac, us.zoom.xos, "
        "com.apple.FaceTime. Password managers are never captured."),
    "parameters": {
        "type": "object",
        "properties": {
            "op": {"type": "string", "enum": ["start", "stop", "pause", "resume", "set_apps"]},
            "apps": {"type": "array", "items": {"type": "string"},
                     "description": "Bundle ids for start or set_apps (start defaults to the 'apps' setting)."},
        },
        "required": ["op"],
    },
}

DRAFT_SCHEMA = {
    "name": "recorder_draft_skill",
    "description": (
        "Turn what the user just did on their Mac (recorded clicks, typing and narration) into a draft "
        "SKILL.md, with detected inputs and 2-3 edge-case questions to ask the user. Use when the user "
        "says 'turn what I just did into a skill' or similar. Show the draft and ask the questions before "
        "saving it with skill_manage."),
    "parameters": {
        "type": "object",
        "properties": {
            "minutes": {"type": "number", "description": "How far back the demonstration starts (default 5)."},
            "goal_hint": {"type": "string", "description": "Optional: what the user says the task was."},
        },
        "required": [],
    },
}
