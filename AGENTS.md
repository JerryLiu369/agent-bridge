# AGENTS.md — agent-bridge orientation

This is the file Claude Code (or any agent) reads first when entering this
repo. It exists to make a new agent productive in 5 minutes, not 50.

## What this repo is

A Python package that gives you **one API** to drive coding agents from
multiple underlying runtimes:

```python
from agent_bridge import AgentSession, PiConfig, PiProvider, PiModel
session = AgentSession(backend="pi", config=PiConfig(...))   # or "codex"
for event in session.send_stream("..."):
    if event.type == "text_delta":
        print(event.delta, end="")
```

Currently supported runtimes: **Pi** (Earendil's `pi-coding-agent` Node SDK)
and **Codex** (OpenAI's `codex app-server` over JSON-RPC stdio).

The architecture is documented in `agent_bridge_multi_backend_architecture_clean.md`
— that's the design source-of-truth, this file is the operator manual.

## Repo layout

```
agent_bridge/                       Python package
├── __init__.py                     public exports — AgentSession, configs, events
├── session.py                      AgentSession + create_adapter() factory
├── errors.py                       BridgeError, RpcError
├── core/                           backend-agnostic primitives
│   ├── adapter.py                  BackendAdapter Protocol
│   ├── capabilities.py             Capabilities dataclass (11 flags)
│   ├── config.py                   AgentSessionConfig (cwd, system_prompt, safety_mode)
│   ├── events.py                   16 AgentEvent dataclasses
│   ├── tools.py                    CustomTool dataclass
│   └── transport.py                JsonlSubprocessTransport (shared)
└── backends/
    ├── pi/                         Pi backend
    │   ├── adapter.py              PiAdapter
    │   ├── config.py               PiConfig / PiProvider / PiModel
    │   ├── event_mapper.py         server.mjs frame → AgentEvent
    │   ├── server.mjs              Node bridge spawned as subprocess
    │   └── package.json
    └── codex/                      Codex backend
        ├── adapter.py              CodexAdapter
        ├── config.py               CodexConfig / CodexProvider / CodexModel / CodexAuth
        ├── event_mapper.py         JSON-RPC frame → AgentEvent
        └── rpc.py                  CodexRpcClient (bidirectional JSON-RPC demux)

examples/
├── demo.py                         minimal "swap backends" sample (one BACKEND= line)
├── demo_pi.py                      Pi full walkthrough
└── demo_codex.py                   Codex full walkthrough

tests/                              standalone test scripts (no pytest dependency)
├── test_codex_adapter.py
├── test_safety_mode.py
└── test_token_usage.py
```

## Data flow at runtime

```
caller.send_stream(msg)
   │
   ▼
AgentSession  ─►  BackendAdapter.send_stream
                       │
                       ├─ writes prompt frame via JsonlSubprocessTransport
                       │     (Pi: "prompt"; Codex: "turn/start")
                       │
                       ▼
                 backend subprocess (server.mjs / codex app-server)
                       │
                       ▼
                 emits backend-native frames
                       │
                       ▼
                 event_mapper.py translates each frame into 0+ AgentEvents
                       │
                       ▼
                 yielded back through send_stream
```

## Core abstractions you must understand before changing anything

**AgentSession** — pure facade. Owns one BackendAdapter, forwards calls.
The factory in `session.py` is the *only* place a `if backend == ...`
branch is allowed.

**BackendAdapter** (Protocol in `core/adapter.py`) — every backend
satisfies the same structural protocol: `send_stream / send / abort /
close`, `state / messages / capabilities` properties.

**event_mapper** — pure function `(raw_frame) → Iterable[AgentEvent]`. One
frame can fan out into multiple public events (e.g. one Codex `fileChange`
item with N `changes` → N FileChangeEvents). The mapper is **the single
source of truth for backend-specific protocol details**. Changing wire
field names? Edit the mapper, not the adapter.

**safety_mode** — single permission knob in `core/config.py`. Two values:
- `"allow_all"` (default): Pi gets default 4 tools (read/bash/edit/write); Codex gets `sandbox=danger-full-access`, `approvalPolicy=never`
- `"read_only"`: Pi gets `tools=["read"]` only; Codex gets `sandbox=read-only`, `approvalPolicy=never`

Both modes use Codex's `approvalPolicy=never` so the agent never blocks
asking for approval — the sandbox enforces the actual policy. There is
**no third mode** and approval handling is intentionally not part of the
public surface (escape hatch: `adapter.set_approval_handler(...)`).

**CustomTool** (`core/tools.py`) — the same `CustomTool(name, description,
parameters, fn)` instance plugs into either backend's `custom_tools=[...]`.
Pi calls it as a regular tool; Codex registers it as a `dynamicTools` entry
on `thread/start` and dispatches `item/tool/call` ServerRequests to `fn`.

## Pi vs Codex: capability matrix

11 capability flags on `session.capabilities`. Use these instead of
hard-coding `session.backend == "..."`:

| flag | Pi | Codex | meaning |
|---|---|---|---|
| streaming | ✓ | ✓ | basic streaming output |
| reasoning_stream | ✓ | ✓ | thinking/reasoning_summary deltas exist |
| custom_tools | ✓ | ✓ | CustomTool dispatch supported |
| builtin_command_exec |   | ✓ | shell exec produces command_* events (Pi treats bash as a regular tool) |
| builtin_file_ops |   | ✓ | file edits produce file_change events (Pi treats edit/write as regular tools) |
| turn_diff |   | ✓ | TurnDiffEvent (cumulative diff per turn) |
| sandbox |   | ✓ | OS-level sandbox enforcement (Pi has process-level tool allow-list only) |
| resume |   | ✓ | thread/resume via `resume_thread_id` |
| token_usage_events | ✓ | ✓ | TokenUsageEvent per LLM inference |
| model_switch | ✓ | ✓ | per-turn `model=` override |
| reasoning_effort_switch |   | ✓ | per-turn `effort=` override |

## Event matrix (16 types)

| event | Pi | Codex |
|---|---|---|
| TextDeltaEvent | ✓ | ✓ |
| ThinkingDeltaEvent | ✓ | ✓ (gpt-5 series usually empty — OpenAI hides raw CoT) |
| ReasoningSummaryDeltaEvent |   | ✓ |
| ToolCallEvent / ToolResultEvent | ✓ | ✓ (covers Pi's bash/read/edit/write **and** dynamic tools on both) |
| CommandStartEvent / CommandEndEvent |   | ✓ (Pi flattens shell into ToolCallEvent; this event is only for Codex's first-class shell) |
| FileChangeEvent |   | ✓ (Pi flattens write/edit into ToolCallEvent) |
| TurnDiffEvent |   | ✓ |
| TokenUsageEvent | ✓ | ✓ |
| TurnStartEvent / TurnEndEvent | ✓ | ✓ |
| AgentEndEvent | ✓ | ✓ (Codex synthesizes it from `turn/completed`) |
| WarningEvent |   | ✓ (Pi protocol has no warning channel) |
| ErrorEvent | ✓ | ✓ |

The asymmetric ones aren't bugs — Pi's protocol genuinely doesn't have those concepts, or Pi *does* have the underlying capability but exposes it through `ToolCallEvent` rather than a first-class event. Don't try to "fix" the asymmetry; it's intentional.

## Wire-shape gotchas (we hit each of these in practice — don't repeat)

When editing `backends/codex/event_mapper.py`, these are the field-name
traps already discovered the hard way:

- **camelCase, not snake_case**: `item.type == "commandExecution"` and
  `item.type == "fileChange"` — *not* `command_execution` / `file_change`.
- **token usage path**: `params.tokenUsage.last.{inputTokens, outputTokens, cachedInputTokens}`.
  *Not* `params.usage.cacheReadTokens`. Read `.last` (incremental), not `.total`.
- **dynamic tool response**: reply with
  `{"contentItems": [{"type": "inputText", "text": ...}], "success": bool}`.
  The content item type is `"inputText"`, *not* `"text"`. Get this wrong and
  the model sees "dynamic tool response was invalid" and ignores you.
- **fileChange items contain a `changes: [...]` array**: a single fileChange
  item can touch N files in one frame. Mapper must fan out to N FileChangeEvents.
- **turn/diff/updated field is `diff`**, not `unifiedDiff`.
- **codex 0.133+ removed `wire_api="chat"`**: only `wire_api="responses"` works.
  CodexModel.api_format only accepts `"response"` — anything else raises at
  startup.
- **Codex doesn't stream command output**: there's no
  `item/commandExecution/outputDelta` in practice. The full stdout arrives
  on `item/completed` as `aggregatedOutput`. We surface it as
  `CommandEndEvent.output`.

When in doubt, write a probe to capture the live wire shape (see "How to
verify protocol shape" below) — *don't* trust the OpenAI documentation.

## Modification guidelines

### Adding a new event type
1. Add the dataclass to `core/events.py` and to the `AgentEvent` union and `__all__`.
2. Re-export from `agent_bridge/__init__.py`.
3. Wire the corresponding raw frame in **both** mappers if both backends produce it; only one if asymmetric.
4. Add a unit test that constructs the raw frame and asserts the mapper output.
5. Update the event-matrix table in this file and in `README.md`.

### Adding a new Capabilities flag
Same checklist + flip the flag in both `PI_CAPABILITIES` (in `backends/pi/adapter.py`)
and `CODEX_CAPABILITIES` (in `backends/codex/adapter.py`). Don't add
"capability declared True but no public path to use it" flags — every flag
must correspond to an actually-exposed event or method. We deleted
`fork` / `inject_items` / `steer` / `compact` / `plan_events` / `approvals`
for exactly this reason.

### Editing the Codex adapter or mapper
- The mapper signature is `(msg) → Iterable[AgentEvent]`. Always return a
  list (or generator), even for the empty case. The adapter loops with
  `for ev in mapper(msg): yield ev` — don't break that contract.
- The Codex `_handle_server_request` method is *also* a generator and
  must yield events for tool dispatch. See current code for the pattern.
- Don't trust upstream protocol docs — verify with a probe before shipping.

### What NOT to do
- **Don't reintroduce abstractions we deleted** (`compact()`, `fork()`,
  `inject_items()`, `steer()`, `PlanUpdateEvent`, `ApprovalRequestEvent`).
  They were removed because no one consumes them and nothing on the public
  API can produce them in our `safety_mode`-locked world. If you genuinely
  need one, justify the public-API path that consumes it before adding.
- **Don't try to unify Pi's `ToolCallEvent("bash")` with Codex's
  `CommandStartEvent`**. The protocols treat shell differently; merging
  loses fields (cwd / processId / exitCode / aggregatedOutput).
- **Don't put backend-specific knowledge in `AgentSession`**. The only
  permitted backend dispatch is in `_create_adapter` in `session.py`.
- **Don't change `safety_mode` to have a third value** without thinking
  through both backends. The current 2-valued model is load-bearing —
  it's why we don't expose Approval as a public concern.

## Testing

```bash
python3 tests/test_codex_adapter.py        # 18 tests, codex protocol + dispatch
python3 tests/test_safety_mode.py          # 8 tests, safety_mode → backend translation
python3 tests/test_token_usage.py          # 7 tests, token mapper field shapes
```

All three are vanilla `python3 file.py`-runnable, no pytest required, no
network or API key needed. They use `FakeTransport` to mock the subprocess.
Run them every time you touch a mapper or adapter.

## Demos (live, require .env + network)

```bash
python3 examples/demo.py             # one-line BACKEND switch sample
python3 examples/demo_pi.py          # Pi full walkthrough
python3 examples/demo_codex.py       # Codex full walkthrough
```

Each demo creates a temp workspace, runs 3-5 turns, verifies disk state,
and cleans up. They read credentials from `.env` at the repo root (which
is `.gitignored`). The `.env` schema:

```
TEST_PROVIDER_BASE_URL=https://...
TEST_PROVIDER_API_KEY=sk-...
TEST_MODEL_NAME=...
TEST_MODEL_API_FORMAT=completion        # for Pi: completion | response | anthropic
# TEST_MODEL_THINKING=                  # optional: off|minimal|low|medium|high|xhigh
```

For Codex you also need `codex` CLI ≥ 0.133.0 on PATH. The Codex demo
overrides the model to `gpt-5.5` by default (set `CODEX_MODEL` to override)
because the upstream gateway routes the .env's `deepseek-v4-pro` to chat
completions, which codex 0.133+ no longer speaks.

## How to verify protocol shape (probes)

When the wire format is unclear, write a one-off probe under `/tmp` that
captures the raw JSON-RPC frames. Pattern:

1. Spawn `codex app-server` (or `node server.mjs`) directly via subprocess.
2. Send the JSON-RPC frames yourself; print every server response/notification verbatim.
3. Look at field names and shapes in the actual output, then update the mapper.

This is how every wire-shape bug in this repo was found. Don't trust docs —
verify.

## When updating dependencies

- **codex CLI version**: pinned at 0.133+ informally. If you need to support
  a new codex version, run a probe and check whether method names or item
  types changed (codex's app-server is "experimental" in their docs).
- **Pi SDK version**: lives in `node_modules`; demo discovers it via
  `npm root -g`. If a Pi SDK update changes event shapes, fix
  `agent_bridge/backends/pi/server.mjs` (the Node bridge).
