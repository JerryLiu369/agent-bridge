"""Common config base. Backend-specific configs live under
`agent_bridge.backends.<name>.config` and inherit from this.

`safety_mode` is the single source of truth for "what is the agent allowed
to do". Each adapter translates it into its native primitives:

  * Pi    — built-in tool allow-list passed to server.mjs
  * Codex — `sandbox` + `approvalPolicy` on `thread/start`

Custom tools (registered Python callables) are always available regardless
of safety_mode. The mode only governs the agent's built-in capabilities.
"""

from dataclasses import dataclass


SAFETY_ALLOW_ALL = "allow_all"
SAFETY_READ_ONLY = "read_only"

_VALID_SAFETY_MODES = frozenset({SAFETY_ALLOW_ALL, SAFETY_READ_ONLY})


@dataclass
class AgentSessionConfig:
    cwd: str = "."
    system_prompt: str = ""
    safety_mode: str = SAFETY_ALLOW_ALL

    def __post_init__(self) -> None:
        if self.safety_mode not in _VALID_SAFETY_MODES:
            raise ValueError(
                f"safety_mode must be one of {sorted(_VALID_SAFETY_MODES)}, "
                f"got {self.safety_mode!r}"
            )
