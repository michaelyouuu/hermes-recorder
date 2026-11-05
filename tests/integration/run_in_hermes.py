"""Run tests/integration inside Hermes's own runtime (its interpreter, bootstrap and dependencies).

  <hermes python> -I tests/integration/run_in_hermes.py [HERMES_AGENT_DIR] [pytest args...]

Hermes's packages come first on sys.path; this repo's .venv is appended only so pytest is importable.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
agent = Path(sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-")
             else os.environ.get("HERMES_AGENT_DIR", Path.home() / ".hermes" / "hermes-agent"))
args = [a for a in sys.argv[1:] if a != str(agent)]
sys.path.insert(0, str(agent))
import hermes_bootstrap  # noqa: E402,F401  (sets up Hermes's dependency paths)

for sp in sorted((ROOT / ".venv" / "lib").glob("python3*/site-packages")):
    sys.path.append(str(sp))
import pytest  # noqa: E402

# Relative path on purpose: pytest treats "[...]" in an argument as a parameter id, and this repo's
# absolute path may contain brackets.
os.chdir(ROOT)
sys.exit(pytest.main(["-o", "addopts=", "-p", "no:cacheprovider", "tests/integration", *args]))
