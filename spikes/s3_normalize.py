"""S3: normalize raw UI events into compact steps for the skill compiler (throwaway).

Keeps one app's events (plus Dock launches), merges consecutive typing into one field, turns the
legacy whole-field values into deltas, and drops clicks that only focus the field typed into next.
Usage: python spikes/s3_normalize.py spikes/out/s3_notes.jsonl com.apple.Notes
"""
from __future__ import annotations

import json
import sys


def label(e: dict) -> str:
    for k in ("title", "desc", "placeholder", "text", "label_from_context", "identifier"):
        if e.get(k):
            return f'{e.get("role", "?").removeprefix("AX")} "{e[k]}"'
    return e.get("role", "?").removeprefix("AX")


def main(path: str, app: str) -> None:
    ev = [json.loads(l) for l in open(path)]
    t0 = ev[0]["ts"]
    steps: list[dict] = []
    prev_val: dict[str, str] = {}
    for e in ev:
        if e["type"] == "click" and e.get("app") == "com.apple.dock" and e.get("title"):
            steps.append({"t": round(e["ts"] - t0, 1), "action": "open_app", "target": e["title"]})
            continue
        if e.get("app") != app:
            continue
        if e["type"] == "click":
            if e.get("subrole") == "AXSecureTextField":
                steps.append({"t": round(e["ts"] - t0, 1), "action": "enter_secret", "target": label(e)})
            else:
                steps.append({"t": round(e["ts"] - t0, 1), "action": "click", "target": label(e)})
        elif e["type"] == "typed":
            key = label(e)
            if "inserted" in e:
                ins = e["inserted"]
            else:  # legacy cumulative value: diff against the last value seen for this field
                v = e.get("value") or ""
                old = prev_val.get(key, "")
                ins = v[len(old):] if v.startswith(old) else v
                prev_val[key] = v
            # a click that only focused this field is noise
            if steps and steps[-1]["action"] == "click" and steps[-1]["target"] == key:
                steps.pop()
            if steps and steps[-1]["action"] == "type" and steps[-1]["target"] == key:
                steps[-1]["text"] += ins
            else:
                steps.append({"t": round(e["ts"] - t0, 1), "action": "type", "target": key, "text": ins})
    for i, s in enumerate(steps, 1):
        s["n"] = i
    print(json.dumps({"app": app, "duration_s": round(ev[-1]["ts"] - t0, 1), "steps": steps}, indent=1))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
