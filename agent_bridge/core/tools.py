"""Custom tool definition shared by both backends.

The Python callable is dispatched by the adapter when its backend signals a
tool invocation (Pi: bridge `tool_request` frame; Codex: `item/tool/call`
ServerRequest).
"""

from dataclasses import dataclass, field
from typing import Callable


@dataclass
class CustomTool:
    name: str
    description: str
    parameters: dict
    fn: Callable
    prompt_snippet: str | None = None
    prompt_guidelines: list[str] = field(default_factory=list)
