"""Owns the capture helper's lifecycle and the Unix socket it streams over.

The real helper is a generated ``Hermes Recorder Helper.app`` launched through LaunchServices (M2), so
there are no stdin/stdout pipes: the socket is the only channel. The helper's first line must be a
``hello`` carrying the per-session token; anything else closes the connection.
"""
from __future__ import annotations

import logging
import os
import secrets
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Protocol

from . import privacy
from . import protocol as proto
from .buffer import RingBuffer

log = logging.getLogger(__name__)

MAX_SUN_PATH = 100  # macOS limit is 104 bytes including NUL; keep a margin
MAX_RESTARTS = 3


class LaunchError(RuntimeError):
    """The helper could not be started; the message says how to fix it."""


class Launcher(Protocol):
    def launch(self, socket_path: str, token: str, opts: dict[str, Any]) -> None: ...
    def terminate(self, pid: int | None = None) -> None: ...


def pid_path(pid: int) -> str | None:
    """Executable path of ``pid`` (macOS libproc), or None."""
    try:
        import ctypes
        lib = ctypes.CDLL("/usr/lib/libproc.dylib")
        buf = ctypes.create_string_buffer(4096)
        n = lib.proc_pidpath(int(pid), buf, 4096)
        return buf.value[:n].decode() if n > 0 else None
    except Exception:
        return None


class AppLauncher:
    """Launches the generated ``Hermes Recorder Helper.app`` through LaunchServices (``open``)."""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)

    def _paths(self):
        from .setup import Paths
        return Paths(self.data_dir)

    def launch(self, socket_path: str, token: str, opts: dict[str, Any]) -> None:
        from . import setup
        paths = self._paths()
        ok, why = setup.build_is_current(paths, setup.host_python())
        if not ok:
            raise LaunchError(f"The capture helper isn't ready ({why}). Run `hermes recorder setup` in a terminal.")
        paths.logs.mkdir(parents=True, exist_ok=True)
        args = [str(Path(__file__).resolve().parent.parent / "helper" / "main.py"),
                "--socket", socket_path, "--token", token, "--apps", ",".join(opts.get("apps") or []),
                "--model", str(opts.get("whisper_model") or "base.en"),
                "--mic", "1" if opts.get("capture_mic", True) else "0",
                "--log", str(paths.logs / "helper.log")]
        try:
            p = subprocess.run(setup.open_argv(paths.app, setup.helper_env(paths), args),
                               capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise LaunchError(f"could not launch the helper app: {e}") from e
        if p.returncode != 0:
            raise LaunchError(f"macOS refused to launch the helper app: {(p.stderr or p.stdout).strip()[:300]}")

    def terminate(self, pid: int | None = None) -> None:
        """SIGTERM the helper, but only if ``pid`` really is our helper's executable (pid reuse guard)."""
        if not pid:
            return
        from .setup import HELPER_EXE
        exe = str(self._paths().app / "Contents" / "MacOS" / HELPER_EXE)
        path = pid_path(pid)
        if path and os.path.realpath(path) == os.path.realpath(exe):
            try:
                os.kill(pid, 15)
            except OSError:
                pass


class SubprocessLauncher:
    """Starts a helper as a plain child process (tests and the fake helper)."""

    def __init__(self, argv_factory: Callable[[str, str, dict[str, Any]], list[str]]):
        self.argv_factory = argv_factory
        self.proc: subprocess.Popen | None = None

    def launch(self, socket_path: str, token: str, opts: dict[str, Any]) -> None:
        argv = self.argv_factory(socket_path, token, opts)
        try:
            self.proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL, close_fds=True)
        except OSError as e:
            raise LaunchError(f"could not start helper: {e}") from e

    def terminate(self, pid: int | None = None) -> None:
        p, self.proc = self.proc, None
        if p is None:
            return
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=2)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(timeout=2)


def choose_socket_path(data_dir: Path) -> tuple[str, str | None]:
    """Return (socket path, temp dir to remove later or None)."""
    path = str(Path(data_dir) / "helper.sock")
    if len(path.encode()) <= MAX_SUN_PATH:
        return path, None
    d = tempfile.mkdtemp(prefix="hrec-")
    os.chmod(d, 0o700)
    return os.path.join(d, "h.sock"), d


class HelperManager:
    def __init__(self, buffer: RingBuffer, data_dir: Path, launcher: Launcher,
                 hello_timeout: float = 15.0, restart_delays: tuple[float, ...] = (1.0, 2.0, 4.0)):
        self.buffer = buffer
        self.data_dir = Path(data_dir)
        self.launcher = launcher
        self.hello_timeout = hello_timeout
        self.restart_delays = restart_delays[:MAX_RESTARTS]
        self._lock = threading.RLock()       # lifecycle (start/stop/restart); may be held while waiting for hello
        self._conn_lock = threading.Lock()   # current connection only; never held while waiting
        self._state = "stopped"  # stopped | starting | running | paused | error
        self._error: str | None = None
        self._apps: list[str] = []
        self._opts: dict[str, Any] = {}
        self._listener: socket.socket | None = None
        self._conn: socket.socket | None = None
        self._sock_path: str | None = None
        self._tmp_dir: str | None = None
        self._token = ""
        self._hello: proto.Message | None = None
        self._hello_event = threading.Event()
        self._conn_closed = threading.Event()  # set when the current helper connection ends
        self._threads: list[threading.Thread] = []
        self._stopping = threading.Event()
        self._restarts = 0
        self.counters = {"lines": 0, "stored": 0, "dropped_privacy": 0, "bad_lines": 0, "rejected_conns": 0}
        self.started_at: float | None = None
        self.last_line_at: float | None = None

    # ------------------------------------------------------------------ public API
    @property
    def state(self) -> str:
        return self._state

    @property
    def apps(self) -> list[str]:
        return list(self._apps)

    @property
    def live(self) -> bool:
        return self._state == "running"

    def start(self, apps: list[str], opts: dict[str, Any] | None = None) -> None:
        apps = privacy.clean_apps(apps)
        if not apps:
            raise LaunchError("No apps to capture. Pass apps (e.g. com.google.Chrome) or set the "
                              "'apps' setting for hermes-recorder.")
        with self._lock:
            if self._state in ("running", "paused", "starting"):
                self._apps = apps
                self._send(proto.control("set_apps", time.time(), apps))
                return
            self._apps, self._opts = apps, dict(opts or {})
            self._stopping.clear()
            self._restarts = 0
            self._error = None
            self._state = "starting"
            try:
                self._open_listener()
                self._launch_and_wait()
            except Exception as e:
                self._error = str(e)
                self._teardown()
                self._state = "error"
                raise
            self._state = "running"
            self.started_at = time.time()

    def stop(self, grace: float = 1.0) -> None:
        with self._lock:
            if self._state == "stopped" and self._listener is None:
                return
            self._stopping.set()
            try:
                had_conn = self._conn is not None
                self._send(proto.control("stop", time.time()))
                if had_conn:
                    # let the helper shut its capture streams down and close the socket itself
                    self._conn_closed.wait(grace)
            except Exception:
                pass
            self._teardown()
            self._state = "stopped"

    def pause(self) -> None:
        with self._lock:
            if self._state == "running":
                self._send(proto.control("pause", time.time()))
                self._state = "paused"

    def resume(self) -> None:
        with self._lock:
            if self._state == "paused":
                self._send(proto.control("resume", time.time()))
                self._state = "running"

    def set_apps(self, apps: list[str]) -> None:
        apps = privacy.clean_apps(apps)
        if not apps:
            raise LaunchError("set_apps needs at least one allowed app")
        with self._lock:
            self._apps = apps
            if self._state in ("running", "paused"):
                self._send(proto.control("set_apps", time.time(), apps))

    def status(self) -> dict[str, Any]:
        hello = self._hello.fields if self._hello else {}
        return {
            "state": self._state, "apps": self.apps, "error": self._error,
            "helper": {"pid": hello.get("pid"), "version": hello.get("helper_version"),
                       "permissions": hello.get("permissions"), "restarts": self._restarts,
                       "last_line_age_s": round(time.time() - self.last_line_at, 1) if self.last_line_at else None},
            "counters": dict(self.counters), "protocol_version": proto.PROTOCOL_VERSION,
        }

    # ------------------------------------------------------------------ internals
    def _open_listener(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        path, tmp = choose_socket_path(self.data_dir)
        if os.path.exists(path):
            os.unlink(path)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old = os.umask(0o177)
        try:
            srv.bind(path)
        finally:
            os.umask(old)
        os.chmod(path, 0o600)
        srv.listen(2)
        srv.settimeout(0.5)
        self._listener, self._sock_path, self._tmp_dir = srv, path, tmp
        t = threading.Thread(target=self._accept_loop, name="hermes-recorder-accept", daemon=True)
        t.start()
        self._threads.append(t)

    def _launch_and_wait(self) -> None:
        self._token = secrets.token_hex(16)
        self._hello, self._hello_event = None, threading.Event()
        self.launcher.launch(self._sock_path, self._token, {**self._opts, "apps": self._apps})
        if not self._hello_event.wait(self.hello_timeout):
            self.launcher.terminate(None)
            raise LaunchError(f"The capture helper didn't connect within {self.hello_timeout:.0f} s. "
                              "Run `hermes recorder doctor` to check permissions.")

    def _accept_loop(self) -> None:
        while not self._stopping.is_set():
            srv = self._listener
            if srv is None:
                return
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), name="hermes-recorder-reader", daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        conn.settimeout(self.hello_timeout)
        f = conn.makefile("rb")
        try:
            first = f.readline(proto.MAX_LINE_BYTES + 1)
            msg = proto.decode(first)
            if msg.type != "hello" or not secrets.compare_digest(str(msg.get("token", "")), self._token):
                raise proto.ProtocolError("first message must be hello with the session token")
        except Exception as e:
            self.counters["rejected_conns"] += 1
            log.warning("hermes-recorder: rejected helper connection: %s", e)
            conn.close()
            return
        with self._conn_lock:
            old, self._conn, self._hello = self._conn, conn, msg
            self._conn_closed.clear()
        if old is not None:
            try:
                old.close()
            except OSError:
                pass
        self._error = None
        self._hello_event.set()
        conn.settimeout(None)
        try:
            for line in f:
                self._handle_line(line)
        except OSError:
            pass
        finally:
            with self._conn_lock:
                was_current = self._conn is conn
                if was_current:
                    self._conn = None
                    self._conn_closed.set()
            conn.close()
            if was_current and not self._stopping.is_set():
                self._on_helper_lost()

    def _handle_line(self, line: bytes) -> None:
        self.counters["lines"] += 1
        self.last_line_at = time.time()
        try:
            msg = proto.decode(line)
        except proto.ProtocolError:
            self.counters["bad_lines"] += 1
            return
        if msg.type == "error":
            self._error = str(msg.get("message"))[:500]
            return
        if msg.type in ("status", "hello", "control"):
            return
        clean = privacy.sanitize(msg, self._apps)
        if clean is None:
            self.counters["dropped_privacy"] += 1
            return
        if self.buffer.add_message(clean) is not None:
            self.counters["stored"] += 1

    def _on_helper_lost(self) -> None:
        def restart() -> None:
            for delay in self.restart_delays[self._restarts:]:
                if self._stopping.wait(delay):
                    return
                with self._lock:
                    if self._stopping.is_set():
                        return
                    self._restarts += 1
                    try:
                        self.launcher.terminate(self._helper_pid())
                        self._launch_and_wait()
                        if self._state == "paused":
                            self._send(proto.control("pause", time.time()))
                        log.info("hermes-recorder: helper restarted (%d)", self._restarts)
                        return
                    except Exception as e:
                        self._error = str(e)
            with self._lock:
                if not self._stopping.is_set():
                    self._error = (self._error or "helper exited") + f" (gave up after {self._restarts} restarts)"
                    self._teardown()
                    self._state = "error"

        with self._lock:
            if self._state in ("running", "paused"):
                self._error = "helper disconnected; restarting"
        threading.Thread(target=restart, name="hermes-recorder-restart", daemon=True).start()

    def _helper_pid(self) -> int | None:
        pid = self._hello.get("pid") if self._hello else None
        return pid if isinstance(pid, int) and pid > 1 and pid != os.getpid() else None

    def _send(self, msg: proto.Message) -> None:
        with self._conn_lock:
            conn = self._conn
        if conn is None:
            return
        try:
            conn.sendall(proto.encode(msg))
        except OSError as e:
            log.info("hermes-recorder: send failed: %s", e)

    def _teardown(self) -> None:
        self._stopping.set()
        with self._conn_lock:
            conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            conn.close()
        srv, self._listener = self._listener, None
        if srv is not None:
            srv.close()
        self.launcher.terminate(self._helper_pid())
        current = threading.current_thread()
        for t in self._threads:
            if t is not current:
                t.join(timeout=2)
        self._threads = []
        if self._sock_path and os.path.exists(self._sock_path):
            try:
                os.unlink(self._sock_path)
            except OSError:
                pass
        if self._tmp_dir:
            try:
                os.rmdir(self._tmp_dir)
            except OSError:
                pass
        self._sock_path = self._tmp_dir = None

