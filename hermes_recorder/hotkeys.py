"""Hotkey combos: parsing and display. Stdlib only, shared by the plugin and the helper."""
from __future__ import annotations

# macOS virtual key codes (ANSI layout) and Carbon modifier masks
KEYCODES = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9, "b": 11, "q": 12, "w": 13,
    "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "9": 25, "7": 26,
    "8": 28, "0": 29, "o": 31, "u": 32, "i": 34, "p": 35, "l": 37, "j": 38, "k": 40, "n": 45, "m": 46,
    "space": 49, "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98, "f8": 100, "f9": 101,
    "f10": 109, "f11": 103, "f12": 111,
}
MODIFIERS = {"cmd": 0x0100, "command": 0x0100, "shift": 0x0200, "opt": 0x0800, "option": 0x0800, "alt": 0x0800,
             "ctrl": 0x1000, "control": 0x1000}


def parse_combo(combo: str) -> tuple[int, int]:
    """'ctrl+opt+r' -> (keycode, carbon modifier mask). Requires at least one modifier besides shift."""
    parts = [p.strip().lower() for p in combo.replace("-", "+").split("+") if p.strip()]
    if not parts:
        raise ValueError("empty hotkey")
    key, mods = parts[-1], parts[:-1]
    if key not in KEYCODES:
        raise ValueError(f"unsupported key {key!r}")
    mask = 0
    for m in mods:
        if m not in MODIFIERS:
            raise ValueError(f"unknown modifier {m!r}")
        mask |= MODIFIERS[m]
    if not mask & (MODIFIERS["cmd"] | MODIFIERS["opt"] | MODIFIERS["ctrl"]):
        raise ValueError("use at least one of cmd, opt or ctrl so the hotkey doesn't block normal typing")
    return KEYCODES[key], mask


def pretty(combo: str) -> str:
    """'ctrl+opt+r' -> '⌃⌥R'."""
    sym = {"ctrl": "⌃", "control": "⌃", "opt": "⌥", "option": "⌥", "alt": "⌥", "shift": "⇧", "cmd": "⌘",
           "command": "⌘"}
    parts = [p.strip().lower() for p in combo.replace("-", "+").split("+") if p.strip()]
    return "".join(sym.get(p, "") for p in parts[:-1]) + (parts[-1].upper() if parts else "")
