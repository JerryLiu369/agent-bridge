"""Map Codex app-server v2 messages to public AgentEvent types.

Returns an iterable of events for each frame so a single Codex item can
fan out into multiple public events (e.g. one `fileChange` item with N
changes → N `FileChangeEvent`s). Most frames produce 0 or 1 event.

Field names here are pinned to live-captured wire shapes from codex-cli
0.133.0. Treat fields as best-effort: pull what we know, ignore the rest.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Iterable

from ...core.events import (
    AgentEvent,
    CommandEndEvent,
    CommandStartEvent,
    ErrorEvent,
    FileChangeEvent,
    ReasoningSummaryDeltaEvent,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    TokenUsageEvent,
    TurnDiffEvent,
    TurnEndEvent,
    TurnStartEvent,
    WarningEvent,
)

if TYPE_CHECKING:
    from .rpc import ServerMessage


# Methods that indicate a server-side approval request. The adapter
# default-denies these; we don't surface them as public events.
_APPROVAL_METHODS = {
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
    "item/permissions/requestApproval",
    "item/tool/requestUserInput",
    "applyPatchApproval",
    "execCommandApproval",
    "mcpServer/elicitation/request",
}


def map_codex_notification(msg: "ServerMessage") -> Iterable[AgentEvent]:
    """Translate one server notification into 0 or more public events."""
    method = msg.method
    params = msg.params

    # ---- text / reasoning ------------------------------------------------

    if method == "item/agentMessage/delta":
        return [TextDeltaEvent(
            delta=params.get("delta", ""),
            item_id=params.get("itemId"),
        )]
    if method == "item/reasoning/textDelta":
        return [ThinkingDeltaEvent(delta=params.get("delta", ""))]
    if method in (
        "item/reasoning/summaryTextDelta",
        "item/reasoning/summaryPartAdded",
    ):
        return [ReasoningSummaryDeltaEvent(delta=params.get("delta", ""))]

    # ---- turn lifecycle --------------------------------------------------

    if method == "turn/started":
        turn = params.get("turn") or {}
        return [TurnStartEvent(turn_id=turn.get("id", ""))]
    if method == "turn/completed":
        turn = params.get("turn") or {}
        return [TurnEndEvent(
            turn_id=turn.get("id", ""),
            stop_reason=turn.get("stopReason") or turn.get("status") or "stop",
        )]

    # ---- item lifecycle (commandExecution, fileChange) -------------------
    #
    # Real wire shapes (live-captured, codex 0.133.0):
    #   item/started   { item: { type: "commandExecution", command, cwd, ... } }
    #   item/completed { item: { type: "commandExecution", aggregatedOutput,
    #                            exitCode, ... } }
    #   item/started   { item: { type: "fileChange", changes: [
    #                              { path, kind: {type: "add"|"update"|...},
    #                                diff: "..." }, ... ] } }
    #   item/completed { item: { type: "fileChange", changes: [...] } }
    #
    # Codex emits one fileChange item per apply_patch call; we fan its
    # `changes` array out into one FileChangeEvent per file.

    if method == "item/started":
        item = params.get("item") or {}
        item_type = item.get("type")
        if item_type == "commandExecution":
            return [CommandStartEvent(
                command=item.get("command") or item.get("argv") or "",
                cwd=item.get("cwd"),
            )]
        if item_type == "fileChange":
            return [
                FileChangeEvent(
                    path=ch.get("path", ""),
                    diff=ch.get("diff"),
                    status="started",
                )
                for ch in (item.get("changes") or [])
            ]
        return []  # other item starts (agentMessage/userMessage/reasoning) are notifications-only

    if method == "item/completed":
        item = params.get("item") or {}
        item_type = item.get("type")
        if item_type == "commandExecution":
            return [CommandEndEvent(
                exit_code=item.get("exitCode"),
                output=item.get("aggregatedOutput") or "",
            )]
        if item_type == "fileChange":
            return [
                FileChangeEvent(
                    path=ch.get("path", ""),
                    diff=ch.get("diff"),
                    status="completed",
                )
                for ch in (item.get("changes") or [])
            ]
        return []

    # process/exited can also produce a command end (rare, kept for safety).
    if method == "process/exited":
        return [CommandEndEvent(exit_code=params.get("exitCode"))]

    # ---- turn-level diff -------------------------------------------------
    #
    # Real wire shape: { diff: "<unified diff>", threadId, turnId }.
    # (Field is `diff`, NOT `unifiedDiff` as I'd guessed earlier.)

    if method == "turn/diff/updated":
        return [TurnDiffEvent(
            unified_diff=params.get("diff") or params.get("unifiedDiff") or "",
        )]

    # ---- token usage -----------------------------------------------------

    if method == "thread/tokenUsage/updated":
        # tokenUsage.last carries the incremental usage for the most recent
        # LLM inference; tokenUsage.total is cumulative. We expose `last`.
        usage = (params.get("tokenUsage") or {}).get("last") or {}
        return [TokenUsageEvent(
            input_tokens=usage.get("inputTokens", 0) or 0,
            output_tokens=usage.get("outputTokens", 0) or 0,
            cached_input_tokens=usage.get("cachedInputTokens", 0) or 0,
        )]

    # ---- warning / error -------------------------------------------------

    if method in ("warning", "guardianWarning", "deprecationNotice", "configWarning"):
        return [WarningEvent(
            kind={
                "warning": "general",
                "guardianWarning": "guardian",
                "deprecationNotice": "deprecation",
                "configWarning": "config",
            }[method],
            message=params.get("message", ""),
        )]
    if method == "error":
        return [ErrorEvent(message=params.get("message", "unknown error"))]

    return []


def map_codex_server_request(msg: "ServerMessage") -> Iterable[AgentEvent]:
    """Translate a server-side request into public events.

    Currently nothing to emit:
      - approval requests are default-denied silently by the adapter
      - `item/tool/call` events are emitted directly from the adapter's
        dispatch path (so tool_call/tool_result fire around the actual fn)

    Kept for symmetry with `map_codex_notification`.
    """
    return []
