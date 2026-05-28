"""PiAdapter — drives the Node.js Pi bridge subprocess."""

import os
import subprocess
from pathlib import Path
from typing import Iterator

from ...core.capabilities import Capabilities
from ...core.config import SAFETY_ALLOW_ALL, SAFETY_READ_ONLY
from ...core.events import AgentEndEvent, AgentEvent
from ...core.transport import JsonlSubprocessTransport
from ...core.tools import CustomTool
from ...errors import BridgeError
from .config import PiConfig, PiModel, PiProvider
from .event_mapper import map_pi_event


_BRIDGE_DIR = Path(__file__).parent
_BRIDGE_SERVER = _BRIDGE_DIR / "server.mjs"

_PI_AGENT_PACKAGE = "@earendil-works/pi-coding-agent"


PI_CAPABILITIES = Capabilities(
    streaming=True,
    reasoning_stream=True,
    custom_tools=True,
    builtin_command_exec=False,
    builtin_file_ops=False,
    turn_diff=False,
    sandbox=False,
    resume=False,
    token_usage_events=True,
    model_switch=True,
    reasoning_effort_switch=False,
)


def _tools_for_safety_mode(mode: str) -> list[str] | None:
    """Translate safety_mode → Pi server.mjs tools allow-list.

    `None`  → bridge applies its built-in default (read/bash/edit/write).
    `[...]` → exact allow-list.
    """
    if mode == SAFETY_ALLOW_ALL:
        return None
    if mode == SAFETY_READ_ONLY:
        return ["read"]
    raise ValueError(f"Unknown safety_mode: {mode!r}")


def _discover_pi_agent_base() -> str:
    env_base = os.environ.get("PI_AGENT_BASE")
    if env_base:
        return env_base
    try:
        npm_root = subprocess.check_output(
            ["npm", "root", "-g"],
            text=True,
        ).strip()
    except Exception:
        return ""
    return str(Path(npm_root) / _PI_AGENT_PACKAGE)


class PiAdapter:
    capabilities = PI_CAPABILITIES

    def __init__(self, config: PiConfig):
        self.config = config
        self._custom_tools: list[CustomTool] = list(config.custom_tools or [])
        self._closed = False

        bridge_script = config.bridge_path or str(_BRIDGE_SERVER)
        env = {"PI_AGENT_BASE": _discover_pi_agent_base()}

        self._transport = JsonlSubprocessTransport(
            ["node", bridge_script],
            cwd=config.cwd,
            env=env,
        )

        self._init_bridge()

    # -- bootstrap ----------------------------------------------------------

    def _init_bridge(self) -> None:
        cfg = self.config
        provider: PiProvider = cfg.provider  # type: ignore[assignment]
        model: PiModel = cfg.model  # type: ignore[assignment]

        self._transport.write({
            "type": "init",
            "provider": {
                "base_url": provider.base_url,
                "api_key": provider.api_key,
            },
            "model": {
                "name": model.name,
                "api_format": model.api_format,
                "thinking": model.thinking,
            },
            "cwd": os.path.abspath(cfg.cwd),
            "system_prompt": cfg.system_prompt,
            "tools": _tools_for_safety_mode(cfg.safety_mode),
            "custom_tools": [
                {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                    "prompt_snippet": t.prompt_snippet,
                    "prompt_guidelines": t.prompt_guidelines,
                }
                for t in self._custom_tools
            ],
            "persist": cfg.persist,
        })

        ready = self._transport.read()
        if ready is None:
            raise BridgeError(
                f"Bridge process exited before ready. Stderr:\n{self._transport.stderr_text}"
            )
        if ready.get("type") == "error":
            raise ValueError(f"Bridge init error: {ready.get('message')}")
        if ready.get("type") != "ready":
            raise BridgeError(f"Expected ready, got: {ready}")

    # -- public API ---------------------------------------------------------

    def send_stream(self, message: str, **turn_options) -> Iterator[AgentEvent]:
        if turn_options:
            raise TypeError(
                f"PiAdapter does not support turn options: {sorted(turn_options)}"
            )
        self._transport.check_alive()
        self._transport.write({"type": "prompt", "message": message})

        while True:
            self._transport.check_alive()
            raw = self._transport.read()
            if raw is None:
                raise BridgeError("Bridge closed stdout unexpectedly")

            t = raw.get("type")

            if t == "tool_request":
                self._dispatch_tool_request(raw)
                continue
            if t == "response":
                continue

            event = map_pi_event(raw)
            if event is None:
                continue

            yield event

            if isinstance(event, AgentEndEvent):
                break

    def send(self, message: str, **turn_options) -> list[AgentEvent]:
        return list(self.send_stream(message, **turn_options))

    def abort(self) -> None:
        self._transport.write({"type": "abort"})

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._transport.write({"type": "shutdown"})
        except Exception:
            pass
        self._transport.close()

    # -- adapter-only extensions (not part of BackendAdapter) ---------------

    def set_model(self, provider: PiProvider, model: PiModel) -> None:
        self._transport.write({
            "type": "set_model",
            "provider": {"base_url": provider.base_url, "api_key": provider.api_key},
            "model": {
                "name": model.name,
                "api_format": model.api_format,
                "thinking": model.thinking,
            },
        })

    def set_thinking_level(self, level: str) -> None:
        self._transport.write({"type": "set_thinking_level", "level": level})

    # -- introspection ------------------------------------------------------

    @property
    def messages(self) -> list[dict]:
        self._transport.check_alive()
        self._transport.write({"type": "get_messages"})
        while True:
            raw = self._transport.read()
            if raw is None:
                raise BridgeError("Bridge closed before get_messages response")
            if raw.get("type") == "response" and raw.get("command") == "get_messages":
                return raw.get("data", {}).get("messages", [])

    @property
    def state(self) -> dict:
        self._transport.check_alive()
        self._transport.write({"type": "get_state"})
        while True:
            raw = self._transport.read()
            if raw is None:
                raise BridgeError("Bridge closed before get_state response")
            if raw.get("type") == "response" and raw.get("command") == "get_state":
                return raw.get("data", {})

    # -- internal -----------------------------------------------------------

    def _dispatch_tool_request(self, raw: dict) -> None:
        tool_id = raw["id"]
        tool_name = raw["tool"]
        args = raw.get("args", {})

        fn = next((t.fn for t in self._custom_tools if t.name == tool_name), None)
        if fn is None:
            self._transport.write({
                "type": "tool_error",
                "id": tool_id,
                "message": f"No Python handler for tool: {tool_name}",
            })
            return

        try:
            result = fn(**args)
            self._transport.write({
                "type": "tool_result",
                "id": tool_id,
                "content": str(result),
                "is_error": False,
            })
        except Exception as exc:
            self._transport.write({
                "type": "tool_result",
                "id": tool_id,
                "content": str(exc),
                "is_error": True,
            })
