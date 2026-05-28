"""Capability flags advertised by each backend adapter."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Capabilities:
    streaming: bool = True
    reasoning_stream: bool = False

    custom_tools: bool = False
    builtin_command_exec: bool = False
    builtin_file_ops: bool = False

    turn_diff: bool = False

    sandbox: bool = False

    resume: bool = False

    token_usage_events: bool = False
    model_switch: bool = False
    reasoning_effort_switch: bool = False
