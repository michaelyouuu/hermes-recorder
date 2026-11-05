"""`hermes recorder setup`: build the helper's runtime under plugin-data.

  runtime/venv/                       helper dependencies (from pyproject.toml), created from Hermes's python
  runtime/Hermes Recorder Helper.app  Info.plist + a copy of that python, ad-hoc signed, so macOS asks for
                                      permissions as "Hermes Recorder Helper" whoever launches it
  runtime/build.json                  what was built, for doctor and for rebuild-on-change
  models/                             Hugging Face cache; the helper itself runs offline

Nothing compiled is shipped in the plugin: the bundle is generated here from the user's own interpreter.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import plistlib
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import runtime

log = logging.getLogger(__name__)

HELPER_BUNDLE_ID = "io.github.michaelyouuu.hermes-recorder.helper"
HELPER_NAME = "Hermes Recorder Helper"
HELPER_EXE = "hermes-recorder-helper"
USAGE = {
    "NSMicrophoneUsageDescription": "Hermes Recorder transcribes your narration locally so Hermes can answer "
                                    "questions about it.",
    "NSAudioCaptureUsageDescription": "Hermes Recorder transcribes audio from apps you choose, locally on this Mac.",
    "NSScreenCaptureUsageDescription": "Hermes Recorder captures audio from apps you choose, locally on this Mac.",
}
BUILD_VERSION = 1

Run = Callable[..., subprocess.CompletedProcess]


@dataclass
class Paths:
    data: Path

    @property
    def runtime(self) -> Path:
        return self.data / "runtime"

    @property
    def venv(self) -> Path:
        return self.runtime / "venv"

    @property
    def app(self) -> Path:
        return self.runtime / f"{HELPER_NAME}.app"

    @property
    def build_json(self) -> Path:
        return self.runtime / "build.json"

    @property
    def models(self) -> Path:
        return self.data / "models"

    @property
    def logs(self) -> Path:
        return self.data / "logs"

    def site_packages(self) -> Path:
        found = sorted((self.venv / "lib").glob("python3*/site-packages"))
        if not found:
            raise FileNotFoundError(f"no site-packages in {self.venv}")
        return found[0]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def python_home(exe: Path) -> Path:
    """The interpreter's prefix (bin/../), which the copied binary needs as PYTHONHOME."""
    return Path(exe).resolve().parent.parent


def host_python() -> Path:
    """The real interpreter running Hermes (resolve venv shims to the base binary)."""
    base = Path(getattr(sys, "_base_executable", sys.executable) or sys.executable).resolve()
    return base


def info_plist(bundle_id: str, name: str, exe: str, usage: dict[str, str], env: dict[str, str]) -> bytes:
    return plistlib.dumps({
        "CFBundleIdentifier": bundle_id, "CFBundleName": name, "CFBundleDisplayName": name,
        "CFBundleExecutable": exe, "CFBundlePackageType": "APPL", "CFBundleVersion": str(BUILD_VERSION),
        "CFBundleShortVersionString": "0.1.0", "LSMinimumSystemVersion": "14.0", "LSUIElement": True,
        **usage, "LSEnvironment": env,
    })


def build_app(dest: Path, python: Path, bundle_id: str, name: str, exe: str,
              usage: dict[str, str], run: Run = subprocess.run) -> Path:
    """Create ``dest`` (an .app) around a copy of ``python`` and ad-hoc sign it."""
    tmp = dest.with_name(dest.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    (tmp / "Contents" / "MacOS").mkdir(parents=True)
    shutil.copy2(python, tmp / "Contents" / "MacOS" / exe)
    (tmp / "Contents" / "Info.plist").write_bytes(
        info_plist(bundle_id, name, exe, usage, {"PYTHONHOME": str(python_home(python))}))
    run(["/usr/bin/codesign", "--force", "--sign", "-", "--identifier", bundle_id, str(tmp)],
        check=True, capture_output=True, text=True)
    shutil.rmtree(dest, ignore_errors=True)
    os.replace(tmp, dest)
    return dest


def requirements() -> list[str]:
    """The helper's dependencies, from the plugin's pyproject.toml."""
    import tomllib
    data = tomllib.loads((runtime.PLUGIN_ROOT / "pyproject.toml").read_text())
    return list(data["project"]["dependencies"])


def read_build(paths: Paths) -> dict | None:
    try:
        return json.loads(paths.build_json.read_text())
    except (OSError, ValueError):
        return None


def build_is_current(paths: Paths, python: Path) -> tuple[bool, str]:
    b = read_build(paths)
    if b is None:
        return False, "not built"
    if b.get("build_version") != BUILD_VERSION:
        return False, "built by an older version"
    if not (paths.app / "Contents" / "MacOS" / HELPER_EXE).exists():
        return False, "helper app missing"
    if b.get("python_sha256") != sha256(python):
        return False, "Hermes's Python changed since the helper was built"
    if b.get("requirements") != requirements():
        return False, "dependencies changed"
    return True, "up to date"


def platform_problems() -> list[str]:
    out = []
    if sys.platform != "darwin":
        out.append("hermes-recorder captures on macOS only.")
        return out
    if platform.machine() != "arm64":
        out.append("hermes-recorder needs Apple Silicon (faster-whisper has no Intel Mac wheels).")
    major = int(platform.mac_ver()[0].split(".")[0] or 0)
    if major < 14:
        out.append(f"macOS 14 or newer is required (this Mac runs {platform.mac_ver()[0]}).")
    return out


def build_runtime(paths: Paths, python: Path | None = None, run: Run = subprocess.run,
                  say: Callable[[str], None] = print, force: bool = False) -> dict:
    """Create the venv, install deps, build and sign the app. Idempotent."""
    python = Path(python or host_python())
    ok, why = build_is_current(paths, python)
    if ok and not force:
        say("Helper runtime is up to date.")
        return read_build(paths) or {}
    prior = read_build(paths)
    say(f"Building the helper runtime ({why})…")
    paths.runtime.mkdir(parents=True, exist_ok=True)
    if not (paths.venv / "bin" / "python").exists() or force:
        say("  creating venv")
        run([str(python), "-m", "venv", "--clear", str(paths.venv)], check=True, capture_output=True, text=True)
    reqs = requirements()
    say(f"  installing {len(reqs)} packages (faster-whisper, pyobjc…), this can take a minute")
    run([str(paths.venv / "bin" / "python"), "-m", "pip", "install", "--disable-pip-version-check", "-q", *reqs],
        check=True, capture_output=True, text=True)
    say("  building and signing the helper app")
    build_app(paths.app, python, HELPER_BUNDLE_ID, HELPER_NAME, HELPER_EXE, USAGE, run=run)
    build = {"build_version": BUILD_VERSION, "python": str(python), "python_sha256": sha256(python),
             "python_home": str(python_home(python)), "requirements": reqs, "app": str(paths.app),
             "bundle_id": HELPER_BUNDLE_ID, "built_at": time.time()}
    paths.build_json.write_text(json.dumps(build, indent=2))
    if prior and prior.get("python_sha256") != build["python_sha256"]:
        say("  NOTE: Hermes's Python changed, so macOS will ask for the helper's permissions again.")
    return build


def download_model(paths: Paths, model: str, run: Run = subprocess.run, say: Callable[[str], None] = print) -> None:
    say(f"Downloading the '{model}' transcription model from Hugging Face (once, ~150-900 MB)…")
    paths.models.mkdir(parents=True, exist_ok=True)
    code = ("import sys; from faster_whisper import WhisperModel; "
            "WhisperModel(sys.argv[1], device='cpu', compute_type='int8')")
    env = {**os.environ, "HF_HOME": str(paths.models)}
    env.pop("HF_HUB_OFFLINE", None)
    run([str(paths.venv / "bin" / "python"), "-c", code, model], check=True, capture_output=True, text=True, env=env)


def helper_env(paths: Paths) -> dict[str, str]:
    """Environment for the helper app: its own deps, the plugin root, offline model cache."""
    b = read_build(paths) or {}
    return {"PYTHONHOME": b.get("python_home", ""),
            "PYTHONPATH": f"{paths.site_packages()}{os.pathsep}{runtime.PLUGIN_ROOT}",
            "HF_HOME": str(paths.models), "HF_HUB_OFFLINE": "1", "PYTHONDONTWRITEBYTECODE": "1"}


def open_argv(app: Path, env: dict[str, str], args: list[str], wait: bool = False) -> list[str]:
    argv = ["/usr/bin/open", "-n", "-g"]
    if wait:
        argv.append("-W")
    for k, v in env.items():
        argv += ["--env", f"{k}={v}"]
    return argv + ["-a", str(app), "--args", *args]


def run_preflight(paths: Paths, request: bool, events: bool, run: Run = subprocess.run,
                  timeout: float = 90) -> dict:
    """Run the helper app's preflight mode (it writes JSON to a file: `open` gives no stdout)."""
    out = paths.runtime / f"preflight-{os.getpid()}.json"
    out.unlink(missing_ok=True)
    args = [str(runtime.PLUGIN_ROOT / "helper" / "main.py"), "--preflight", str(out)]
    if request:
        args.append("--request")
    if events:
        args.append("--events")
    run(open_argv(paths.app, helper_env(paths), args, wait=True), check=False, capture_output=True,
        text=True, timeout=timeout)
    try:
        return json.loads(out.read_text())
    except (OSError, ValueError):
        return {"error": "the helper app didn't report back (it may have been blocked from launching)"}
    finally:
        out.unlink(missing_ok=True)
