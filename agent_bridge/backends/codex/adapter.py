"""CodexAdapter — drives `codex app-server` over JSON-RPC stdio."""

from __future__ import annotations

import os
from typing import Callable, Iterable, Iterator

from ...core.capabilities import Capabilities
from ...core.config import SAFETY_ALLOW_ALL, SAFETY_READ_ONLY
from ...core.events import (
    AgentEndEvent,
    AgentEvent,
    ToolCallEvent,
    ToolResultEvent,
    TurnEndEvent,
)
from ...core.tools import CustomTool
from ...core.transport import JsonlSubprocessTransport
from ...errors import BridgeError
from .config import CodexConfig, CodexModel, CodexProvider
from .event_mapper import (
    _APPROVAL_METHODS,
    map_codex_notification,
    map_codex_server_request,
)
from .rpc import CodexRpcClient, ServerMessage


CODEX_CAPABILITIES = Capabilities(
    streaming=True,
    reasoning_stream=True,
    custom_tools=True,
    builtin_command_exec=True,
    builtin_file_ops=True,
    turn_diff=True,
    sandbox=True,
    resume=True,
    token_usage_events=True,
    model_switch=True,
    reasoning_effort_switch=True,
)


_PROVIDER_ID = "agent_bridge_provider"
_PROVIDER_DISPLAY_NAME = "agent-bridge custom"

_VALID_THINKING = frozenset({None, "off", "minimal", "low", "medium", "high"})
_VALID_API_FORMAT = frozenset({"response"})


_TURN_OPTION_KEYS = {
    "model": "model",
    "effort": "effort",
    "cwd": "cwd",
    "summary": "summary",
    "output_schema": "outputSchema",
    "service_tier": "serviceTier",
}


def _sandbox_for_safety_mode(mode: str) -> str:
    if mode == SAFETY_ALLOW_ALL:
        return "danger-full-access"
    if mode == SAFETY_READ_ONLY:
        return "read-only"
    raise ValueError(f"Unknown safety_mode: {mode!r}")


def _validate_model(model: CodexModel) -> None:
    if model.api_format not in _VALID_API_FORMAT:
        raise ValueError(
            f"CodexModel.api_format={model.api_format!r} not supported by "
            f"codex 0.133+ (chat completions removed in Feb 2026). "
            f"Only 'response' is valid here. "
            f"Use the Pi backend for 'completion' or 'anthropic'."
        )
    if model.thinking not in _VALID_THINKING:
        raise ValueError(
            f"CodexModel.thinking={model.thinking!r} not supported. "
            f"Codex accepts: None / 'off' / 'minimal' / 'low' / 'medium' / 'high'. "
            f"('xhigh' exists on Pi but not on Codex.)"
        )


def _thinking_to_effort(thinking: str | None) -> str | None:
    if thinking is None or thinking == "off":
        return None
    return thinking


ApprovalHandler = Callable[[ServerMessage], dict]
ToolHandler = Callable[[ServerMessage], dict]


class CodexAdapter:
    capabilities = CODEX_CAPABILITIES

    def __init__(self, config: CodexConfig):
        self.config = config
        self._closed = False
        self._thread_id: str | None = None
        self._approval_handler: ApprovalHandler | None = None
        self._tool_handler: ToolHandler | None = None
        self._messages: list[dict] = []

        if config.model is not None:
            _validate_model(config.model)

        self._custom_tools: dict[str, CustomTool] = {
            t.name: t for t in (config.custom_tools or [])
        }

        cmd = self._build_command(config)
        env = self._build_env(config)
        self._transport = JsonlSubprocessTransport(cmd, cwd=config.cwd, env=env)
        self.rpc = CodexRpcClient(self._transport)

        self._initialize()
        self._start_or_resume_thread()

    # -- bootstrap ---------------------------------------------------------

    def _build_command(self, config: CodexConfig) -> list[str]:
        cmd = list(config.app_server_cmd or ["codex", "app-server"])
        if config.listen and config.listen != "stdio://":
            cmd += ["--listen", config.listen]

        provider = config.provider
        if provider is not None:
            cmd += [
                "-c", f'model_provider="{_PROVIDER_ID}"',
                "-c", f'model_providers.{_PROVIDER_ID}.name="{_PROVIDER_DISPLAY_NAME}"',
                "-c", f'model_providers.{_PROVIDER_ID}.base_url="{provider.base_url}"',
                "-c", f'model_providers.{_PROVIDER_ID}.wire_api="responses"',
                "-c", f'model_providers.{_PROVIDER_ID}.experimental_bearer_token="{provider.api_key}"',
            ]
        return cmd

    def _build_env(self, config: CodexConfig) -> dict:
        env: dict[str, str] = {}
        if config.auth.codex_home:
            env["CODEX_HOME"] = config.auth.codex_home
        if config.provider is None and config.auth.api_key:
            env["CODEX_API_KEY"] = config.auth.api_key
        return env

    def _initialize(self) -> None:
        from ... import __version__ as version

        self.rpc.request("initialize", {
            "clientInfo": {
                "name": "agent-bridge",
                "title": "agent-bridge",
                "version": version,
            },
            "capabilities": {"experimentalApi": True},
        })
        self.rpc.notify("initialized", {})

    def _start_or_resume_thread(self) -> None:
        cfg = self.config
        if cfg.resume_thread_id:
            res = self.rpc.request("thread/resume", {
                "threadId": cfg.resume_thread_id,
            })
        else:
            params: dict = {
                "cwd": os.path.abspath(cfg.cwd),
                "sandbox": _sandbox_for_safety_mode(cfg.safety_mode),
                "approvalPolicy": "never",
                "reasoningSummary": "auto",
            }
            if cfg.model is not None:
                params["model"] = cfg.model.name
                effort = _thinking_to_effort(cfg.model.thinking)
                if effort is not None:
                    params["reasoningEffort"] = effort
            if cfg.base_instructions:
                params["baseInstructions"] = cfg.base_instructions
            if cfg.developer_instructions or cfg.system_prompt:
                params["developerInstructions"] = (
                    cfg.developer_instructions or cfg.system_prompt
                )
            if self._custom_tools:
                params["dynamicTools"] = [
                    {
                        "name": t.name,
                        "description": t.description,
                        "inputSchema": t.parameters,
                    }
                    for t in self._custom_tools.values()
                ]
            res = self.rpc.request("thread/start", params)

        thread = res.get("thread") or {}
        self._thread_id = thread.get("id")
        if not self._thread_id:
            raise BridgeError(f"thread/start did not return an id: {res!r}")

    # -- public API --------------------------------------------------------

    def send_stream(self, message: str, **turn_options) -> Iterator[AgentEvent]:
        params: dict = {
            "threadId": self._thread_id,
            "input": [{"type": "text", "text": message}],
        }

        unknown = set(turn_options) - set(_TURN_OPTION_KEYS)
        if unknown:
            raise TypeError(
                f"CodexAdapter does not recognize turn options: {sorted(unknown)}"
            )
        for snake, camel in _TURN_OPTION_KEYS.items():
            value = turn_options.get(snake)
            if value is not None:
                params[camel] = value

        self.rpc.request("turn/start", params)

        for msg in self.rpc.iter_messages():
            if msg.kind == "request":
                # Server-side requests can produce events as we handle them
                # (ToolCallEvent / ToolResultEvent for dynamic tools).
                for ev in self._handle_server_request(msg):
                    yield ev
                # Mapper is currently a no-op for server requests but kept
                # for symmetry — yield anything it produces.
                for ev in map_codex_server_request(msg):
                    yield ev
                continue

            saw_turn_end: TurnEndEvent | None = None
            for ev in map_codex_notification(msg):
                if isinstance(ev, TurnEndEvent):
                    saw_turn_end = ev
                yield ev

            if msg.method == "turn/completed":
                # Synthesize an AgentEndEvent so the "send is done" sentinel
                # is the same on both backends.
                stop_reason = saw_turn_end.stop_reason if saw_turn_end else "stop"
                yield AgentEndEvent(stop_reason=stop_reason)
                break

    def send(self, message: str, **turn_options) -> list[AgentEvent]:
        return list(self.send_stream(message, **turn_options))

    def abort(self) -> None:
        if self._thread_id:
            self.rpc.request("turn/interrupt", {"threadId": self._thread_id})

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._thread_id:
                self.rpc.notify("thread/unsubscribe", {"threadId": self._thread_id})
        except Exception:
            pass
        self.rpc.close()
        self._transport.close()

    # -- approval / tool wiring -------------------------------------------

    def set_approval_handler(self, handler: ApprovalHandler | None) -> None:
        self._approval_handler = handler

    def set_tool_handler(self, handler: ToolHandler | None) -> None:
        self._tool_handler = handler

    # -- introspection -----------------------------------------------------

    @property
    def thread_id(self) -> str | None:
        return self._thread_id

    @property
    def state(self) -> dict:
        model_name = self.config.model.name if self.config.model else None
        return {
            "thread_id": self._thread_id,
            "model": model_name,
        }

    @property
    def messages(self) -> list[dict]:
        return list(self._messages)

    # -- internals ---------------------------------------------------------

    def _handle_server_request(self, msg: ServerMessage) -> Iterable[AgentEvent]:
        """Respond to a ServerRequest. Yields any events that handling
        produces — currently ToolCallEvent + ToolResultEvent for dynamic
        tool calls. Approval events are produced by map_codex_server_request,
        not here."""
        if msg.method in _APPROVAL_METHODS:
            decision = (
                self._approval_handler(msg)
                if self._approval_handler
                else {"decision": "denied"}
            )
            self.rpc.respond(msg.request_id, decision)
            return

        if msg.method == "item/tool/call":
            tool_name = msg.params.get("tool") or msg.params.get("toolName") or ""
            args_raw = msg.params.get("arguments") or {}
            args = args_raw if isinstance(args_raw, dict) else {"_": args_raw}
            tool_call_id = msg.params.get("id") or str(msg.request_id)

            yield ToolCallEvent(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                arguments=args,
            )

            # Custom override (escape hatch) wins over CustomTool registry.
            if self._tool_handler is not None:
                try:
                    result = self._tool_handler(msg)
                    self.rpc.respond(msg.request_id, result)
                    yield ToolResultEvent(
                        tool_call_id=tool_call_id,
                        tool_name=tool_name,
                        content=_stringify(result),
                        is_error=False,
                    )
                except Exception as exc:
                    self.rpc.respond(msg.request_id, result=_tool_failure(str(exc)))
                    yield ToolResultEvent(
                        tool_call_id=tool_call_id,
                        tool_name=tool_name,
                        content=str(exc),
                        is_error=True,
                    )
                return

            tool = self._custom_tools.get(tool_name)
            if tool is None:
                msg_text = f"No Python handler for tool: {tool_name!r}"
                self.rpc.respond(msg.request_id, result=_tool_failure(msg_text))
                yield ToolResultEvent(
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    content=msg_text,
                    is_error=True,
                )
                return

            try:
                result = tool.fn(**args) if isinstance(args_raw, dict) else tool.fn(args_raw)
            except Exception as exc:
                self.rpc.respond(msg.request_id, result=_tool_failure(str(exc)))
                yield ToolResultEvent(
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    content=str(exc),
                    is_error=True,
                )
                return

            content = _stringify(result)
            self.rpc.respond(
                msg.request_id,
                result={
                    "contentItems": [{"type": "inputText", "text": content}],
                    "success": True,
                },
            )
            yield ToolResultEvent(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                content=content,
                is_error=False,
            )
            return

        # Unknown server-side request — refuse rather than block.
        self.rpc.respond(
            msg.request_id,
            error={"code": -32601, "message": f"method not handled: {msg.method}"},
        )


def _stringify(value) -> str:
    if isinstance(value, str):
        return value
    return str(value)


def _tool_failure(message: str) -> dict:
    return {
        "contentItems": [{"type": "inputText", "text": message}],
        "success": False,
    }
