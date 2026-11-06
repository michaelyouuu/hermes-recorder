"""Everything register(ctx) wires up. Kept separate so tests can call it with a fake ctx."""
from __future__ import annotations

from . import cli, commands, hooks, runtime, schemas, tools

TOOLSET = "recorder"


def register_all(ctx) -> None:
    """Register tools, hooks and commands. Nothing here may start threads, open sockets or touch disk."""
    runtime.bind_context(ctx)
    ctx.register_tool(name="screen_recall", toolset=TOOLSET, schema=schemas.RECALL_SCHEMA, handler=tools.screen_recall)
    ctx.register_tool(name="recorder_status", toolset=TOOLSET, schema=schemas.STATUS_SCHEMA, handler=tools.recorder_status)
    ctx.register_tool(name="recorder_control", toolset=TOOLSET, schema=schemas.CONTROL_SCHEMA, handler=tools.recorder_control)
    ctx.register_tool(name="recorder_save_notes", toolset=TOOLSET, schema=schemas.SAVE_NOTES_SCHEMA,
                      handler=tools.save_notes)
    ctx.register_tool(name="recorder_notes", toolset=TOOLSET, schema=schemas.NOTES_SCHEMA, handler=tools.recorder_notes)
    ctx.register_tool(name="recorder_draft_skill", toolset=TOOLSET, schema=schemas.DRAFT_SCHEMA, handler=tools.draft_skill)
    ctx.register_hook("pre_llm_call", hooks.pre_llm_call)
    ctx.register_hook("on_session_end", hooks.on_session_end)
    ctx.register_hook("pre_gateway_dispatch", hooks.pre_gateway_dispatch)
    ctx.register_command("recorder", commands.recorder, description="Start, stop or check local capture",
                         args_hint="[on|off|pause|resume|status|notes|forget] [apps|minutes]")
    ctx.register_command("teach", commands.teach, description="Draft a skill from the last N minutes",
                         args_hint="[minutes]")
    ctx.register_cli_command("recorder", "Recorder setup and diagnostics", cli.setup_parser, cli.run)
    ctx.register_skill("replay-recorded", runtime.PLUGIN_ROOT / "skills" / "replay-recorded" / "SKILL.md",
                       description="Run a skill recorded with hermes-recorder; follow up on a meeting")
    ctx.on_unload(runtime.shutdown)
