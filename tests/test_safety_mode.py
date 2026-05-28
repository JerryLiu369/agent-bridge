"""Smoke tests for safety_mode → backend-primitive translation.

Pi:    safety_mode → tools allow-list passed to server.mjs
Codex: safety_mode → sandbox + approvalPolicy on thread/start
"""

from __future__ import annotations

import sys
import threading
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_bridge import (
    CodexAuth,
    CodexConfig,
    PiConfig,
    PiModel,
    PiProvider,
)
from agent_bridge.backends.codex import adapter as codex_adapter_mod
from agent_bridge.backends.codex.adapter import CodexAdapter
from agent_bridge.backends.pi.adapter import _tools_for_safety_mode


# ---------------------------------------------------------------------------
# Pi: pure function — no transport needed.
# ---------------------------------------------------------------------------

def test_pi_allow_all_returns_none_for_default_tools():
    assert _tools_for_safety_mode("allow_all") is None


def test_pi_read_only_restricts_to_read_tool():
    assert _tools_for_safety_mode("read_only") == ["read"]


def test_pi_rejects_unknown_safety_mode():
    try:
        _tools_for_safety_mode("nope")
    except ValueError as exc:
        assert "nope" in str(exc)
    else:
        raise AssertionError("expected ValueError")


# ---------------------------------------------------------------------------
# AgentSessionConfig validation runs at construction time.
# ---------------------------------------------------------------------------

def test_pi_config_rejects_bad_safety_mode():
    try:
        PiConfig(
            provider=PiProvider(base_url="x"),
            model=PiModel(name="m", api_format="completion"),
            safety_mode="whatever",
        )
    except ValueError as exc:
        assert "whatever" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_codex_config_rejects_bad_safety_mode():
    try:
        CodexConfig(cwd="/tmp", auth=CodexAuth(), safety_mode="whatever")
    except ValueError as exc:
        assert "whatever" in str(exc)
    else:
        raise AssertionError("expected ValueError")


# ---------------------------------------------------------------------------
# Codex: requires the FakeTransport from test_codex_adapter so the adapter
# can complete its initialize / thread-start handshake without a subprocess.
# ---------------------------------------------------------------------------

class FakeTransport:
    def __init__(self, *args, **kwargs):
        self.writes: list[dict] = []
        self.queue: list[dict] = []
        self._closed = False
        self._cond = threading.Condition()

    def write(self, obj):
        with self._cond:
            self.writes.append(obj)
            method = obj.get("method")
            if method == "initialize":
                self.queue.append({"id": obj["id"], "result": {}})
            elif method == "thread/start":
                self.queue.append({
                    "id": obj["id"],
                    "result": {"thread": {"id": "thr_test"}},
                })
            self._cond.notify_all()

    def read(self):
        with self._cond:
            while not self.queue and not self._closed:
                self._cond.wait()
            if self._closed and not self.queue:
                return None
            return self.queue.pop(0)

    def close(self):
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    @property
    def stderr_text(self):
        return ""

    def is_alive(self):
        return not self._closed

    def check_alive(self):
        pass


def _patched_adapter():
    holder: dict = {}
    original = codex_adapter_mod.JsonlSubprocessTransport

    def factory(*a, **kw):
        ft = FakeTransport()
        holder["transport"] = ft
        return ft

    codex_adapter_mod.JsonlSubprocessTransport = factory  # type: ignore[assignment]

    def restore():
        codex_adapter_mod.JsonlSubprocessTransport = original  # type: ignore[assignment]

    return holder, restore


def test_codex_allow_all_uses_danger_full_access():
    holder, restore = _patched_adapter()
    try:
        adapter = CodexAdapter(CodexConfig(cwd="/tmp", auth=CodexAuth()))
        start = next(w for w in holder["transport"].writes if w.get("method") == "thread/start")
        assert start["params"]["sandbox"] == "danger-full-access", start
        assert start["params"]["approvalPolicy"] == "never", start
        adapter.close()
    finally:
        restore()


def test_codex_read_only_uses_read_only_sandbox():
    holder, restore = _patched_adapter()
    try:
        adapter = CodexAdapter(
            CodexConfig(cwd="/tmp", auth=CodexAuth(), safety_mode="read_only")
        )
        start = next(w for w in holder["transport"].writes if w.get("method") == "thread/start")
        assert start["params"]["sandbox"] == "read-only", start
        assert start["params"]["approvalPolicy"] == "never", start
        adapter.close()
    finally:
        restore()


def test_codex_resume_thread_skips_safety_translation():
    """thread/resume reuses the existing thread's safety settings; we
    shouldn't try to re-send sandbox/approvalPolicy on resume."""
    holder, restore = _patched_adapter()
    try:
        # Patch the FakeTransport to also handle thread/resume:
        original_factory = codex_adapter_mod.JsonlSubprocessTransport

        def factory(*a, **kw):
            ft = FakeTransport()
            original_write = ft.write

            def write_with_resume(obj):
                if obj.get("method") == "thread/resume":
                    with ft._cond:
                        ft.writes.append(obj)
                        ft.queue.append({
                            "id": obj["id"],
                            "result": {"thread": {"id": "thr_resumed"}},
                        })
                        ft._cond.notify_all()
                else:
                    original_write(obj)

            ft.write = write_with_resume  # type: ignore[method-assign]
            holder["transport"] = ft
            return ft

        codex_adapter_mod.JsonlSubprocessTransport = factory  # type: ignore[assignment]

        adapter = CodexAdapter(CodexConfig(
            cwd="/tmp",
            auth=CodexAuth(),
            resume_thread_id="thr_old",
            safety_mode="read_only",
        ))

        # Should not have sent thread/start at all.
        assert not any(w.get("method") == "thread/start" for w in holder["transport"].writes)
        resume = next(w for w in holder["transport"].writes if w.get("method") == "thread/resume")
        assert resume["params"] == {"threadId": "thr_old"}
        adapter.close()
    finally:
        restore()


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main() -> int:
    tests = [
        test_pi_allow_all_returns_none_for_default_tools,
        test_pi_read_only_restricts_to_read_tool,
        test_pi_rejects_unknown_safety_mode,
        test_pi_config_rejects_bad_safety_mode,
        test_codex_config_rejects_bad_safety_mode,
        test_codex_allow_all_uses_danger_full_access,
        test_codex_read_only_uses_read_only_sandbox,
        test_codex_resume_thread_skips_safety_translation,
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
