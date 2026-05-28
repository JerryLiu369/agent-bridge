#!/usr/bin/env python3
"""
demo.py — minimal "what does it take to swap backends?" sample.

For full feature walkthroughs see `demo_pi.py` / `demo_codex.py`. This one
boils the swap down to its essence:

  * Everything in the SWITCH block changes when you swap backends.
  * Everything below /SWITCH is backend-agnostic.

Reads creds from `.env` at the repo root (one level up from this file).

Run:  python3 examples/demo.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from agent_bridge import (
    AgentSession,
    AgentEndEvent,
    CodexAuth,
    CodexConfig,
    CodexModel,
    CodexProvider,
    CustomTool,
    PiConfig,
    PiModel,
    PiProvider,
    TextDeltaEvent,
    ToolCallEvent,
    ToolResultEvent,
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


# ===========================================================================
# Custom tool — defined ONCE, plugs into either backend without changes.
# ===========================================================================

def get_secret_number() -> str:
    return "4242"


SECRET_TOOL = CustomTool(
    name="get_secret_number",
    description="Returns a secret number that the agent cannot guess.",
    parameters={"type": "object", "properties": {}},
    fn=get_secret_number,
)


# ===========================================================================
# ▼▼▼  SWITCH BLOCK  ▼▼▼  (this is everything that changes between backends)
# ===========================================================================

BACKEND = "pi"   # ← change to "codex" to swap

ENV = load_env(ENV_PATH)
CWD = os.path.abspath(".")

if BACKEND == "pi":
    config = PiConfig(
        provider=PiProvider(
            base_url=ENV["TEST_PROVIDER_BASE_URL"],
            api_key=ENV["TEST_PROVIDER_API_KEY"],
        ),
        model=PiModel(
            name=ENV["TEST_MODEL_NAME"],
            api_format=ENV.get("TEST_MODEL_API_FORMAT", "completion"),
        ),
        cwd=CWD,
        safety_mode="read_only",
        custom_tools=[SECRET_TOOL],
    )

elif BACKEND == "codex":
    config = CodexConfig(
        provider=CodexProvider(
            base_url=ENV["TEST_PROVIDER_BASE_URL"],
            api_key=ENV["TEST_PROVIDER_API_KEY"],
        ),
        # Codex 0.133+ only speaks OpenAI Responses; the upstream gateway
        # routes deepseek-v4-pro to chat completions, so we override to a
        # Responses-compatible model.
        model=CodexModel(name=os.environ.get("CODEX_MODEL", "gpt-5.5")),
        auth=CodexAuth(),  # ignored when provider is set
        cwd=CWD,
        safety_mode="read_only",
        custom_tools=[SECRET_TOOL],
    )

else:
    raise SystemExit(f"unknown BACKEND={BACKEND!r} (use 'pi' or 'codex')")

# ===========================================================================
# ▲▲▲  /SWITCH BLOCK  ▲▲▲  (everything below is backend-agnostic)
# ===========================================================================


def print_event(event) -> None:
    if isinstance(event, TextDeltaEvent):
        sys.stdout.write(event.delta)
        sys.stdout.flush()
    elif isinstance(event, ToolCallEvent):
        print(f"\n  [tool_call] {event.tool_name}({event.arguments})")
    elif isinstance(event, ToolResultEvent):
        print(f"\n  [tool_result] {event.tool_name} → {event.content!r}")
    elif isinstance(event, AgentEndEvent):
        print(f"\n  [agent_end stop_reason={event.stop_reason}]")


print(f"--- backend = {BACKEND} ---")

with AgentSession(backend=BACKEND, config=config) as session:
    print("\n=== Round 1 ===")
    for ev in session.send_stream(
        "Call get_secret_number and tell me the value in one sentence."
    ):
        print_event(ev)

    print("\n=== Round 2 (continuity) ===")
    for ev in session.send_stream(
        "Multiply that number by 7. Reply with just the result."
    ):
        print_event(ev)

print(f"\nDone. Switch BACKEND='{BACKEND}' → "
      f"'{'codex' if BACKEND == 'pi' else 'pi'}' and rerun to see the other side.")
