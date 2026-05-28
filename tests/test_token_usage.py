"""Token usage mapping tests — verifies both mappers translate raw frames
into TokenUsageEvent with correct field names.

Codex uses `params.tokenUsage.last.{inputTokens, outputTokens, cachedInputTokens}`
Pi uses `{input_tokens, output_tokens, cached_input_tokens}` directly off the
server.mjs frame.

Run:  python3 tests/test_token_usage.py
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_bridge.backends.codex.event_mapper import map_codex_notification
from agent_bridge.backends.codex.rpc import ServerMessage
from agent_bridge.backends.pi.event_mapper import map_pi_event


# ---------------------------------------------------------------------------
# Codex: real frame shape captured from live `codex app-server` 0.133.0.
# ---------------------------------------------------------------------------

def _codex_token_msg(last: dict, total: dict | None = None) -> ServerMessage:
    return ServerMessage(
        kind="notification",
        method="thread/tokenUsage/updated",
        params={
            "threadId": "t",
            "turnId": "u",
            "tokenUsage": {
                "last": last,
                "total": total or last,
                "modelContextWindow": 258400,
            },
        },
    )


def test_codex_reads_last_not_total():
    # Total and last differ on the second LLM inference of a multi-step turn.
    # We must read .last (incremental).
    msg = _codex_token_msg(
        last={"inputTokens": 10246, "outputTokens": 5, "cachedInputTokens": 10112},
        total={"inputTokens": 20377, "outputTokens": 75, "cachedInputTokens": 18688},
    )
    events = list(map_codex_notification(msg))
    assert len(events) == 1
    e = events[0]
    assert e.type == "token_usage"
    assert e.input_tokens == 10246, e
    assert e.output_tokens == 5, e
    assert e.cached_input_tokens == 10112, e


def test_codex_first_inference_total_equals_last():
    same = {"inputTokens": 10131, "outputTokens": 70, "cachedInputTokens": 8576}
    msg = _codex_token_msg(last=same, total=same)
    e = list(map_codex_notification(msg))[0]
    assert e.input_tokens == 10131
    assert e.output_tokens == 70
    assert e.cached_input_tokens == 8576


def test_codex_missing_fields_default_to_zero():
    msg = _codex_token_msg(last={"inputTokens": 5})  # output/cached missing
    e = list(map_codex_notification(msg))[0]
    assert e.input_tokens == 5
    assert e.output_tokens == 0
    assert e.cached_input_tokens == 0


def test_codex_malformed_token_payload_does_not_crash():
    # Defensive: if Codex ever changes the shape, mapper should default to 0
    # rather than blow up.
    msg = ServerMessage(
        kind="notification",
        method="thread/tokenUsage/updated",
        params={"threadId": "t"},  # no tokenUsage at all
    )
    e = list(map_codex_notification(msg))[0]
    assert e.input_tokens == 0
    assert e.output_tokens == 0
    assert e.cached_input_tokens == 0


# ---------------------------------------------------------------------------
# Pi: server.mjs flat shape (snake_case keys).
# ---------------------------------------------------------------------------

def test_pi_token_usage_basic():
    e = map_pi_event({
        "type": "token_usage",
        "input_tokens": 100,
        "output_tokens": 20,
        "cached_input_tokens": 50,
    })
    assert e.type == "token_usage"
    assert e.input_tokens == 100
    assert e.output_tokens == 20
    assert e.cached_input_tokens == 50


def test_pi_token_usage_missing_fields_default_to_zero():
    e = map_pi_event({"type": "token_usage", "input_tokens": 7})
    assert e.input_tokens == 7
    assert e.output_tokens == 0
    assert e.cached_input_tokens == 0


# ---------------------------------------------------------------------------
# Public TokenUsageEvent shape (3 fields only — no cost / no totals / no
# reasoning_output / no cache_write).
# ---------------------------------------------------------------------------

def test_event_only_exposes_three_token_fields():
    from agent_bridge import TokenUsageEvent
    e = TokenUsageEvent(input_tokens=1, output_tokens=2, cached_input_tokens=3)
    fields = sorted(f for f in e.__dataclass_fields__ if f != "type")
    assert fields == ["cached_input_tokens", "input_tokens", "output_tokens"], fields


def main() -> int:
    tests = [
        test_codex_reads_last_not_total,
        test_codex_first_inference_total_equals_last,
        test_codex_missing_fields_default_to_zero,
        test_codex_malformed_token_payload_does_not_crash,
        test_pi_token_usage_basic,
        test_pi_token_usage_missing_fields_default_to_zero,
        test_event_only_exposes_three_token_fields,
    ]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except Exception:
            failed += 1
            print(f"  FAIL  {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
