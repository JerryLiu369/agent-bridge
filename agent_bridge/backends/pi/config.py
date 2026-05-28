"""PiConfig — backend-specific config for the Pi adapter."""

from dataclasses import dataclass, field

from ...core.config import AgentSessionConfig
from ...core.tools import CustomTool


@dataclass
class PiProvider:
    base_url: str
    api_key: str = ""


@dataclass
class PiModel:
    name: str
    api_format: str  # "completion" | "response" | "anthropic"
    thinking: str | None = None


@dataclass
class PiConfig(AgentSessionConfig):
    provider: PiProvider | None = None
    model: PiModel | None = None
    custom_tools: list[CustomTool] = field(default_factory=list)
    persist: bool = False
    bridge_path: str = ""

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.provider is None:
            raise TypeError("PiConfig.provider is required")
        if self.model is None:
            raise TypeError("PiConfig.model is required")
