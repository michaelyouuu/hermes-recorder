"""Everything register(ctx) wires up. Kept separate so tests can call it with a fake ctx."""
from __future__ import annotations


def register_all(ctx) -> None:
    """Register tools, hooks and commands. Nothing here may start threads, open sockets or touch disk."""
    return None
