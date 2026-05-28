"""Public AgentEvent types — the most-common-superset for coding agents.

Pi backend emits a subset; Codex backend exercises the full surface.
"""

from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Text / reasoning
# ---------------------------------------------------------------------------

@dataclass
class TextDeltaEvent:
    delta: str
    item_id: str | None = None
    type: str = "text_delta"


@dataclass
class ThinkingDeltaEvent:
    delta: str
    type: str = "thinking_delta"


@dataclass
class ReasoningSummaryDeltaEvent:
    delta: str
    type: str = "reasoning_summary_delta"


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@dataclass
class ToolCallEvent:
    tool_call_id: str
    tool_name: str
    arguments: dict
    type: str = "tool_call"


@dataclass
class ToolResultEvent:
    tool_call_id: str
    tool_name: str
    content: str
    is_error: bool = False
    type: str = "tool_result"


# ---------------------------------------------------------------------------
# Codex command execution
# ---------------------------------------------------------------------------

@dataclass
class CommandStartEvent:
    command: str | list[str]
    cwd: str | None = None
    type: str = "command_start"


@dataclass
class CommandEndEvent:
    exit_code: int | None = None
    output: str = ""
    type: str = "command_end"


# ---------------------------------------------------------------------------
# Codex file changes
# ---------------------------------------------------------------------------

@dataclass
class FileChangeEvent:
    path: str
    diff: str | None = None
    status: str = "completed"
    type: str = "file_change"


@dataclass
class TurnDiffEvent:
    unified_diff: str
    type: str = "turn_diff"


# ---------------------------------------------------------------------------
# Billing / lifecycle
# ---------------------------------------------------------------------------

@dataclass
class TokenUsageEvent:
    """Token usage for one LLM inference (incremental, not cumulative).

    Emitted once per LLM call. Multi-step turns (agent uses tools) produce
    multiple events. Sum across the turn if you want a total.
    """
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    type: str = "token_usage"


@dataclass
class TurnStartEvent:
    turn_id: str
    type: str = "turn_start"


@dataclass
class TurnEndEvent:
    turn_id: str = ""
    stop_reason: str = ""
    type: str = "turn_end"


@dataclass
class AgentEndEvent:
    stop_reason: str
    type: str = "agent_end"


@dataclass
class WarningEvent:
    kind: str
    message: str
    type: str = "warning"


@dataclass
class ErrorEvent:
    message: str
    raw: dict | None = None
    type: str = "error"


AgentEvent = (
    TextDeltaEvent
    | ThinkingDeltaEvent
    | ReasoningSummaryDeltaEvent
    | ToolCallEvent
    | ToolResultEvent
    | CommandStartEvent
    | CommandEndEvent
    | FileChangeEvent
    | TurnDiffEvent
    | TokenUsageEvent
    | TurnStartEvent
    | TurnEndEvent
    | AgentEndEvent
    | WarningEvent
    | ErrorEvent
)


__all__ = [
    "AgentEvent",
    "TextDeltaEvent",
    "ThinkingDeltaEvent",
    "ReasoningSummaryDeltaEvent",
    "ToolCallEvent",
    "ToolResultEvent",
    "CommandStartEvent",
    "CommandEndEvent",
    "FileChangeEvent",
    "TurnDiffEvent",
    "TokenUsageEvent",
    "TurnStartEvent",
    "TurnEndEvent",
    "AgentEndEvent",
    "WarningEvent",
    "ErrorEvent",
]
