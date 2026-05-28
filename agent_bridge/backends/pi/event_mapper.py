"""Map Pi bridge frames to public AgentEvent types.

The Pi bridge (server.mjs) does most of the translation already. This mapper
just wraps each frame in the appropriate dataclass. Frames that aren't
events (tool_request, response, ready) return None — adapters handle those
out-of-band.
"""

from ...core.events import (
    AgentEndEvent,
    AgentEvent,
    ErrorEvent,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    TokenUsageEvent,
    ToolCallEvent,
    ToolResultEvent,
    TurnEndEvent,
    TurnStartEvent,
)


def map_pi_event(raw: dict) -> AgentEvent | None:
    t = raw.get("type")

    if t == "text_delta":
        return TextDeltaEvent(delta=raw.get("delta", ""))
    if t == "thinking_delta":
        return ThinkingDeltaEvent(delta=raw.get("delta", ""))
    if t == "turn_start":
        return TurnStartEvent(turn_id=raw.get("turn_id", ""))
    if t == "tool_call":
        return ToolCallEvent(
            tool_call_id=raw["tool_call_id"],
            tool_name=raw["tool_name"],
            arguments=raw.get("arguments", {}),
        )
    if t == "tool_result":
        return ToolResultEvent(
            tool_call_id=raw["tool_call_id"],
            tool_name=raw["tool_name"],
            content=raw.get("content", ""),
            is_error=raw.get("is_error", False),
        )
    if t == "turn_end":
        return TurnEndEvent()
    if t == "token_usage":
        return TokenUsageEvent(
            input_tokens=raw.get("input_tokens", 0) or 0,
            output_tokens=raw.get("output_tokens", 0) or 0,
            cached_input_tokens=raw.get("cached_input_tokens", 0) or 0,
        )
    if t == "agent_end":
        return AgentEndEvent(stop_reason=raw.get("stop_reason", "stop"))
    if t == "error":
        return ErrorEvent(message=raw.get("message", "unknown error"))

    return None
