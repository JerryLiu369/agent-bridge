#!/usr/bin/env python3
"""
demo_codex.py — full-feature walkthrough of the Codex backend.

Reuses the same .env (base_url + api_key) as demo_pi.py, but Codex 0.133+
only speaks the OpenAI Responses API, so we have to override the model
name. The default below is `gpt-5.5` (verified to work against
aigc.x-see.cn). Set `CODEX_MODEL` to use a different one.

Sets up a fresh temp workspace, runs the agent there with full permissions,
exercises:
  - AgentSession + CodexConfig / CodexProvider / CodexModel
  - Custom provider injection (-c flags + experimental_bearer_token)
  - safety_mode="allow_all"
  - Same CustomTool definition we used in demo_pi.py
    (proves the tool definition is backend-agnostic)
  - capabilities query (notice the diffs vs Pi)
  - Codex-specific event stream:
      reasoning_summary, command_start/end (cat/ls etc),
      file_change + turn_diff (apply_patch), warning, token_usage
  - Multi-turn session continuity
  - Cumulative token accounting

Cleans up the temp workspace at the end.

Run:
  python3 examples/demo_codex.py             # uses gpt-5.5
  CODEX_MODEL=gpt-5.4 python3 examples/demo_codex.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from agent_bridge import (
    AgentEndEvent,
    AgentSession,
    BridgeError,
    CodexAuth,
    CodexConfig,
    CodexModel,
    CodexProvider,
    CommandEndEvent,
    CommandStartEvent,
    CustomTool,
    ErrorEvent,
    FileChangeEvent,
    ReasoningSummaryDeltaEvent,
    RpcError,
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


ENV_PATH = REPO_ROOT / ".env"


def load_env(path) -> dict[str, str]:
    out: dict[str, str] = {}
    with open(path) as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


# Same tool definition as demo_pi.py — copy/paste-identical, no Codex-specific
# bits anywhere.

SECRET = 4242


def get_secret_number() -> str:
    print("    [tool] get_secret_number() called")
    return str(SECRET)


SECRET_TOOL = CustomTool(
    name="get_secret_number",
    description=(
        "Returns a secret number that the agent cannot guess. "
        "Call this whenever the user asks for THE secret number."
    ),
    parameters={"type": "object", "properties": {}},
    fn=get_secret_number,
)


class Accumulator:
    input = 0
    output = 0
    cached = 0


def print_event(event, acc: Accumulator) -> None:
    if isinstance(event, TextDeltaEvent):
        sys.stdout.write(event.delta)
        sys.stdout.flush()
    elif isinstance(event, ThinkingDeltaEvent):
        sys.stdout.write(f"\033[90m{event.delta}\033[0m")
        sys.stdout.flush()
    elif isinstance(event, ReasoningSummaryDeltaEvent):
        sys.stdout.write(f"\033[36m{event.delta}\033[0m")  # cyan summary
        sys.stdout.flush()
    elif isinstance(event, ToolCallEvent):
        args_brief = ", ".join(f"{k}={v!r}"[:60] for k, v in event.arguments.items())
        print(f"\n  [tool_call] {event.tool_name}({args_brief})")
    elif isinstance(event, ToolResultEvent):
        flag = "ERR" if event.is_error else "ok"
        content = event.content
        if len(content) > 120:
            content = content[:120] + "…"
        print(f"  [tool_result {flag}] {event.tool_name} → {content!r}")
    elif isinstance(event, CommandStartEvent):
        print(f"\n  [command_start] {event.command} (cwd={event.cwd})")
    elif isinstance(event, CommandEndEvent):
        out = (event.output or "").strip()
        if out and len(out) > 120:
            out = out[:120] + "…"
        suffix = f" output={out!r}" if out else ""
        print(f"\n  [command_end exit={event.exit_code}{suffix}]")
    elif isinstance(event, FileChangeEvent):
        print(f"\n  [file_change] {event.path} status={event.status}")
        if event.diff:
            preview = event.diff[:200] + ("…" if len(event.diff) > 200 else "")
            print(f"      diff={preview!r}")
    elif isinstance(event, TurnDiffEvent):
        print(f"\n  [turn_diff] {len(event.unified_diff)} chars")
    elif isinstance(event, TokenUsageEvent):
        acc.input += event.input_tokens
        acc.output += event.output_tokens
        acc.cached += event.cached_input_tokens
        print(
            f"\n  [token_usage] +in={event.input_tokens} "
            f"+out={event.output_tokens} +cached={event.cached_input_tokens}"
        )
    elif isinstance(event, TurnStartEvent):
        print(f"\n  [turn_start id={event.turn_id}]")
    elif isinstance(event, TurnEndEvent):
        print(f"\n  [turn_end stop_reason={event.stop_reason!r}]")
    elif isinstance(event, AgentEndEvent):
        print(f"\n  [agent_end stop_reason={event.stop_reason}]")
    elif isinstance(event, WarningEvent):
        print(f"\n  [warning {event.kind}] {event.message}")
    elif isinstance(event, ErrorEvent):
        print(f"\n  [ERROR] {event.message}", file=sys.stderr)


def main() -> int:
    env = load_env(ENV_PATH)
    base_url = env.get("TEST_PROVIDER_BASE_URL", "")
    api_key = env.get("TEST_PROVIDER_API_KEY", "")
    if not (base_url and api_key):
        print(f"ERROR: {ENV_PATH} missing base_url/api_key", file=sys.stderr)
        return 1

    model_name = os.environ.get("CODEX_MODEL", "gpt-5.5")

    workspace = Path(tempfile.mkdtemp(prefix="agent-bridge-demo-codex-"))
    notes_path = workspace / "notes.txt"
    notes_path.write_text("favorite_color: indigo\nfavorite_number: 7\n")

    print("=" * 70)
    print("agent-bridge demo: backend=codex (allow_all + temp workspace)")
    print(f"  base_url   : {base_url}")
    print(f"  model      : {model_name}")
    print(f"  api_format : response (the only one Codex supports)")
    print(f"  workspace  : {workspace}")
    print(f"  pre-seeded : notes.txt (favorite_color: indigo, favorite_number: 7)")
    print("=" * 70)

    config = CodexConfig(
        cwd=str(workspace),
        auth=CodexAuth(),                       # ignored when provider is set
        provider=CodexProvider(base_url=base_url, api_key=api_key),
        model=CodexModel(name=model_name),
        safety_mode="allow_all",
        custom_tools=[SECRET_TOOL],
    )

    acc = Accumulator()
    rc = 0

    try:
        with AgentSession(backend="codex", config=config) as session:
            caps = session.capabilities
            print("\nCapabilities (Codex backend):")
            for field, value in sorted(caps.__dict__.items()):
                print(f"  {field:<30} {value}")

            print("\n--- Round 1: custom tool ---")
            try:
                for ev in session.send_stream(
                    "Call get_secret_number, then tell me the number in one sentence."
                ):
                    print_event(ev, acc)
            except (BridgeError, RpcError) as exc:
                print(f"\n[CRASH] {exc}", file=sys.stderr)
                return 1

            print("\n--- Round 2: continuity (no tool needed) ---")
            for ev in session.send_stream(
                "Multiply that secret number by 7. Reply with just the result."
            ):
                print_event(ev, acc)

            print("\n--- Round 3: read a file via shell (cat) ---")
            for ev in session.send_stream(
                "Use a shell command to read notes.txt in the current directory. "
                "Tell me the favorite_color value, in one sentence. "
                "You MUST run the actual shell command."
            ):
                print_event(ev, acc)

            print("\n--- Round 4: write a new file via apply_patch ---")
            for ev in session.send_stream(
                "Create a new file called greeting.txt in the current directory "
                "containing the text: 'Hello from agent-bridge demo'. "
                "Confirm when done in one sentence."
            ):
                print_event(ev, acc)

            print("\n--- Round 5: edit an existing file via apply_patch ---")
            for ev in session.send_stream(
                "Edit notes.txt to change favorite_color from indigo to scarlet, "
                "then confirm the change in one sentence."
            ):
                print_event(ev, acc)

            print("\n--- session.state ---")
            st = session.state
            print(f"  thread_id : {st.get('thread_id')}")
            print(f"  model     : {st.get('model')}")

        print("\n--- workspace contents after the run ---")
        for path in sorted(workspace.iterdir()):
            content = path.read_text()
            print(f"  {path.name}:")
            for line in content.splitlines():
                print(f"    {line}")

    finally:
        shutil.rmtree(workspace, ignore_errors=True)
        print(f"\n[cleanup] removed {workspace}")

    print("\n" + "=" * 70)
    print(f"TOTAL across 5 rounds:")
    print(f"  input_tokens         : {acc.input}")
    print(f"  output_tokens        : {acc.output}")
    print(f"  cached_input_tokens  : {acc.cached}")
    print("=" * 70)
    return rc


if __name__ == "__main__":
    sys.exit(main())
