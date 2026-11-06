"""Privacy rules applied to every message before it enters the buffer."""
from __future__ import annotations

import re
from typing import Any, Iterable

from .protocol import Message

# Never captured, even if the user lists them.
HARD_DENYLIST = frozenset({
    "com.1password.1password",
    "com.agilebits.onepassword7",
    "com.agilebits.onepassword-osx",
    "com.bitwarden.desktop",
    "com.apple.keychainaccess",
    "com.apple.Passwords",
    "com.lastpass.LastPass",
    "com.dashlane.dashlanephonefinal",
})

# System UI kept as context for teach mode (e.g. "opened Notes from the Dock"); never audio sources.
CONTEXT_APPS = frozenset({"com.apple.dock"})

SECRET = "{SECRET}"
_MASKS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b\d(?:[ -]?\d){12,18}\b"), "{CARD}"),
    (re.compile(r"\b(?:sk|pk|rk|ghp|gho|ghs|xox[abprs]|AKIA)[-_A-Za-z0-9]{12,}\b"), "{API_KEY}"),
    (re.compile(r"\b[A-Za-z0-9_\-]{32,}\b"), "{TOKEN}"),
    (re.compile(r"^\s*\d{4,8}\s*$"), "{CODE}"),
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "{EMAIL}"),
]


def mask(text: str) -> str:
    for rx, rep in _MASKS:
        text = rx.sub(rep, text)
    return text


def clean_apps(apps: Iterable[str]) -> list[str]:
    """Drop denylisted and blank bundle ids, keep order, de-duplicate."""
    out: list[str] = []
    for a in apps:
        a = str(a).strip()
        if a and a not in HARD_DENYLIST and a not in out:
            out.append(a)
    return out


def allowed(app: str | None, allowlist: Iterable[str], kind: str) -> bool:
    if not app or app in HARD_DENYLIST:
        return False
    if kind == "ui_event" and app in CONTEXT_APPS:
        return True
    return app in set(allowlist)


def _mask_fields(fields: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    out = dict(fields)
    for k in keys:
        if isinstance(out.get(k), str) and out[k] != SECRET:
            out[k] = mask(out[k])
    return out


def sanitize(msg: Message, allowlist: Iterable[str]) -> Message | None:
    """Return the message to store, or None to drop it."""
    if msg.type in ("transcript", "ui_event", "app_switch"):
        if not allowed(msg.app, allowlist, msg.type):
            return None
    if msg.type == "transcript":
        return Message(msg.type, msg.ts, _mask_fields(msg.fields, ("text",)), msg.v)
    if msg.type == "ui_event":
        fields = dict(msg.fields)
        target = fields.get("target") or {}
        if isinstance(target, dict) and target.get("subrole") == "AXSecureTextField":
            # Second guard: never keep anything typed into a secure field.
            fields.pop("inserted", None)
            fields["value"] = SECRET
        return Message(msg.type, msg.ts, _mask_fields(fields, ("value", "inserted")), msg.v)
    return msg
