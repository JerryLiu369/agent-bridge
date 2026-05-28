"""JSONL subprocess transport, shared by Pi and Codex backends.

One JSON object per line on stdin/stdout. stderr is drained on a background
thread and surfaced as part of `BridgeError` when the child dies.
"""

import json
import os
import subprocess
import threading

from ..errors import BridgeError


class JsonlSubprocessTransport:
    def __init__(
        self,
        cmd: list[str],
        cwd: str,
        env: dict | None = None,
    ):
        self._cmd = list(cmd)
        self._cwd = os.path.abspath(cwd)
        process_env = os.environ.copy()
        if env:
            process_env.update(env)

        self._proc = subprocess.Popen(
            self._cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=process_env,
            cwd=self._cwd,
        )

        self._stdin_lock = threading.Lock()
        self._stderr_lines: list[str] = []
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr, daemon=True
        )
        self._stderr_thread.start()
        self._closed = False

    # -- I/O ----------------------------------------------------------------

    def write(self, obj: dict) -> None:
        if self._closed:
            raise BridgeError("Transport is closed")
        line = (json.dumps(obj) + "\n").encode()
        with self._stdin_lock:
            self._proc.stdin.write(line)
            self._proc.stdin.flush()

    def read(self) -> dict | None:
        """One JSON object. Returns None on EOF. Skips blank/malformed lines."""
        while True:
            raw = self._proc.stdout.readline()
            if not raw:
                return None
            line = raw.decode(errors="replace").strip()
            if not line:
                continue
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue

    # -- introspection ------------------------------------------------------

    def is_alive(self) -> bool:
        return not self._closed and self._proc.poll() is None

    @property
    def stderr_text(self) -> str:
        return "\n".join(self._stderr_lines)

    @property
    def returncode(self) -> int | None:
        return self._proc.returncode

    def check_alive(self) -> None:
        if self._closed:
            raise BridgeError("Transport is closed")
        if self._proc.poll() is not None:
            raise BridgeError(
                f"Subprocess exited (code {self._proc.returncode}). "
                f"Stderr:\n{self.stderr_text}"
            )

    # -- lifecycle ----------------------------------------------------------

    def _drain_stderr(self) -> None:
        for line in self._proc.stderr:
            self._stderr_lines.append(line.decode(errors="replace").rstrip())

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._proc.stdin.close()
        except Exception:
            pass
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait()
