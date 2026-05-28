"""Standalone smoke tests for CodexAdapter — no pytest dependency.

Verifies the three custom-tool guarantees:

  1. `initialize` opts into experimentalApi
  2. `thread/start` carries `dynamicTools` derived from CustomTool
  3. `item/tool/call` ServerRequests dispatch to CustomTool.fn and reply
     with the Codex-shaped `{contentItems, success}` payload

Uses an in-memory FakeTransport so no real `codex app-server` subprocess
is spawned.

Run:  python3 tests/test_codex_adapter.py
"""

from __future__ import annotations

import sys
import threading
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_bridge import CodexAuth, CodexConfig, CodexModel, CodexProvider, CustomTool
from agent_bridge.backends.codex import adapter as codex_adapter_mod
from agent_bridge.backends.codex.adapter import CodexAdapter


# ---------------------------------------------------------------------------
# FakeTransport — talks the JsonlSubprocessTransport contract that
# CodexAdapter / CodexRpcClient expect.
# ---------------------------------------------------------------------------

class FakeTransport:
    def __init__(self, *args, **kwargs):
        self.writes: list[dict] = []
        self.queue: list[dict] = []
        self._next_turn_script: list[dict] = []
        self._closed = False
        self._cond = threading.Condition()

    def script_turn(self, messages: list[dict]) -> None:
        self._next_turn_script = list(messages)

    def write(self, obj: dict) -> None:
        with self._cond:
            self.writes.append(obj)
            method = obj.get("method")
            if method == "initialize":
                self.queue.append({
                    "id": obj["id"],
                    "result": {
                        "userAgent": "fake/0.0.0",
                        "codexHome": "/tmp/.codex",
                        "platformFamily": "unix",
                        "platformOs": "linux",
                    },
                })
            elif method == "thread/start":
                self.queue.append({
                    "id": obj["id"],
                    "result": {"thread": {"id": "thr_test"}},
                })
            elif method == "turn/start":
                self.queue.append({
                    "id": obj["id"],
                    "result": {"turn": {"id": "turn_1", "status": "inProgress"}},
                })
                self.queue.extend(self._next_turn_script)
                self._next_turn_script = []
            elif method == "turn/interrupt":
                self.queue.append({"id": obj["id"], "result": {}})
            self._cond.notify_all()

    def read(self) -> dict | None:
        with self._cond:
            while not self.queue and not self._closed:
                self._cond.wait()
            if self._closed and not self.queue:
                return None
            return self.queue.pop(0)

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    @property
    def stderr_text(self) -> str:
        return ""

    def is_alive(self) -> bool:
        return not self._closed

    def check_alive(self) -> None:
        pass


def _patched_adapter():
    """Returns (holder, restore).

    holder['transport'] gets the FakeTransport instance.
    holder['cmd'] / holder['cwd'] / holder['env'] capture how
    JsonlSubprocessTransport was called, so tests can assert on the launch
    command without spawning a real subprocess.
    """
    holder: dict = {}
    original = codex_adapter_mod.JsonlSubprocessTransport

    def factory(cmd, cwd=None, env=None, *a, **kw):
        ft = FakeTransport()
        holder["transport"] = ft
        holder["cmd"] = list(cmd)
        holder["cwd"] = cwd
        holder["env"] = dict(env or {})
        return ft

    codex_adapter_mod.JsonlSubprocessTransport = factory  # type: ignore[assignment]

    def restore():
        codex_adapter_mod.JsonlSubprocessTransport = original  # type: ignore[assignment]

    return holder, restore


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

def test_initialize_opts_into_experimental_api():
    holder, restore = _patched_adapter()
    try:
        adapter = CodexAdapter(CodexConfig(
            cwd="/tmp", auth=CodexAuth(), model=CodexModel(name="gpt-test"),
        ))
        init = next(w for w in holder["transport"].writes if w.get("method") == "initialize")
        assert init["params"]["capabilities"] == {"experimentalApi": True}, init
        assert adapter.thread_id == "thr_test"
        adapter.close()
    finally:
        restore()


def test_custom_tools_become_dynamic_tools_on_thread_start():
    holder, restore = _patched_adapter()
    try:
        cfg = CodexConfig(
            cwd="/tmp",
            auth=CodexAuth(),
            custom_tools=[CustomTool(
                name="lookup_ticket",
                description="Fetch a ticket",
                parameters={
                    "type": "object",
                    "properties": {"id": {"type": "string"}},
                    "required": ["id"],
                },
                fn=lambda id: f"ticket {id}",
            )],
        )
        adapter = CodexAdapter(cfg)
        start = next(w for w in holder["transport"].writes if w.get("method") == "thread/start")
        dts = start["params"]["dynamicTools"]
        assert len(dts) == 1
        assert dts[0]["name"] == "lookup_ticket"
        assert dts[0]["description"] == "Fetch a ticket"
        assert dts[0]["inputSchema"]["properties"]["id"]["type"] == "string"
        adapter.close()
    finally:
        restore()


def test_thread_start_omits_dynamic_tools_when_none():
    holder, restore = _patched_adapter()
    try:
        adapter = CodexAdapter(CodexConfig(cwd="/tmp", auth=CodexAuth()))
        start = next(w for w in holder["transport"].writes if w.get("method") == "thread/start")
        assert "dynamicTools" not in start["params"]
        adapter.close()
    finally:
        restore()


def test_item_tool_call_dispatches_to_custom_tool():
    holder, restore = _patched_adapter()
    try:
        calls: list[dict] = []

        def lookup(**kwargs) -> str:
            calls.append(kwargs)
            return f"ticket {kwargs['id']}: ok"

        adapter = CodexAdapter(CodexConfig(
            cwd="/tmp",
            auth=CodexAuth(),
            custom_tools=[CustomTool(
                name="lookup_ticket",
                description="Fetch a ticket",
                parameters={"type": "object", "properties": {"id": {"type": "string"}}},
                fn=lookup,
            )],
        ))
        transport = holder["transport"]
        transport.script_turn([
            {
                "id": 99,
                "method": "item/tool/call",
                "params": {"tool": "lookup_ticket", "arguments": {"id": "ABC-1"}},
            },
            {
                "method": "turn/completed",
                "params": {"turn": {"id": "turn_1", "status": "completed"}},
            },
        ])

        events = list(adapter.send_stream("look up ABC-1"))
        assert calls == [{"id": "ABC-1"}], calls

        response = next(w for w in transport.writes if w.get("id") == 99)
        assert response["result"] == {
            "contentItems": [{"type": "inputText", "text": "ticket ABC-1: ok"}],
            "success": True,
        }, response

        # The adapter should now yield ToolCallEvent before invoking the
        # python fn, then ToolResultEvent after — same shape as Pi.
        kinds = [e.type for e in events]
        assert "tool_call" in kinds, kinds
        assert "tool_result" in kinds, kinds
        assert kinds.index("tool_call") < kinds.index("tool_result"), kinds

        tc = next(e for e in events if e.type == "tool_call")
        assert tc.tool_name == "lookup_ticket"
        assert tc.arguments == {"id": "ABC-1"}

        tr = next(e for e in events if e.type == "tool_result")
        assert tr.tool_name == "lookup_ticket"
        assert tr.content == "ticket ABC-1: ok"
        assert tr.is_error is False

        # AgentEndEvent should be synthesized at turn/completed.
        assert "agent_end" in kinds, kinds
        assert kinds[-1] == "agent_end", kinds

        # turn_end still appears, before agent_end.
        assert kinds.index("turn_end") < kinds.index("agent_end"), kinds
        adapter.close()
    finally:
        restore()


def test_item_tool_call_failure_returns_success_false():
    holder, restore = _patched_adapter()
    try:
        def boom(**kwargs):
            raise RuntimeError("kaboom")

        adapter = CodexAdapter(CodexConfig(
            cwd="/tmp",
            auth=CodexAuth(),
            custom_tools=[CustomTool(
                name="boom",
                description="explodes",
                parameters={"type": "object", "properties": {}},
                fn=boom,
            )],
        ))
        transport = holder["transport"]
        transport.script_turn([
            {
                "id": 99,
                "method": "item/tool/call",
                "params": {"tool": "boom", "arguments": {}},
            },
            {
                "method": "turn/completed",
                "params": {"turn": {"id": "turn_1", "status": "completed"}},
            },
        ])
        events = list(adapter.send_stream("trigger"))

        response = next(w for w in transport.writes if w.get("id") == 99)
        assert response["result"]["success"] is False
        assert "kaboom" in response["result"]["contentItems"][0]["text"]
        assert "error" not in response

        # Even on failure, ToolCallEvent + ToolResultEvent(is_error=True)
        # should still appear in the stream.
        tr = next(e for e in events if e.type == "tool_result")
        assert tr.is_error is True
        assert "kaboom" in tr.content
        adapter.close()
    finally:
        restore()


def test_codex_synthesizes_agent_end_after_turn_completed():
    """Each `send_stream` call should end with AgentEndEvent — same sentinel
    Pi uses — so callers can branch on the same `agent_end` type."""
    holder, restore = _patched_adapter()
    try:
        adapter = CodexAdapter(CodexConfig(cwd="/tmp", auth=CodexAuth()))
        transport = holder["transport"]
        transport.script_turn([
            {
                "method": "turn/completed",
                "params": {"turn": {"id": "turn_1", "status": "completed"}},
            },
        ])
        events = list(adapter.send_stream("hi"))
        kinds = [e.type for e in events]
        assert kinds[-1] == "agent_end", kinds
        agent_end = events[-1]
        assert agent_end.stop_reason == "completed", agent_end
        adapter.close()
    finally:
        restore()


def test_codex_file_change_item_fans_out_per_file():
    """A single fileChange item with N changes should produce N
    FileChangeEvents — verified against the real wire shape."""
    holder, restore = _patched_adapter()
    try:
        adapter = CodexAdapter(CodexConfig(cwd="/tmp", auth=CodexAuth()))
        transport = holder["transport"]
        transport.script_turn([
            {
                "method": "item/started",
                "params": {
                    "item": {
                        "type": "fileChange",
                        "id": "call_x",
                        "status": "inProgress",
                        "changes": [
                            {"path": "a.txt", "kind": {"type": "add"},
                             "diff": "hello\n"},
                            {"path": "b.txt", "kind": {"type": "update"},
                             "diff": "@@ -1 +1 @@\n-old\n+new\n"},
                        ],
                    },
                },
            },
            {
                "method": "item/completed",
                "params": {
                    "item": {
                        "type": "fileChange",
                        "id": "call_x",
                        "status": "completed",
                        "changes": [
                            {"path": "a.txt", "kind": {"type": "add"},
                             "diff": "hello\n"},
                            {"path": "b.txt", "kind": {"type": "update"},
                             "diff": "@@ -1 +1 @@\n-old\n+new\n"},
                        ],
                    },
                },
            },
            {
                "method": "turn/diff/updated",
                "params": {"diff": "diff --git a/a.txt b/a.txt\n..."},
            },
            {
                "method": "turn/completed",
                "params": {"turn": {"id": "t1", "status": "completed"}},
            },
        ])

        events = list(adapter.send_stream("apply patch"))
        fc = [e for e in events if e.type == "file_change"]
        # 2 changes × 2 lifecycle frames (started + completed) = 4
        assert len(fc) == 4, [e for e in fc]

        # Started events in order
        started = [e for e in fc if e.status == "started"]
        assert [e.path for e in started] == ["a.txt", "b.txt"], started
        assert started[0].diff == "hello\n"

        # Completed events in same order
        completed = [e for e in fc if e.status == "completed"]
        assert [e.path for e in completed] == ["a.txt", "b.txt"], completed

        # turn_diff event uses `diff` field on the wire (not `unifiedDiff`).
        td = next(e for e in events if e.type == "turn_diff")
        assert td.unified_diff.startswith("diff --git"), td.unified_diff

        adapter.close()
    finally:
        restore()


def test_unknown_tool_name_returns_failure_response():
    holder, restore = _patched_adapter()
    try:
        adapter = CodexAdapter(CodexConfig(cwd="/tmp", auth=CodexAuth()))
        transport = holder["transport"]
        transport.script_turn([
            {
                "id": 99,
                "method": "item/tool/call",
                "params": {"tool": "nope", "arguments": {}},
            },
            {
                "method": "turn/completed",
                "params": {"turn": {"id": "turn_1", "status": "completed"}},
            },
        ])
        list(adapter.send_stream("trigger"))

        response = next(w for w in transport.writes if w.get("id") == 99)
        assert response["result"]["success"] is False
        assert "nope" in response["result"]["contentItems"][0]["text"]
        adapter.close()
    finally:
        restore()


# ---------------------------------------------------------------------------
# Custom provider injection (CodexProvider + CodexModel)
# ---------------------------------------------------------------------------

def test_custom_provider_injects_c_flags_in_launch_command():
    holder, restore = _patched_adapter()
    try:
        cfg = CodexConfig(
            cwd="/tmp",
            provider=CodexProvider(
                base_url="https://gw.example.com/v1",
                api_key="sk-secret",
            ),
            model=CodexModel(name="gpt-5.5"),
        )
        adapter = CodexAdapter(cfg)
        cmd = holder["cmd"]
        # Joined command for substring assertions
        joined = " ".join(cmd)
        assert 'model_provider="agent_bridge_provider"' in joined, joined
        assert 'model_providers.agent_bridge_provider.base_url="https://gw.example.com/v1"' in joined
        assert 'model_providers.agent_bridge_provider.wire_api="responses"' in joined
        assert 'model_providers.agent_bridge_provider.experimental_bearer_token="sk-secret"' in joined
        assert 'model_providers.agent_bridge_provider.name="agent-bridge custom"' in joined
        # Auth fallback env should NOT appear when provider is used.
        assert "CODEX_API_KEY" not in holder["env"], holder["env"]
        adapter.close()
    finally:
        restore()


def test_no_provider_means_no_c_flags():
    holder, restore = _patched_adapter()
    try:
        adapter = CodexAdapter(CodexConfig(cwd="/tmp", auth=CodexAuth()))
        cmd = holder["cmd"]
        assert "-c" not in cmd, cmd
        assert cmd == ["codex", "app-server"], cmd
        adapter.close()
    finally:
        restore()


def test_auth_api_key_falls_back_when_no_provider():
    holder, restore = _patched_adapter()
    try:
        adapter = CodexAdapter(CodexConfig(
            cwd="/tmp",
            auth=CodexAuth(api_key="sk-fallback"),
        ))
        assert holder["env"].get("CODEX_API_KEY") == "sk-fallback", holder["env"]
        adapter.close()
    finally:
        restore()


def test_auth_api_key_ignored_when_provider_set():
    holder, restore = _patched_adapter()
    try:
        adapter = CodexAdapter(CodexConfig(
            cwd="/tmp",
            auth=CodexAuth(api_key="sk-fallback"),
            provider=CodexProvider(base_url="https://x", api_key="sk-real"),
            model=CodexModel(name="x"),
        ))
        # Provider's bearer goes via -c; auth.api_key is ignored entirely.
        assert "CODEX_API_KEY" not in holder["env"]
        joined = " ".join(holder["cmd"])
        assert "sk-real" in joined
        assert "sk-fallback" not in joined
        adapter.close()
    finally:
        restore()


# ---------------------------------------------------------------------------
# CodexModel field translation on thread/start
# ---------------------------------------------------------------------------

def test_thread_start_translates_thinking_to_reasoning_effort():
    holder, restore = _patched_adapter()
    try:
        adapter = CodexAdapter(CodexConfig(
            cwd="/tmp",
            model=CodexModel(name="gpt-5.5", thinking="medium"),
        ))
        start = next(w for w in holder["transport"].writes if w.get("method") == "thread/start")
        assert start["params"]["model"] == "gpt-5.5"
        assert start["params"]["reasoningEffort"] == "medium"
        # Always asks for an auto summary so reasoning summary deltas can flow.
        assert start["params"]["reasoningSummary"] == "auto"
        adapter.close()
    finally:
        restore()


def test_thread_start_omits_reasoning_effort_when_thinking_is_none():
    holder, restore = _patched_adapter()
    try:
        adapter = CodexAdapter(CodexConfig(
            cwd="/tmp",
            model=CodexModel(name="gpt-5.5"),  # thinking defaults to None
        ))
        start = next(w for w in holder["transport"].writes if w.get("method") == "thread/start")
        assert "reasoningEffort" not in start["params"], start["params"]
        # reasoningSummary still always set.
        assert start["params"]["reasoningSummary"] == "auto"
        adapter.close()
    finally:
        restore()


def test_thinking_off_is_treated_as_none():
    holder, restore = _patched_adapter()
    try:
        adapter = CodexAdapter(CodexConfig(
            cwd="/tmp",
            model=CodexModel(name="gpt-5.5", thinking="off"),
        ))
        start = next(w for w in holder["transport"].writes if w.get("method") == "thread/start")
        assert "reasoningEffort" not in start["params"], start["params"]
        adapter.close()
    finally:
        restore()


# ---------------------------------------------------------------------------
# Validation: invalid api_format / thinking values raise before spawning
# ---------------------------------------------------------------------------

def test_codex_rejects_api_format_completion():
    holder, restore = _patched_adapter()
    try:
        try:
            CodexAdapter(CodexConfig(
                cwd="/tmp",
                model=CodexModel(name="x", api_format="completion"),
            ))
        except ValueError as exc:
            assert "completion" in str(exc) or "0.133" in str(exc), str(exc)
            # Adapter must have raised BEFORE creating any transport.
            assert "transport" not in holder, "transport was created before validation"
        else:
            raise AssertionError("expected ValueError")
    finally:
        restore()


def test_codex_rejects_api_format_anthropic():
    holder, restore = _patched_adapter()
    try:
        try:
            CodexAdapter(CodexConfig(
                cwd="/tmp",
                model=CodexModel(name="x", api_format="anthropic"),
            ))
        except ValueError as exc:
            assert "anthropic" in str(exc), str(exc)
        else:
            raise AssertionError("expected ValueError")
    finally:
        restore()


def test_codex_rejects_thinking_xhigh():
    holder, restore = _patched_adapter()
    try:
        try:
            CodexAdapter(CodexConfig(
                cwd="/tmp",
                model=CodexModel(name="x", thinking="xhigh"),
            ))
        except ValueError as exc:
            assert "xhigh" in str(exc), str(exc)
        else:
            raise AssertionError("expected ValueError")
    finally:
        restore()


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main() -> int:
    tests = [
        test_initialize_opts_into_experimental_api,
        test_custom_tools_become_dynamic_tools_on_thread_start,
        test_thread_start_omits_dynamic_tools_when_none,
        test_item_tool_call_dispatches_to_custom_tool,
        test_item_tool_call_failure_returns_success_false,
        test_codex_synthesizes_agent_end_after_turn_completed,
        test_codex_file_change_item_fans_out_per_file,
        test_unknown_tool_name_returns_failure_response,
        # provider injection
        test_custom_provider_injects_c_flags_in_launch_command,
        test_no_provider_means_no_c_flags,
        test_auth_api_key_falls_back_when_no_provider,
        test_auth_api_key_ignored_when_provider_set,
        # model translation
        test_thread_start_translates_thinking_to_reasoning_effort,
        test_thread_start_omits_reasoning_effort_when_thinking_is_none,
        test_thinking_off_is_treated_as_none,
        # validation
        test_codex_rejects_api_format_completion,
        test_codex_rejects_api_format_anthropic,
        test_codex_rejects_thinking_xhigh,
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
