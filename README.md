# agent-bridge

Python entrypoint for coding agents. One API, multiple backends:

- **Pi** — the [Pi Agent SDK](https://github.com/earendil-works/pi) (Node.js bridge).
- **Codex** — OpenAI's `codex app-server` (JSON-RPC over stdio).

```python
from agent_bridge import AgentSession, PiConfig, PiProvider, PiModel

session = AgentSession(
    backend="pi",
    config=PiConfig(
        provider=PiProvider(base_url="https://api.deepseek.com/v1", api_key="sk-..."),
        model=PiModel(name="deepseek-chat", api_format="completion"),
        cwd="/your/project",
    ),
)

for event in session.send_stream("列出当前目录的文件"):
    if event.type == "text_delta":
        print(event.delta, end="", flush=True)

session.close()
```

The same call shape works against Codex; only the config swaps. The Pi
and Codex configs share field names where they can — `provider`, `model`,
`safety_mode`, `custom_tools`, etc. — so backend swaps are mechanical.
Custom Python tools written for one backend run unchanged on the other.

## Install

```bash
git clone <this-repo>
cd agent-bridge
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

### Backend prerequisites

| Backend | Requirement |
|---------|-------------|
| `pi`    | Node.js ≥ 18, `npm install -g @earendil-works/pi-coding-agent` |
| `codex` | `codex` CLI ≥ 0.124.0 on `PATH`, plus `codex login` (or `OPENAI_API_KEY` / `CODEX_API_KEY`) |

## Backends

### Pi

```python
from agent_bridge import AgentSession, PiConfig, PiProvider, PiModel, CustomTool

def web_search(query: str) -> str:
    return "..."

session = AgentSession(
    backend="pi",
    config=PiConfig(
        provider=PiProvider(base_url="https://api.anthropic.com/v1", api_key="sk-..."),
        model=PiModel(name="claude-sonnet-4-5", api_format="anthropic", thinking="medium"),
        cwd=".",
        custom_tools=[CustomTool(
            name="web_search",
            description="Search the web",
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
            fn=web_search,
        )],
    ),
)
```

### Codex

```python
from agent_bridge import (
    AgentSession, CodexConfig, CodexAuth, CodexProvider, CodexModel,
)

session = AgentSession(
    backend="codex",
    config=CodexConfig(
        cwd="/your/project",
        provider=CodexProvider(
            base_url="https://your-gateway/v1",   # OpenAI-Responses-compatible
            api_key="sk-...",
        ),
        model=CodexModel(name="gpt-5.5", thinking="medium"),
        safety_mode="read_only",
    ),
)

for event in session.send_stream("Refactor the auth module"):
    if event.type == "text_delta":
        print(event.delta, end="", flush=True)

session.close()
```

If you don't pass a `provider`, the adapter falls back to your local
`codex login` state (or `CodexAuth(api_key=...)` for direct API-key auth
to OpenAI's built-in provider).

Codex turn options (`model`, `effort`, `cwd`, `summary`, `output_schema`,
`service_tier`) can also be passed as keyword args to `send` / `send_stream`.
The Pi adapter rejects all turn options.

#### Codex limitations to know about

- **Only OpenAI Responses API is supported** (`api_format="response"`).
  codex 0.133+ removed Chat Completions support; passing `"completion"` or
  `"anthropic"` raises at startup. If your endpoint only speaks Chat
  Completions, use the Pi backend instead.
- `thinking="xhigh"` doesn't exist on Codex (use `"high"` instead). All
  other `thinking` values map cleanly to Codex's `reasoning_effort`.
- Custom provider injection works by passing `-c model_providers.<id>.*`
  flags to `codex app-server` at startup. The provider is process-level —
  every thread in the same `AgentSession` shares it. To swap providers,
  create a new `AgentSession`.

## Safety modes

`safety_mode` lives on `AgentSessionConfig` (the base both `PiConfig` and
`CodexConfig` inherit from), and is the single knob for "what is the agent
allowed to do". Each backend translates it into its native primitives:

| `safety_mode` | Pi (built-in tools) | Codex (`thread/start`) |
|---|---|---|
| `allow_all` (default) | full set: `read`, `bash`, `edit`, `write` | `sandbox=danger-full-access`, `approvalPolicy=never` |
| `read_only`           | `read` only | `sandbox=read-only`, `approvalPolicy=never` |

Both modes use Codex's `approvalPolicy=never` so the agent never blocks
waiting for client confirmation; the sandbox enforces the actual policy.
There's an `adapter.set_approval_handler(...)` escape hatch on the Codex
adapter for power users who need a custom policy.

Custom tools are always available regardless of mode.

## Custom tools

`CustomTool` is shared between backends. Define the tool once, plug the
same instance into either config:

```python
from agent_bridge import CustomTool

def lookup_ticket(id: str) -> str:
    return f"ticket {id}: in progress"

ticket_tool = CustomTool(
    name="lookup_ticket",
    description="Fetch a ticket by id",
    parameters={
        "type": "object",
        "properties": {"id": {"type": "string"}},
        "required": ["id"],
    },
    fn=lookup_ticket,
)

# Same tool object — works on both backends.
PiConfig(..., custom_tools=[ticket_tool])
CodexConfig(..., custom_tools=[ticket_tool])
```

Under the hood, the Codex adapter:

- opts into `capabilities.experimentalApi=true` during `initialize` (required
  for dynamic tools)
- registers each `CustomTool` as a `dynamicTools` entry on `thread/start`
  (`name` / `description` / `inputSchema` come straight from the dataclass)
- routes the resulting `item/tool/call` ServerRequest to your `fn`, returning
  the result as `{contentItems: [{type: "text", text: ...}], success: bool}`

Exceptions inside `fn` come back as `success=false` with the exception
message, so the model keeps reasoning instead of aborting the turn — same
behavior as the Pi backend.

## Capabilities

The two backends advertise different feature sets:

```python
session.capabilities.builtin_command_exec  # codex: True, pi: False
session.capabilities.builtin_file_ops      # codex: True, pi: False
session.capabilities.turn_diff             # codex: True, pi: False
```

Don't hard-code on `session.backend == "..."`; check capabilities instead.

## Events

`send` / `send_stream` yield instances of `AgentEvent` (a union). Pi emits a
subset; Codex exercises the full set. Each event has a `type` field carrying
the snake-case name.

| Event | Pi | Codex |
|---|---|---|
| `TextDeltaEvent` | ✓ | ✓ |
| `ThinkingDeltaEvent` | ✓ |   |
| `ReasoningSummaryDeltaEvent` |   | ✓ |
| `ToolCallEvent`, `ToolResultEvent` | ✓ | ✓ |
| `CommandStartEvent`, `CommandEndEvent` |   | ✓ |
| `FileChangeEvent`, `TurnDiffEvent` |   | ✓ |
| `TokenUsageEvent` | ✓ | ✓ |
| `TurnStartEvent`, `TurnEndEvent` | ✓ | ✓ |
| `AgentEndEvent` | ✓ | ✓ |
| `WarningEvent` |   | ✓ |
| `ErrorEvent` | ✓ | ✓ |

## Errors

```python
from agent_bridge import BridgeError, RpcError

try:
    for event in session.send_stream("..."):
        if event.type == "error":
            print("Recoverable:", event.message)
except BridgeError as exc:
    print("Subprocess died:", exc)  # session is dead
except RpcError as exc:
    print("Codex JSON-RPC error:", exc.code, exc.message)
```

`ErrorEvent` is recoverable — the session keeps running. `BridgeError`
means the subprocess crashed; create a new `AgentSession`. `RpcError` is
raised by the Codex backend when the app-server returns a JSON-RPC error.

## Architecture

```
AgentSession
 ├─ create_adapter("pi", PiConfig)    → PiAdapter    → JsonlSubprocessTransport → backends/pi/server.mjs
 └─ create_adapter("codex", CodexConfig) → CodexAdapter → JsonlSubprocessTransport + CodexRpcClient → codex app-server
```

See `agent_bridge_multi_backend_architecture_clean.md` for the full design.
