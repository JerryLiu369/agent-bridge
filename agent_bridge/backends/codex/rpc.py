"""Codex app-server JSON-RPC client.

Codex's app-server speaks bidirectional JSON-RPC 2.0 over stdio, but with
two quirks worth calling out:

  * server-emitted frames frequently omit the `"jsonrpc": "2.0"` field —
    classify messages structurally:
        - id + (result | error) → response
        - id + method           → server request (must respond)
        - method, no id         → server notification
  * the server can interleave responses, notifications, and server-side
    requests in any order. We run a background reader thread that demuxes
    onto:
        - a per-id future for matching `request()` calls
        - a single ServerMessage queue for `iter_messages()` consumers

`respond()` is fire-and-forget (no return id); use it after handling a
ServerRequest from `iter_messages()`.
"""

from __future__ import annotations

import itertools
import threading
from dataclasses import dataclass
from queue import Queue
from typing import Iterator

from ...core.transport import JsonlSubprocessTransport
from ...errors import BridgeError, RpcError


@dataclass
class ServerMessage:
    kind: str  # "notification" | "request"
    method: str
    params: dict
    request_id: object | None = None  # set when kind == "request"


class _PendingResponse:
    __slots__ = ("event", "result", "error")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.result: dict | None = None
        self.error: dict | None = None


class CodexRpcClient:
    def __init__(self, transport: JsonlSubprocessTransport):
        self._transport = transport
        self._id_counter = itertools.count(1)
        self._pending: dict[object, _PendingResponse] = {}
        self._pending_lock = threading.Lock()
        self._inbox: "Queue[ServerMessage | None]" = Queue()
        self._closed = False
        self._reader_error: BaseException | None = None
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    # -- background read loop ----------------------------------------------

    def _read_loop(self) -> None:
        try:
            while True:
                msg = self._transport.read()
                if msg is None:
                    break
                self._dispatch(msg)
        except Exception as exc:
            self._reader_error = exc
        finally:
            # Wake up everyone still waiting.
            with self._pending_lock:
                pending = list(self._pending.values())
                self._pending.clear()
            for p in pending:
                p.error = {"code": -32000, "message": "transport closed"}
                p.event.set()
            self._inbox.put(None)  # sentinel for iter_messages()

    def _dispatch(self, msg: dict) -> None:
        msg_id = msg.get("id")
        method = msg.get("method")

        if msg_id is not None and method is None:
            # response
            with self._pending_lock:
                pending = self._pending.pop(msg_id, None)
            if pending is None:
                return  # unknown id, drop
            if "error" in msg:
                pending.error = msg["error"]
            else:
                pending.result = msg.get("result", {})
            pending.event.set()
            return

        if msg_id is not None and method is not None:
            # server-side request
            self._inbox.put(ServerMessage(
                kind="request",
                method=method,
                params=msg.get("params") or {},
                request_id=msg_id,
            ))
            return

        if method is not None:
            # notification
            self._inbox.put(ServerMessage(
                kind="notification",
                method=method,
                params=msg.get("params") or {},
            ))
            return

        # malformed — drop silently

    # -- client → server ---------------------------------------------------

    def request(
        self,
        method: str,
        params: dict | None = None,
        *,
        timeout: float | None = None,
    ) -> dict:
        if self._closed:
            raise BridgeError("RPC client closed")
        if self._reader_error is not None:
            raise BridgeError(f"Reader thread crashed: {self._reader_error}")

        rid = next(self._id_counter)
        pending = _PendingResponse()
        with self._pending_lock:
            self._pending[rid] = pending

        frame = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            frame["params"] = params
        self._transport.write(frame)

        if not pending.event.wait(timeout=timeout):
            with self._pending_lock:
                self._pending.pop(rid, None)
            raise BridgeError(f"RPC timeout for {method!r}")

        if pending.error is not None:
            err = pending.error
            raise RpcError(
                code=err.get("code", -32000),
                message=err.get("message", "rpc error"),
                data=err.get("data"),
            )
        return pending.result or {}

    def notify(self, method: str, params: dict | None = None) -> None:
        if self._closed:
            raise BridgeError("RPC client closed")
        frame = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            frame["params"] = params
        self._transport.write(frame)

    # -- server → client (responses to ServerRequest) ----------------------

    def respond(
        self,
        request_id: object,
        result: dict | None = None,
        *,
        error: dict | None = None,
    ) -> None:
        if self._closed:
            return
        frame: dict = {"jsonrpc": "2.0", "id": request_id}
        if error is not None:
            frame["error"] = error
        else:
            frame["result"] = result or {}
        self._transport.write(frame)

    # -- server stream ------------------------------------------------------

    def iter_messages(self) -> Iterator[ServerMessage]:
        while True:
            msg = self._inbox.get()
            if msg is None:
                if self._reader_error is not None:
                    raise BridgeError(
                        f"Reader thread crashed: {self._reader_error}"
                    )
                return
            yield msg

    # -- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        self._closed = True
