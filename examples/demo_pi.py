#!/usr/bin/env python3
"""
demo_pi.py — full-feature walkthrough of the Pi backend.

Sets up a fresh temp workspace, runs the agent there with full permissions,
exercises:
  - AgentSession + PiConfig / PiProvider / PiModel
  - safety_mode="allow_all"
  - CustomTool — Python function exposed to the agent
  - capabilities query
  - the full event stream: text / thinking / tool_call / tool_result /
    token_usage / turn_start / turn_end / agent_end / error
  - multi-turn session continuity
  - file IO via the built-in `read` / `write` / `edit` tools
  - cumulative token accounting

Cleans up the temp workspace at the end.

Reads provider/model from `.env` at the repo root (one level up from this file).

Run:  python3 examples/demo_pi.py
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
    CustomTool,
    ErrorEvent,
    PiConfig,
    PiModel,
    PiProvider,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    TokenUsageEvent,
    ToolCallEvent,
    ToolResultEvent,
    TurnEndEvent,
    TurnStartEvent,
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

# --- Custom tool (same shape on both backends) ---------------------------

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


# --- Event printer -------------------------------------------------------

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
    elif isinstance(event, ToolCallEvent):
        # File ops + bash + custom tool all flow through this on Pi.
        args_brief = ", ".join(f"{k}={v!r}"[:60] for k, v in event.arguments.items())
        print(f"\n  [tool_call] {event.tool_name}({args_brief})")
    elif isinstance(event, ToolResultEvent):
        flag = "ERR" if event.is_error else "ok"
        content = event.content
        if len(content) > 120:
            content = content[:120] + "…"
        print(f"  [tool_result {flag}] {event.tool_name} → {content!r}")
    elif isinstance(event, TokenUsageEvent):
        acc.input += event.input_tokens
        acc.output += event.output_tokens
        acc.cached += event.cached_input_tokens
        print(
            f"\n  [token_usage] +in={event.input_tokens} "
            f"+out={event.output_tokens} +cached={event.cached_input_tokens}"
        )
    elif isinstance(event, TurnStartEvent):
        print("\n  [turn_start]")
    elif isinstance(event, TurnEndEvent):
        print("\n  [turn_end]")
    elif isinstance(event, AgentEndEvent):
        print(f"\n  [agent_end stop_reason={event.stop_reason}]")
    elif isinstance(event, ErrorEvent):
        print(f"\n  [ERROR] {event.message}", file=sys.stderr)


# --- Main ------------------------------------------------------------------

def main() -> int:
    env = load_env(ENV_PATH)
    base_url = env.get("TEST_PROVIDER_BASE_URL", "")
    api_key = env.get("TEST_PROVIDER_API_KEY", "")
    model_name = env.get("TEST_MODEL_NAME", "")
    api_format = env.get("TEST_MODEL_API_FORMAT", "completion")
    if not (base_url and api_key and model_name):
        print(f"ERROR: {ENV_PATH} missing required keys", file=sys.stderr)
        return 1

    # Ephemeral workspace — agent has full permissions inside it, and we
    # nuke the directory at the end so nothing leaks out of the demo.
    workspace = Path(tempfile.mkdtemp(prefix="agent-bridge-demo-pi-"))
    notes_path = workspace / "notes.txt"
    notes_path.write_text("favorite_color: indigo\nfavorite_number: 7\n")

    print("=" * 70)
    print("agent-bridge demo: backend=pi (allow_all + temp workspace)")
    print(f"  base_url   : {base_url}")
    print(f"  model      : {model_name}")
    print(f"  api_format : {api_format}")
    print(f"  workspace  : {workspace}")
    print(f"  pre-seeded : notes.txt (favorite_color: indigo, favorite_number: 7)")
    print("=" * 70)

    config = PiConfig(
        provider=PiProvider(base_url=base_url, api_key=api_key),
        model=PiModel(name=model_name, api_format=api_format),
        cwd=str(workspace),
        safety_mode="allow_all",
        custom_tools=[SECRET_TOOL],
    )

    acc = Accumulator()
    rc = 0

    try:
        with AgentSession(backend="pi", config=config) as session:
            caps = session.capabilities
            print("\nCapabilities (Pi backend):")
            for field, value in sorted(caps.__dict__.items()):
                print(f"  {field:<30} {value}")

            print("\n--- Round 1: custom tool ---")
            try:
                for ev in session.send_stream(
                    "Call get_secret_number, then tell me the number in one sentence."
                ):
                    print_event(ev, acc)
            except BridgeError as exc:
                print(f"\n[BRIDGE CRASH] {exc}", file=sys.stderr)
                return 1

            print("\n--- Round 2: continuity (no tool needed) ---")
            for ev in session.send_stream(
                "Multiply that secret number by 7. Reply with just the result."
            ):
                print_event(ev, acc)

            print("\n--- Round 3: read a file with the `read` tool ---")
            for ev in session.send_stream(
                "Use the read tool to look at notes.txt in the current directory. "
                "Tell me the favorite_color value, in one sentence."
            ):
                print_event(ev, acc)

            print("\n--- Round 4: write a new file with the `write` tool ---")
            for ev in session.send_stream(
                "Create a new file called greeting.txt in the current directory "
                "containing the text: 'Hello from agent-bridge demo'. "
                "Confirm when done in one sentence."
            ):
                print_event(ev, acc)

            print("\n--- Round 5: edit an existing file with the `edit` tool ---")
            for ev in session.send_stream(
                "Edit notes.txt to change favorite_color from indigo to scarlet, "
                "then confirm the change in one sentence."
            ):
                print_event(ev, acc)

            print("\n--- session.state ---")
            st = session.state
            print(f"  message_count : {st.get('message_count')}")
            print(f"  is_streaming  : {st.get('is_streaming')}")
            print(f"  model         : {st.get('model')}")

        # --- Verify what's actually on disk after the agent ran ----------
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
    print(f"TOTAL across {5} rounds:")
    print(f"  input_tokens         : {acc.input}")
    print(f"  output_tokens        : {acc.output}")
    print(f"  cached_input_tokens  : {acc.cached}")
    print("=" * 70)
    return rc


if __name__ == "__main__":
    sys.exit(main())
