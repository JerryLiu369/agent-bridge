"""AgentSession — the single user-facing entrypoint.

```python
from agent_bridge import AgentSession, PiConfig, PiProvider, PiModel

session = AgentSession(
    backend="pi",
    config=PiConfig(
        provider=PiProvider(base_url=..., api_key=...),
        model=PiModel(name="deepseek-chat", api_format="completion"),
        cwd="/path/to/project",
    ),
)
```

The session owns one BackendAdapter and forwards calls to it. Backend-
specific behavior lives in adapters; the session itself does not branch
on `backend` strings outside of `_create_adapter`.
"""

from __future__ import annotations

from typing import Iterator

from .core.adapter import BackendAdapter
from .core.capabilities import Capabilities
from .core.events import AgentEvent


def _create_adapter(backend: str, config) -> BackendAdapter:
    if backend == "pi":
        from .backends.pi.adapter import PiAdapter
        return PiAdapter(config)
    if backend == "codex":
        from .backends.codex.adapter import CodexAdapter
        return CodexAdapter(config)
    raise ValueError(f"Unknown backend: {backend!r}")


class AgentSession:
    """Unified facade over a backend adapter."""

    def __init__(self, backend: str, config):
        self._backend = backend
        self._adapter = _create_adapter(backend, config)

    # -- forwarding --------------------------------------------------------

    def send_stream(self, message: str, **turn_options) -> Iterator[AgentEvent]:
        return self._adapter.send_stream(message, **turn_options)

    def send(self, message: str, **turn_options) -> list[AgentEvent]:
        return self._adapter.send(message, **turn_options)

    def abort(self) -> None:
        self._adapter.abort()

    def close(self) -> None:
        self._adapter.close()

    # -- introspection -----------------------------------------------------

    @property
    def backend(self) -> str:
        return self._backend

    @property
    def adapter(self) -> BackendAdapter:
        """Escape hatch: lets advanced callers reach backend-specific
        methods (e.g. PiAdapter.set_thinking_level)."""
        return self._adapter

    @property
    def capabilities(self) -> Capabilities:
        return self._adapter.capabilities

    @property
    def state(self) -> dict:
        return self._adapter.state

    @property
    def messages(self) -> list[dict]:
        return self._adapter.messages

    # -- context-manager sugar --------------------------------------------

    def __enter__(self) -> "AgentSession":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
