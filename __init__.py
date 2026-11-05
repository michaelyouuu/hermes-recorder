"""Hermes Recorder plugin entry point. register(ctx) only registers; it has no side effects."""
from __future__ import annotations


def register(ctx):
    from .hermes_recorder.registration import register_all

    register_all(ctx)
