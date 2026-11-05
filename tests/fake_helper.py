"""Fake capture helper for tests: speaks the real protocol over the real socket.

Replays a JSONL fixture, stamping each message with the current time, and obeys control ops.
Exits when the socket closes (like the real helper).

  python tests/fake_helper.py --socket S --token T --fixture F [--delay 0.01] [--crash-after N]
      [--bad-token] [--no-hello] [--hold]
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import time


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--socket", required=True)
    ap.add_argument("--token", required=True)
    ap.add_argument("--fixture", required=True)
    ap.add_argument("--delay", type=float, default=0.01)
    ap.add_argument("--crash-after", type=int, default=-1)
    ap.add_argument("--bad-token", action="store_true")
    ap.add_argument("--no-hello", action="store_true")
    ap.add_argument("--hold", action="store_true", help="stay connected after the fixture ends")
    ap.add_argument("--log", default="")
    a = ap.parse_args()

    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(a.socket)
    paused = threading.Event()
    stop = threading.Event()
    log = open(a.log, "a") if a.log else None

    def send(obj: dict) -> None:
        s.sendall((json.dumps(obj) + "\n").encode())

    def reader() -> None:
        f = s.makefile("rb")
        for line in f:
            msg = json.loads(line)
            if log:
                log.write(json.dumps(msg) + "\n")
                log.flush()
            op = msg.get("op")
            if op == "pause":
                paused.set()
            elif op == "resume":
                paused.clear()
            elif op == "stop":
                stop.set()
                return
        stop.set()  # socket closed by the plugin

    if not a.no_hello:
        send({"v": 1, "type": "hello", "ts": time.time(), "pid": os.getpid(), "helper_version": "fake-0",
              "token": "wrong" if a.bad_token else a.token,
              "permissions": {"screen_capture": True, "microphone": "authorized",
                              "accessibility": False, "input_monitoring": False}})
    threading.Thread(target=reader, daemon=True).start()

    lines = [json.loads(x) for x in open(a.fixture) if x.strip()]
    sent = 0
    for obj in lines:
        if stop.is_set():
            break
        while paused.is_set() and not stop.is_set():
            time.sleep(0.01)
        obj = dict(obj)
        obj["ts"] = time.time()
        try:
            send(obj)
        except OSError:
            break
        sent += 1
        if sent == a.crash_after:
            os._exit(3)
        time.sleep(a.delay)
    while a.hold and not stop.is_set():
        time.sleep(0.02)
    s.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
