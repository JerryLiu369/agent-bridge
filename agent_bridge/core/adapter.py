"""Backend-agnostic adapter protocol.

Adapters do not subclass anything — they just satisfy this Protocol.
The factory (in agent_bridge.session) dispatches `backend` strings to
concrete adapter classes; everything else is structural.
"""

from typing import Iterator, Protocol, runtime_checkable

from .capabilities import Capabilities
from .events import AgentEvent


@runtime_checkable
class BackendAdapter(Protocol):
    capabilities: Capabilities

    def send_stream(self, message: str, **turn_options) -> Iterator[AgentEvent]: ...

    def send(self, message: str, **turn_options) -> list[AgentEvent]: ...

    def abort(self) -> None: ...

    def close(self) -> None: ...

    @property
    def state(self) -> dict: ...

    @property
    def messages(self) -> list[dict]: ...
