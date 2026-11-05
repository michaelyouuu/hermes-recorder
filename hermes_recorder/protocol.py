"""Wire protocol between the capture helper and the plugin: one JSON object per line.

Every message has ``v`` (protocol version), ``type`` and ``ts`` (epoch seconds). Helper -> plugin
types are listed in ``REQUIRED``; plugin -> helper messages are ``control``.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

PROTOCOL_VERSION = 1
MAX_LINE_BYTES = 64 * 1024

# type -> keys that must be present (besides v, type, ts)
REQUIRED: dict[str, tuple[str, ...]] = {
    "hello": ("pid", "helper_version", "token", "permissions"),
    "transcript": ("app", "speaker", "text"),
    "ui_event": ("app", "kind"),
    "app_switch": ("app",),
    "status": (),
    "error": ("message",),
    "control": ("op",),
}
CONTROL_OPS = ("pause", "resume", "stop", "set_apps")
SPEAKERS = ("me", "them")


class ProtocolError(ValueError):
    """A line that isn't a valid protocol message."""


@dataclass
class Message:
    type: str
    ts: float
    fields: dict[str, Any] = field(default_factory=dict)
    v: int = PROTOCOL_VERSION

    def get(self, key: str, default: Any = None) -> Any:
        return self.fields.get(key, default)

    @property
    def app(self) -> str | None:
        return self.fields.get("app")


def encode(msg: Message) -> bytes:
    body = {"v": msg.v, "type": msg.type, "ts": msg.ts, **msg.fields}
    return (json.dumps(body, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def control(op: str, ts: float, apps: list[str] | None = None) -> Message:
    if op not in CONTROL_OPS:
        raise ProtocolError(f"unknown control op: {op}")
    fields: dict[str, Any] = {"op": op}
    if apps is not None:
        fields["apps"] = list(apps)
    return Message("control", ts, fields)


def decode(line: bytes | str) -> Message:
    if isinstance(line, bytes):
        if len(line) > MAX_LINE_BYTES:
            raise ProtocolError("line too long")
        try:
            line = line.decode("utf-8")
        except UnicodeDecodeError as e:
            raise ProtocolError(f"not utf-8: {e}") from e
    try:
        obj = json.loads(line)
    except json.JSONDecodeError as e:
        raise ProtocolError(f"not json: {e}") from e
    if not isinstance(obj, dict):
        raise ProtocolError("not an object")
    v, mtype, ts = obj.pop("v", None), obj.pop("type", None), obj.pop("ts", None)
    if v != PROTOCOL_VERSION:
        raise ProtocolError(f"unsupported protocol version: {v!r}")
    if mtype not in REQUIRED:
        raise ProtocolError(f"unknown type: {mtype!r}")
    if not isinstance(ts, (int, float)) or isinstance(ts, bool):
        raise ProtocolError("ts must be a number")
    missing = [k for k in REQUIRED[mtype] if k not in obj]
    if missing:
        raise ProtocolError(f"{mtype} missing {missing}")
    if mtype == "transcript":
        if obj["speaker"] not in SPEAKERS:
            raise ProtocolError(f"bad speaker {obj['speaker']!r}")
        if not isinstance(obj["text"], str):
            raise ProtocolError("text must be a string")
    if mtype == "control" and obj["op"] not in CONTROL_OPS:
        raise ProtocolError(f"unknown control op: {obj['op']!r}")
    return Message(mtype, float(ts), obj, v)
