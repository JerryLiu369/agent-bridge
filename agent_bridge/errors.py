"""agent-bridge errors."""


class BridgeError(Exception):
    """Subprocess crashed or transport-level protocol broke down."""


class RpcError(Exception):
    """JSON-RPC error from a Codex app-server response."""

    def __init__(self, code: int, message: str, data: dict | None = None):
        super().__init__(f"RpcError({code}): {message}")
        self.code = code
        self.message = message
        self.data = data
