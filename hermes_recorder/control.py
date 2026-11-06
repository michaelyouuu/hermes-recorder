"""Owner-only control socket so `hermes recorder on|off|toggle` (and a global hotkey) can reach the gateway.

Started lazily on the plugin's first activity in a process, never from register(). The socket is 0600 and
every request must carry the token from `control.token` (also 0600), so only the same macOS user can use it.
One JSON request per connection: {"op": "...", "token": "...", "apps": [...], "args": {...}}.
The recorder panel (in the hotkey agent) also uses it: ``summon``, ``feed``, ``ask``, ``pause``, ``resume``.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import socket
import threading
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

MAX_REQUEST = 16 * 1024
MAX_REPLY = 1024 * 1024  # a panel feed carries up to FEED_MAX transcript lines
OPS = ("toggle", "on", "off", "status", "summon", "feed", "ask", "pause", "resume", "notes", "diag")


def paths(data_dir: Path) -> tuple[Path, Path]:
    return Path(data_dir) / "control.sock", Path(data_dir) / "control.token"


class ControlServer:
    def __init__(self, data_dir: Path, handle: Callable[[str, list[str] | None], dict[str, Any]]):
        self.data_dir = Path(data_dir)
        self.handle = handle
        self.sock_path, self.token_path = paths(self.data_dir)
        self._srv: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._token = ""

    @property
    def running(self) -> bool:
        return self._srv is not None

    def start(self) -> None:
        if self._srv is not None:
            return
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if len(str(self.sock_path).encode()) > 100:
            raise OSError("control socket path too long")
        if self.sock_path.exists():
            self.sock_path.unlink()
        self._token = secrets.token_hex(16)
        old = os.umask(0o177)
        try:
            srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            srv.bind(str(self.sock_path))
            tmp = self.token_path.with_suffix(".tmp")
            tmp.write_text(self._token)
            os.replace(tmp, self.token_path)
        finally:
            os.umask(old)
        os.chmod(self.sock_path, 0o600)
        os.chmod(self.token_path, 0o600)
        srv.listen(4)
        srv.settimeout(0.5)
        self._srv = srv
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="hermes-recorder-control", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        srv, self._srv = self._srv, None
        if srv is not None:
            srv.close()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        for p in (self.sock_path, self.token_path):
            try:
                p.unlink()
            except OSError:
                pass

    def _loop(self) -> None:
        while not self._stop.is_set():
            srv = self._srv
            if srv is None:
                return
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), name="hermes-recorder-control-req",
                             daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        with conn:
            conn.settimeout(5)
            try:
                raw = conn.makefile("rb").readline(MAX_REQUEST)
                req = json.loads(raw)
                if not isinstance(req, dict) or not secrets.compare_digest(str(req.get("token", "")), self._token):
                    reply: dict[str, Any] = {"error": "unauthorized"}
                elif req.get("op") not in OPS:
                    reply = {"error": f"unknown op {req.get('op')!r}"}
                else:
                    apps = req.get("apps")
                    apps = [str(a) for a in apps] if isinstance(apps, list) else None
                    fb = req.get("fallback_apps")
                    kw: dict[str, Any] = {}
                    if isinstance(fb, list) and fb:
                        kw["fallback_apps"] = [str(a) for a in fb]
                    if isinstance(req.get("args"), dict) and req["args"]:
                        kw["args"] = req["args"]
                    reply = self.handle(req["op"], apps, **kw)
            except Exception as e:  # never let a bad request hurt the gateway
                reply = {"error": f"{type(e).__name__}: {e}"}
            try:
                conn.sendall((json.dumps(reply) + "\n").encode())
            except OSError:
                pass


def request(data_dir: Path, op: str, apps: list[str] | None = None, timeout: float = 30,
            fallback_apps: list[str] | None = None, args: dict[str, Any] | None = None) -> dict[str, Any]:
    """Client side (CLI, hotkey, panel). Raises ConnectionError when no gateway is listening."""
    sock_path, token_path = paths(data_dir)
    try:
        token = token_path.read_text().strip()
    except OSError as e:
        raise ConnectionError("no control token") from e
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(str(sock_path))
    except OSError as e:
        s.close()
        raise ConnectionError(str(e)) from e
    with s:
        body: dict[str, Any] = {"op": op, "token": token}
        if apps:
            body["apps"] = apps
        if fallback_apps:
            body["fallback_apps"] = fallback_apps
        if args:
            body["args"] = args
        s.sendall((json.dumps(body) + "\n").encode())
        return json.loads(s.makefile("rb").readline(MAX_REPLY) or b"{}")
