"""agent-bridge — multi-backend coding-agent session abstraction.

Single entrypoint:

    from agent_bridge import AgentSession, PiConfig, PiProvider, PiModel
    session = AgentSession(backend="pi", config=PiConfig(...))

Codex backend:

    from agent_bridge import AgentSession, CodexConfig, CodexAuth
    session = AgentSession(backend="codex", config=CodexConfig(...))
"""

__version__ = "0.2.0"

from .core.capabilities import Capabilities
from .core.events import (
    AgentEndEvent,
    AgentEvent,
    CommandEndEvent,
    CommandStartEvent,
    ErrorEvent,
    FileChangeEvent,
    ReasoningSummaryDeltaEvent,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    TokenUsageEvent,
    ToolCallEvent,
    ToolResultEvent,
    TurnDiffEvent,
    TurnEndEvent,
    TurnStartEvent,
    WarningEvent,
)
from .core.tools import CustomTool
from .errors import BridgeError, RpcError
from .session import AgentSession

from .backends.pi.config import PiConfig, PiModel, PiProvider
from .backends.codex.config import CodexAuth, CodexConfig, CodexModel, CodexProvider

__all__ = [
    "__version__",
    # session
    "AgentSession",
    # configs
    "PiConfig",
    "PiProvider",
    "PiModel",
    "CodexConfig",
    "CodexAuth",
    "CodexProvider",
    "CodexModel",
    # tools / capabilities
    "CustomTool",
    "Capabilities",
    # errors
    "BridgeError",
    "RpcError",
    # events
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
