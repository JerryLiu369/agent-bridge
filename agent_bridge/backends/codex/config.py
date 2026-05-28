"""CodexConfig — backend-specific config for the Codex adapter.

Field shape mirrors PiConfig where possible (provider/model) so swapping
backends is a near-mechanical rewrite. See agent_bridge.backends.pi.config
for the parallel structure.

Codex 0.133+ removed `wire_api="chat"`. The only valid `api_format` for
Codex is `"response"`. Other values raise at adapter startup.
"""

from dataclasses import dataclass, field

from ...core.config import AgentSessionConfig
from ...core.tools import CustomTool


@dataclass
class CodexAuth:
    """Auth for Codex's built-in `openai` provider.

    Used when `CodexConfig.provider` is None. Primary path is `codex login`
    (credentials in `~/.codex`); `api_key` here is a lower-priority fallback
    that gets written to the `CODEX_API_KEY` env var.
    """

    api_key: str | None = None
    codex_home: str | None = None


@dataclass
class CodexProvider:
    """Custom OpenAI-Responses-compatible endpoint.

    Maps to a `[model_providers.<id>]` entry injected via `--config` at
    `codex app-server` startup. The endpoint MUST speak the OpenAI Responses
    API — codex 0.133+ no longer supports Chat Completions.
    """

    base_url: str
    api_key: str


@dataclass
class CodexModel:
    """Model selection for the Codex backend.

    Field names match `PiModel` to make backend swaps mechanical.

      api_format  Codex only supports "response". Other values raise at
                  startup; the field exists for cross-backend symmetry.
      thinking    Reasoning effort. Codex accepts None / "off" / "minimal" /
                  "low" / "medium" / "high". "xhigh" raises (Codex doesn't
                  go that high).
    """

    name: str
    api_format: str = "response"
    thinking: str | None = None


@dataclass
class CodexConfig(AgentSessionConfig):
    auth: CodexAuth = field(default_factory=CodexAuth)
    provider: CodexProvider | None = None
    model: CodexModel | None = None
    resume_thread_id: str | None = None
    base_instructions: str | None = None
    developer_instructions: str | None = None

    custom_tools: list[CustomTool] = field(default_factory=list)

    # app-server launch
    app_server_cmd: list[str] | None = None
    listen: str = "stdio://"
