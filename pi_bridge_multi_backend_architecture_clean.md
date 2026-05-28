# agent-bridge 多后端架构设计（干净版）

## 1. 目标

`agent-bridge` 不再以 `PiSession` 为中心，而是改成一个通用 Coding Agent 接入层。

唯一用户入口：

```python
from agent_bridge import AgentSession, PiConfig, CodexConfig

session = AgentSession(
    backend="pi",      # or "codex"
    config=PiConfig(...),
)
```

设计目标：

- 外层 API 稳定。
- 后端实现独立。
- Pi、Codex 不互相污染。
- 不保留旧接口兼容。
- 不做万能大模型 SDK，而是做 coding agent session 抽象。

---

## 2. 核心判断

正确抽象不是：

```text
PiSession
CodexSession
```

而是：

```text
AgentSession
  -> PiAdapter
  -> CodexAdapter
```

`AgentSession` 是统一门面。  
`Adapter` 是每个后端的具体实现。  
用户不直接接触 `PiAdapter` 或 `CodexAdapter`。

---

## 3. 推荐文件结构

```text
agent_bridge/
  __init__.py
  session.py
  errors.py

  core/
    __init__.py
    adapter.py
    config.py
    events.py
    capabilities.py
    transport.py
    tools.py

  backends/
    __init__.py

    pi/
      __init__.py
      adapter.py
      config.py
      event_mapper.py
      server.mjs

    codex/
      __init__.py
      adapter.py
      config.py
      event_mapper.py
      rpc.py
```

不需要：

```text
legacy/
PiSession
types.py 旧兼容 re-export
```

旧代码可以直接迁移到新结构。

---

## 4. 各模块职责

| 模块 | 职责 |
|---|---|
| `session.py` | 用户唯一入口 `AgentSession` |
| `core/adapter.py` | 后端统一接口 |
| `core/config.py` | 公共配置基类 |
| `core/events.py` | 公共事件类型 |
| `core/capabilities.py` | 后端能力声明 |
| `core/transport.py` | subprocess / JSONL / JSON-RPC 通信 |
| `core/tools.py` | 自定义工具定义 |
| `backends/pi/` | Pi 后端 |
| `backends/codex/` | Codex 后端 |

---

## 5. AgentSession

`AgentSession` 只负责创建 adapter 和转发调用。

```python
class AgentSession:
    def __init__(self, backend: str, config):
        self._adapter = create_adapter(backend, config)

    def send_stream(self, message: str, **turn_options):
        return self._adapter.send_stream(message, **turn_options)

    def send(self, message: str, **turn_options):
        return list(self.send_stream(message, **turn_options))

    def abort(self):
        return self._adapter.abort()

    def compact(self, instructions: str = ""):
        return self._adapter.compact(instructions)

    def close(self):
        return self._adapter.close()

    @property
    def state(self):
        return self._adapter.state

    @property
    def messages(self):
        return self._adapter.messages

    @property
    def capabilities(self):
        return self._adapter.capabilities
```

原则：

- `AgentSession` 不写后端逻辑。
- `AgentSession` 不解析 Pi/Codex 事件。
- `AgentSession` 不管理 subprocess。
- `AgentSession` 里不要出现大量 `if backend == ...`。

---

## 6. Adapter 接口

所有后端实现同一个协议。

```python
from typing import Protocol, Iterator
from .events import AgentEvent
from .capabilities import Capabilities

class BackendAdapter(Protocol):
    capabilities: Capabilities

    def send_stream(self, message: str, **turn_options) -> Iterator[AgentEvent]:
        ...

    def send(self, message: str, **turn_options) -> list[AgentEvent]:
        ...

    def abort(self) -> None:
        ...

    def compact(self, instructions: str = "") -> None:
        ...

    def close(self) -> None:
        ...

    @property
    def state(self) -> dict:
        ...

    @property
    def messages(self) -> list[dict]:
        ...
```

这个接口只放公共能力。

不要把这些东西塞进公共接口：

```text
api_key
provider
api_format
sandbox
approval_policy
thread_id
```

这些都是后端配置或后端能力，不是公共 session 接口。

---

## 7. Factory

```python
def create_adapter(backend: str, config) -> BackendAdapter:
    if backend == "pi":
        from agent_bridge.backends.pi.adapter import PiAdapter
        return PiAdapter(config)

    if backend == "codex":
        from agent_bridge.backends.codex.adapter import CodexAdapter
        return CodexAdapter(config)

    raise ValueError(f"Unknown backend: {backend}")
```

`if backend` 只允许出现在 factory。  
其他代码不感知 backend 字符串。

---

## 8. Config 设计

不要做一个万能 config。公共配置只放真正共享的东西。

### 8.1 公共配置

```python
from dataclasses import dataclass

@dataclass
class AgentSessionConfig:
    cwd: str = "."
    system_prompt: str = ""
```

### 8.2 PiConfig

```python
@dataclass
class PiProvider:
    base_url: str
    api_key: str = ""

@dataclass
class PiModel:
    name: str
    api_format: str       # "completion" | "response" | "anthropic"
    thinking: str | None = None

@dataclass
class PiConfig(AgentSessionConfig):
    provider: PiProvider
    model: PiModel
    tools: list[str] | None = None
    custom_tools: list[CustomTool] | None = None
    persist: bool = False
    bridge_path: str = ""
```

### 8.3 CodexConfig

```python
@dataclass
class CodexAuth:
    # Codex 主鉴权走 `codex login`，登录态在 ~/.codex 下，app-server 默认复用。
    # 只有走 API key 直连模式才需要填这里。
    api_key: str | None = None
    codex_home: str | None = None   # 覆盖 CODEX_HOME，默认 ~/.codex

@dataclass
class SandboxConfig:
    mode: str = "workspace-write"          # "read-only" | "workspace-write" | "danger-full-access"
    writable_roots: list[str] | None = None
    network_access: bool = False

@dataclass
class CodexConfig(AgentSessionConfig):
    auth: CodexAuth
    model: str | None = None                # e.g. "gpt-5.4"
    model_provider: str | None = None
    approval_policy: str = "never"          # "untrusted" | "on-request" | "on-failure" | "never"
    sandbox: SandboxConfig | None = None
    resume_thread_id: str | None = None     # 复用旧 thread → 走 thread/resume
    base_instructions: str | None = None    # 覆盖 system prompt 顶层
    developer_instructions: str | None = None
    reasoning_effort: str | None = None     # "minimal" | "low" | "medium" | "high"
    reasoning_summary: str | None = None    # "auto" | "concise" | "detailed" | None

    # app-server 启动相关
    app_server_cmd: list[str] | None = None # 默认 ["codex", "app-server"]
    listen: str = "stdio://"                # 也可走 unix:// 或 ws://
    config_overrides: dict | None = None    # 透传到 -c key=value
```

结论：

- `api_key` 不进 `AgentSession`。
- `api_key` 不进 `send()`。
- Pi 的 key 是 provider credential。
- Codex 的 key 是 app-server/account credential，且**优先级低于** `codex login` 的本地凭据。
- 统一层不要假装两者一样。

---

## 9. 事件设计

统一事件要覆盖 coding agent 的核心行为。事件集合按"最大合理公共集"取，不是"所有后端最小交集"。

```python
AgentEvent =
    # 文本 / 思考
    TextDeltaEvent
  | ThinkingDeltaEvent
  | ReasoningSummaryDeltaEvent
    # 工具
  | ToolCallEvent
  | ToolResultEvent
    # 命令执行（Codex 才有）
  | CommandStartEvent
  | CommandOutputDeltaEvent
  | CommandEndEvent
    # 文件改动（Codex 才有）
  | FileChangeEvent
  | TurnDiffEvent
    # 计划与审批（Codex 才有）
  | PlanUpdateEvent
  | ApprovalRequestEvent
    # 计费 / 限流
  | TokenUsageEvent
    # 生命周期
  | TurnStartEvent
  | TurnEndEvent
  | AgentEndEvent
    # 警告 / 错误
  | WarningEvent
  | ErrorEvent
```

基础事件示例：

```python
@dataclass
class TextDeltaEvent:
    delta: str
    item_id: str | None = None             # Codex 有 item_id；Pi 可为空
    type: str = "text_delta"

@dataclass
class ThinkingDeltaEvent:
    delta: str
    type: str = "thinking_delta"

@dataclass
class ReasoningSummaryDeltaEvent:
    delta: str
    type: str = "reasoning_summary_delta"

@dataclass
class ToolCallEvent:
    tool_call_id: str
    tool_name: str
    arguments: dict
    type: str = "tool_call"

@dataclass
class ToolResultEvent:
    tool_call_id: str
    tool_name: str
    content: str
    is_error: bool = False
    type: str = "tool_result"

@dataclass
class CommandStartEvent:
    command: str | list[str]
    cwd: str | None = None
    type: str = "command_start"

@dataclass
class CommandOutputDeltaEvent:
    delta: str
    stream: str = "stdout"                 # "stdout" | "stderr"
    type: str = "command_output_delta"

@dataclass
class CommandEndEvent:
    exit_code: int | None = None
    type: str = "command_end"

@dataclass
class FileChangeEvent:
    path: str
    diff: str | None = None
    status: str = "completed"              # "started" | "delta" | "completed"
    type: str = "file_change"

@dataclass
class TurnDiffEvent:
    """整个 turn 的累计 diff，Codex 在 turn 内多次更新。"""
    unified_diff: str
    type: str = "turn_diff"

@dataclass
class PlanUpdateEvent:
    """Codex 的 step plan，逐步更新。"""
    steps: list[dict]
    type: str = "plan_update"

@dataclass
class ApprovalRequestEvent:
    request_id: str
    kind: str                              # "exec" | "patch" | "permissions" | "tool_input" | "elicitation"
    message: str = ""
    payload: dict | None = None            # 具体待批内容（命令、补丁、权限范围…）
    type: str = "approval_request"

@dataclass
class TokenUsageEvent:
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    type: str = "token_usage"

@dataclass
class TurnStartEvent:
    turn_id: str
    type: str = "turn_start"

@dataclass
class TurnEndEvent:
    turn_id: str
    stop_reason: str
    type: str = "turn_end"

@dataclass
class AgentEndEvent:
    """整个 session/agent 终止。Codex 上一般不发，由 close() 主动触发。"""
    stop_reason: str
    type: str = "agent_end"

@dataclass
class WarningEvent:
    kind: str                              # "deprecation" | "config" | "guardian" | "general"
    message: str
    type: str = "warning"

@dataclass
class ErrorEvent:
    message: str
    type: str = "error"
```

### 9.1 事件来源映射表

| 公共事件 | Pi 来源 | Codex 来源（v2 通知方法）|
|---|---|---|
| `TextDeltaEvent` | response stream text | `item/agentMessage/delta` |
| `ThinkingDeltaEvent` | thinking stream | `item/reasoning/textDelta` |
| `ReasoningSummaryDeltaEvent` | — | `item/reasoning/summaryTextDelta`、`item/reasoning/summaryPartAdded` |
| `ToolCallEvent` | custom tool request | `item/tool/call`（ServerRequest）+ `item/started` |
| `ToolResultEvent` | custom tool reply | `item/completed`（type=tool_call）|
| `CommandStartEvent` | — | `item/started`（type=command_execution）|
| `CommandOutputDeltaEvent` | — | `item/commandExecution/outputDelta`、`process/outputDelta` |
| `CommandEndEvent` | — | `item/completed`、`process/exited` |
| `FileChangeEvent` | — | `item/fileChange/outputDelta`、`item/fileChange/patchUpdated` |
| `TurnDiffEvent` | — | `turn/diff/updated` |
| `PlanUpdateEvent` | — | `turn/plan/updated`、`item/plan/delta` |
| `ApprovalRequestEvent` | — | `item/commandExecution/requestApproval`、`item/fileChange/requestApproval`、`item/permissions/requestApproval`、`item/tool/requestUserInput`、`mcpServer/elicitation/request`（这些都是 ServerRequest，必须回 response）|
| `TokenUsageEvent` | usage 字段 | `thread/tokenUsage/updated` |
| `TurnStartEvent` | — | `turn/started` |
| `TurnEndEvent` | end-of-turn marker | `turn/completed` |
| `AgentEndEvent` | session 结束 | 由 adapter 在 `close()` 时合成 |
| `WarningEvent` | — | `warning`、`guardianWarning`、`deprecationNotice`、`configWarning` |
| `ErrorEvent` | error | `error` |

### 9.2 原则

- Pi 不支持的事件可以不产生（`CommandStartEvent`、`FileChangeEvent`、`ApprovalRequestEvent` 等都允许缺席）。
- Codex 支持的 command/file/approval/plan/diff 事件不要压扁成 text。
- 不在公共事件里嵌入 backend-specific 原始 payload。需要原始数据的调用方走 `session.capabilities` + adapter 自己的扩展接口。
- `ApprovalRequestEvent` 不止是事件——它**要求调用方回复**。session 层需要提供 `session.respond_to_approval(request_id, decision)` 这样的对偶接口（见 §11、§13）。

---

## 10. Capability 设计

不同后端能力不同，需要显式暴露。调用方靠 `session.capabilities` 判断，而不是硬编码 backend 名称。

```python
@dataclass(frozen=True)
class Capabilities:
    # 流式输出
    streaming: bool = True
    reasoning_stream: bool = False         # ThinkingDelta / ReasoningSummaryDelta
    # 工具
    custom_tools: bool = False             # 客户端注入工具，由后端回调
    builtin_command_exec: bool = False     # 后端原生 shell 执行 → command_* 事件
    builtin_file_ops: bool = False         # 后端原生文件改动 → file_change 事件
    # 计划
    plan_events: bool = False              # turn/plan/updated
    turn_diff: bool = False                # turn/diff/updated
    # 审批 / 沙盒
    approvals: bool = False                # ApprovalRequestEvent + respond_to_approval
    sandbox: bool = False
    # Thread 管理
    resume: bool = False                   # 复用旧 thread
    fork: bool = False                     # 从某个 thread 派生
    inject_items: bool = False             # 中途注入消息
    steer: bool = False                    # turn 中插话
    # 计费 / 模型
    token_usage_events: bool = False
    model_switch: bool = False             # 单 turn 覆盖 model
    reasoning_effort_switch: bool = False  # 单 turn 覆盖 effort
    # 压缩
    compact: bool = False
    compact_async: bool = False            # True 表示 compact 异步完成（看 thread/compacted）
```

Pi：

```python
PI_CAPABILITIES = Capabilities(
    streaming=True,
    reasoning_stream=True,
    custom_tools=True,
    builtin_command_exec=False,
    builtin_file_ops=False,
    plan_events=False,
    turn_diff=False,
    approvals=False,
    sandbox=False,
    resume=False,
    fork=False,
    inject_items=False,
    steer=False,
    token_usage_events=True,
    model_switch=True,
    reasoning_effort_switch=False,
    compact=True,
    compact_async=False,
)
```

Codex：

```python
CODEX_CAPABILITIES = Capabilities(
    streaming=True,
    reasoning_stream=True,
    custom_tools=True,                     # 走 item/tool/call ServerRequest
    builtin_command_exec=True,
    builtin_file_ops=True,
    plan_events=True,
    turn_diff=True,
    approvals=True,
    sandbox=True,
    resume=True,
    fork=True,
    inject_items=True,
    steer=True,
    token_usage_events=True,
    model_switch=True,
    reasoning_effort_switch=True,
    compact=True,
    compact_async=True,                    # thread/compact/start → thread/compacted notif
)
```

注意：

- Codex 的 `compact()` 是异步的 — 调用 `thread/compact/start` 立即返回，等 `thread/compacted` 通知才真正完成。adapter 层要把这个差异吸收掉，给上层一个一致的 `compact()` 调用语义（默认阻塞到收到通知，或返回 future）。
- `custom_tools` 在 Pi 和 Codex 上的协议形态不同：Pi 走自定义 tool callback，Codex 走 `item/tool/call` ServerRequest。统一接口由 §11 的 `tools.CustomTool` 抽象。

---

## 11. Transport 层

抽出公共 subprocess 通信。

```python
class JsonlSubprocessTransport:
    def __init__(
        self,
        cmd: list[str],
        cwd: str,
        env: dict | None = None,
    ):
        ...

    def write(self, obj: dict) -> None:
        ...

    def read(self) -> dict | None:
        ...

    def close(self) -> None:
        ...
```

Pi 使用：

```python
JsonlSubprocessTransport(
    ["node", "agent_bridge/backends/pi/server.mjs"],
    cwd=config.cwd,
    env=env,
)
```

Codex 使用：

```python
JsonlSubprocessTransport(
    ["codex", "app-server"],            # 默认 stdio://
    cwd=config.cwd,
    env=env,
)
```

### 11.1 CodexRpcClient — 双向 JSON-RPC

Codex app-server 是**双向 JSON-RPC 2.0**：

- client → server：`request`（有 id，等 response）、`notify`（无 id）
- server → client：`notify`（事件流，例如 `turn/started`、`item/agentMessage/delta`）
- server → client：`request`（**有 id，要求 client 回 response**），用于 approval / elicitation / `item/tool/call`

光支持 `request` + `read_event` 不够。`CodexRpcClient` 必须能：

1. 把读到的消息按 `id`/`method` 分流：response 唤醒等待的调用方，notification 推到事件队列，server-side request 推到回调或单独队列。
2. 提供 `respond(id, result | error)` 让上层在处理完 approval 后回信。
3. 处理 id 冲突（client id 和 server id 互不干扰，但都要保留以匹配 response）。

接口：

```python
class CodexRpcClient:
    def __init__(self, transport: JsonlSubprocessTransport): ...

    # client -> server
    def request(self, method: str, params: dict | None = None, *, timeout: float | None = None) -> dict:
        """阻塞直到拿到对应 id 的 response。失败抛 RpcError。"""

    def notify(self, method: str, params: dict | None = None) -> None:
        """fire-and-forget。"""

    # server -> client
    def respond(self, request_id, result: dict | None = None, *, error: dict | None = None) -> None:
        """回应 server-side request（approval / elicitation 等）。"""

    # 事件 / 主动请求 拉取
    def iter_messages(self) -> Iterator[ServerMessage]:
        """统一拉取流：ServerNotification 或 ServerRequest。response 不在这里出现。"""

    def close(self) -> None: ...

@dataclass
class ServerMessage:
    kind: str                    # "notification" | "request"
    method: str
    params: dict
    request_id: object | None    # 仅 kind=="request" 时有值，用于 respond()
```

### 11.2 公共 RpcError

```python
class RpcError(Exception):
    def __init__(self, code: int, message: str, data: dict | None = None):
        self.code = code
        self.message = message
        self.data = data
```

`code` 沿用 JSON-RPC 标准（-32600 系列）+ Codex 自定义错误。adapter 在 mapper 里把 `RpcError` 转成统一 `ErrorEvent` 或抛出公共异常。

---

## 12. PiAdapter

职责：

- 启动 Pi bridge server。
- 发送 init。
- 发送 prompt。
- 处理 custom tool callback。
- 读取 raw event。
- 转成公共事件。
- 暴露 state/messages/abort/compact。

结构：

```python
class PiAdapter:
    capabilities = PI_CAPABILITIES

    def __init__(self, config: PiConfig):
        self.config = config
        self.transport = JsonlSubprocessTransport(...)
        self._custom_tools = config.custom_tools or []
        self._init_bridge()

    def send_stream(self, message: str, **turn_options):
        self._reject_unsupported_turn_options(turn_options)

        self.transport.write({
            "type": "prompt",
            "message": message,
        })

        while True:
            raw = self.transport.read()

            if raw["type"] == "tool_request":
                self._dispatch_tool_request(raw)
                continue

            event = map_pi_event(raw)

            if event:
                yield event

            if isinstance(event, AgentEndEvent):
                break
```

Pi 不要假装支持 Codex 的 turn-level sandbox/approval。

---

## 13. CodexAdapter

职责：

- 启动 `codex app-server`（stdio）。
- 走 JSON-RPC `initialize` → `initialized` notif 握手。
- `thread/start` 或 `thread/resume`，记下 `threadId`。
- 每次 `send_stream` → `turn/start`，循环消费 server 消息直到 `turn/completed`。
- 在循环里区分 notification（→ event mapper）和 server request（→ approval/tool 回调，必须 `respond`）。
- `abort()` → `turn/interrupt`。
- `compact()` → `thread/compact/start`，等 `thread/compacted` 通知。
- `close()` → 关闭 transport。

结构：

```python
class CodexAdapter:
    capabilities = CODEX_CAPABILITIES

    def __init__(self, config: CodexConfig):
        self.config = config
        cmd = config.app_server_cmd or ["codex", "app-server"]
        env = self._build_env(config)               # 透传 CODEX_HOME 等
        self.transport = JsonlSubprocessTransport(
            cmd, cwd=config.cwd, env=env,
        )
        self.rpc = CodexRpcClient(self.transport)

        self._messages: list[dict] = []
        self._thread_id: str | None = None
        self._approval_handler = None               # session 设置；None → 默认拒绝

        self._initialize()
        self._start_or_resume_thread()

    # ---- handshake ----
    def _initialize(self):
        self.rpc.request("initialize", {
            "clientInfo": {"name": "agent-bridge", "version": __version__},
            "capabilities": {},
        })
        self.rpc.notify("initialized", {})

    def _start_or_resume_thread(self):
        if self.config.resume_thread_id:
            res = self.rpc.request("thread/resume", {
                "threadId": self.config.resume_thread_id,
            })
        else:
            params = {"cwd": self.config.cwd}
            if self.config.model:                params["model"] = self.config.model
            if self.config.model_provider:       params["modelProvider"] = self.config.model_provider
            if self.config.approval_policy:      params["approvalPolicy"] = self.config.approval_policy
            if self.config.sandbox:              params["sandbox"] = self.config.sandbox.mode
            if self.config.base_instructions:    params["baseInstructions"] = self.config.base_instructions
            if self.config.developer_instructions: params["developerInstructions"] = self.config.developer_instructions
            res = self.rpc.request("thread/start", params)
        self._thread_id = res["thread"]["id"]

    # ---- public API ----
    def send_stream(self, message: str, **turn_options):
        params = {
            "threadId": self._thread_id,
            "input": [{"type": "text", "text": message}],
        }
        # per-turn override：v2 TurnStartParams 实际支持的字段
        for key in ("model", "effort", "approvalPolicy", "sandbox",
                    "cwd", "summary", "outputSchema", "serviceTier"):
            value = turn_options.get(_to_snake_case(key))
            if value is not None:
                params[key] = value

        self.rpc.request("turn/start", params)

        for msg in self.rpc.iter_messages():
            if msg.kind == "request":
                self._handle_server_request(msg)    # approval / tool call → respond()
                event = map_codex_server_request(msg)
                if event:
                    yield event
                continue

            # notification
            event = map_codex_notification(msg)
            if event:
                yield event

            if msg.method == "turn/completed":
                break

    def abort(self):
        if self._thread_id:
            self.rpc.request("turn/interrupt", {"threadId": self._thread_id})

    def compact(self, instructions: str = ""):
        params = {"threadId": self._thread_id}
        if instructions:
            params["instructions"] = instructions
        self.rpc.request("thread/compact/start", params)
        # 阻塞到 thread/compacted
        for msg in self.rpc.iter_messages():
            if msg.kind == "notification" and msg.method == "thread/compacted":
                return

    def close(self):
        try:
            self.rpc.notify("thread/unsubscribe", {"threadId": self._thread_id})
        finally:
            self.transport.close()

    # ---- approval / tool callbacks ----
    def set_approval_handler(self, handler):
        """handler(ApprovalRequestEvent) -> dict (approval decision)。"""
        self._approval_handler = handler

    def _handle_server_request(self, msg: ServerMessage):
        if msg.method in (
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
            "item/permissions/requestApproval",
            "item/tool/requestUserInput",
            "applyPatchApproval",
            "execCommandApproval",
            "mcpServer/elicitation/request",
        ):
            decision = self._approval_handler(msg) if self._approval_handler else {"decision": "denied"}
            self.rpc.respond(msg.request_id, decision)
            return

        if msg.method == "item/tool/call":
            # 客户端注入的 custom tool 被调用 → 走 CustomTool 注册表
            result = self._dispatch_custom_tool(msg.params)
            self.rpc.respond(msg.request_id, result)
            return

        # 未识别的 server request：拒绝，避免阻塞
        self.rpc.respond(msg.request_id, error={"code": -32601, "message": "method not handled"})
```

Codex 可以支持更丰富的 turn options：

```python
session.send_stream(
    "修复测试",
    model="gpt-5.4",
    effort="medium",                      # reasoning effort
    approval_policy="on-request",         # untrusted | on-request | on-failure | never
    sandbox="workspace-write",
    summary="concise",                    # reasoning summary
    output_schema={...},                  # 约束最终消息
)
```

这些参数只属于 `CodexAdapter`，不进入公共 adapter 接口定义。session 层在转发 `turn_options` 时，对不识别的参数：

- 由 adapter 自己抛 `TypeError("Codex turn option not recognized: ...")`，
- 或在 PiAdapter 里走 `_reject_unsupported_turn_options(turn_options)`。

### 13.1 与 §12 PiAdapter 的对照

| 行为 | Pi | Codex |
|---|---|---|
| 启动 | `node server.mjs` | `codex app-server` |
| 握手 | bridge init message | `initialize` + `initialized` |
| 会话 | session 嵌入在 bridge 里 | 独立 `threadId` |
| 一次发送 | 写 `prompt` 帧，循环读事件 | `turn/start`，循环读消息 |
| 中断 | `abort` 控制帧 | `turn/interrupt` |
| 压缩 | 同步 `compact` 控制帧 | 异步 `thread/compact/start` + 等 `thread/compacted` |
| 工具 | client 注册的 custom tool 直接由 bridge 转发 | `item/tool/call` ServerRequest |
| 鉴权 | provider api_key（per provider）| `~/.codex` 登录态 / `CODEX_API_KEY` |

---

## 13.5 Codex app-server 协议参考

下面这部分基于 `codex-cli 0.133.0` 实测产出。**长期 single source of truth 是 `codex app-server generate-json-schema`**——不要把这一节当成规范，它只是当前快照，给实现者作脚手架。

### 13.5.1 选哪个子命令

Codex 暴露了三个候选 server，agent-bridge 选 `app-server`：

| 子命令 | 定位 | 是否合适 |
|---|---|---|
| `codex mcp-server` | 把 Codex 包成 MCP server，给别的 agent 调用 | 不合适——我们是用方 |
| `codex exec-server` | non-interactive 单次执行，默认 ws | 太薄，丢失 thread / approval / 流式 item |
| `codex app-server` | 完整应用层协议：thread + turn + 流事件 + 双向 approval | **就用这个** |

`app-server` 自我标注 experimental，但 schema 已经稳定输出，VS Code 扩展、TUI、`codex remote-control` 都基于它。

### 13.5.2 启动

```bash
codex app-server [--listen <URL>]
```

`--listen` 取值：

- `stdio://`（默认）— agent-bridge 选这个
- `unix://PATH`
- `ws://IP:PORT`（非 loopback 必须配 `--ws-auth`）
- `off`

另有 `codex app-server daemon` 长驻 + `codex app-server proxy` 走 unix socket，做多客户端共享。MVP 不需要。

### 13.5.3 协议总览

JSON-RPC 2.0，`v2` 是当前 schema（`v1` 仅留 `initialize` 与若干 ServerRequest）。生成命令：

```bash
codex app-server generate-json-schema --out ./schema   # JSON Schema
codex app-server generate-ts          --out ./bindings # TypeScript bindings
```

四类顶层消息：

- `ClientRequest` — client → server，需要 response（80+ 方法）
- `ClientNotification` — 仅 `initialized`
- `ServerRequest` — **server → client，client 必须回 response**（approval / elicitation / `item/tool/call`）
- `ServerNotification` — server → client，事件流（60+ 方法）

### 13.5.4 握手时序（实测）

```jsonc
// → C2S
{"jsonrpc":"2.0","id":1,"method":"initialize",
 "params":{"clientInfo":{"name":"agent-bridge","version":"0.0.1"},"capabilities":{}}}

// ← S2C result
{"id":1,"result":{"userAgent":"...","codexHome":"/home/.../.codex",
 "platformFamily":"unix","platformOs":"linux"}}

// → C2S notification
{"jsonrpc":"2.0","method":"initialized","params":{}}

// ← S2C 一些 notifications，例如 remoteControl/status/changed

// → C2S
{"jsonrpc":"2.0","id":2,"method":"thread/start","params":{"cwd":"/tmp"}}

// ← S2C：result + thread/started 通知 同时发
{"id":2,"result":{"thread":{"id":"019e638d-...","status":{"type":"idle"},...}}}
{"method":"thread/started","params":{"thread":{...}}}
```

注意：

- `thread/start` 不需要鉴权，**鉴权直到 `turn/start` 才生效**。
- result 和 `thread/started` 通知重复携带 thread payload — adapter 取一份即可，建议从 result 取。

### 13.5.5 关键方法目录（v2，节选）

**生命周期 / Thread**

- `initialize`（→ `initialized` notif）
- `thread/start` / `thread/resume` / `thread/fork` / `thread/archive` / `thread/unarchive`
- `thread/list` / `thread/loaded/list` / `thread/read` / `thread/rollback`
- `thread/inject_items` — 中途注入消息
- `thread/compact/start` —（异步）压缩，靠 `thread/compacted` 通知收尾
- `thread/name/set` / `thread/goal/set|get|clear` / `thread/metadata/update`
- `thread/unsubscribe`

**Turn**

- `turn/start`（必传 `threadId` + `input: UserInput[]`，可选 `model` / `effort` / `approvalPolicy` / `cwd` / `sandbox` / `summary` / `outputSchema` / `serviceTier`）
- `turn/steer` — 中途插话
- `turn/interrupt` —（对应公共 API 的 `abort()`）

**Server → Client request（必须回复，approval/工具的核心）**

- `item/commandExecution/requestApproval`
- `item/fileChange/requestApproval`
- `item/permissions/requestApproval`
- `item/tool/requestUserInput`
- `item/tool/call` — 客户端注入的 custom tool 被调用
- `mcpServer/elicitation/request`
- `attestation/generate`、`account/chatgptAuthTokens/refresh`
- `applyPatchApproval`、`execCommandApproval`（v1 兼容名，可能还在发）

**Server notification（事件流，进 mapper）**

- 文本/思考：`item/agentMessage/delta`、`item/reasoning/textDelta`、`item/reasoning/summaryTextDelta`、`item/reasoning/summaryPartAdded`
- Item 生命周期：`item/started`、`item/completed`、`item/autoApprovalReview/started|completed`
- 命令：`item/commandExecution/outputDelta`、`process/outputDelta`、`process/exited`、`item/commandExecution/terminalInteraction`
- 文件：`item/fileChange/outputDelta`、`item/fileChange/patchUpdated`、`turn/diff/updated`、`fs/changed`
- Turn：`turn/started`、`turn/completed`、`turn/plan/updated`
- Token：`thread/tokenUsage/updated`
- Thread：`thread/started`、`thread/closed`、`thread/archived`、`thread/compacted`、`thread/settings/updated`
- 错误/警告：`error`、`warning`、`guardianWarning`、`deprecationNotice`、`configWarning`
- Realtime（语音）：`thread/realtime/*` — agent-bridge 暂不映射

**辅助类（adapter 大概率不直接用）**

- 文件系统：`fs/readFile|writeFile|createDirectory|...`
- 命令执行（脱离 turn 的 host shell）：`command/exec`、`command/exec/write|terminate|resize`
- 配置：`config/read`、`config/value/write`、`config/batchWrite`
- 模型：`model/list`、`modelProvider/capabilities/read`
- 模糊文件查找：`fuzzyFileSearch`
- Plugin / Marketplace / Hooks / MCP server / Account / Feedback 一族

### 13.5.6 鉴权

- 主鉴权走 `codex login`，登录态写在 `~/.codex`，app-server 启动后自动复用。
- API key 直连：env `OPENAI_API_KEY` / `CODEX_API_KEY`，或经 `--config` 注入。
- agent-bridge `CodexAuth.api_key` 仅在 API key 模式有意义，且**优先级低于** `~/.codex` 中已有的登录态——这一点要在文档和报错里写清楚，避免使用方误以为传 `api_key` 就能切账号。

### 13.5.7 协议升级策略

`app-server` 仍标 experimental。agent-bridge 的稳健做法：

1. 锁定支持的 codex CLI 版本范围（例如 `>=0.133.0,<0.140.0`），写在 `CodexConfig.app_server_cmd` 的运行时检查里——不匹配就拒绝启动。
2. CI 跑 `codex app-server generate-json-schema`，diff 出方法/字段变化（见 §17 schema 同步）。
3. event mapper 对未知 method 走"安静丢弃 + 一次性 warn"，不抛异常。让协议增量演进而不阻塞用户。

---

## 14. 对外导出

`__init__.py` 只导出新接口。

```python
from .session import AgentSession
from .core.events import *
from .core.tools import CustomTool
from .core.capabilities import Capabilities

from .backends.pi.config import PiConfig, PiProvider, PiModel
from .backends.codex.config import CodexConfig, CodexAuth, SandboxConfig

__all__ = [
    "AgentSession",
    "PiConfig",
    "PiProvider",
    "PiModel",
    "CodexConfig",
    "CodexAuth",
    "SandboxConfig",
    "CustomTool",
    "Capabilities",
]
```

不再导出：

```text
PiSession
Provider
Model
ResponseEvent
```

这些名字容易把架构重新拉回 Pi-only。

---

## 15. 推荐迁移步骤

### 第一步：重建目录

先建新目录：

```text
core/
backends/pi/
backends/codex/
```

### 第二步：移动 Pi 逻辑

把现有内容拆开：

```text
session.py              -> backends/pi/adapter.py
types.py                -> core/events.py + core/tools.py
bridge/server.mjs       -> backends/pi/server.mjs
```

### 第三步：新增统一入口

实现：

```text
session.py -> AgentSession
core/adapter.py
core/transport.py
```

### 第四步：删掉旧命名

删除或重写：

```text
PiSession
Provider
Model
types.py
```

换成：

```text
AgentSession
PiConfig
PiProvider
PiModel
core/events.py
```

### 第五步：接 Codex MVP

前置：用户机器上 `codex` ≥ 0.133.0、并已 `codex login`（或导出 `OPENAI_API_KEY`）。

先只做：

- 跑 `codex app-server`（stdio）。
- `initialize` request + `initialized` notif 握手。
- `thread/start`（不传 resume）。
- `turn/start`（仅传 `threadId` + 单条 text input）。
- 消费 server 消息：把 `item/agentMessage/delta` → `TextDeltaEvent`，`turn/started` → `TurnStartEvent`，`turn/completed` → `TurnEndEvent`。
- `abort()` → `turn/interrupt`。
- `close()` → 关闭 transport。
- 服务端发任何 ServerRequest → 默认 `respond` 拒绝（保证不卡死）。

暂时不做：

- approval（`item/*/requestApproval` 仅默认拒绝，不接回调）。
- file diff、command event、plan event。
- thread/resume、thread/fork、inject_items、steer。
- compact。
- sandbox 细节（先用 `thread/start` 的默认）。
- custom tool（`item/tool/call` 也先默认拒绝）。

### 第六步：补 Codex 完整能力

按依赖顺序加：

1. token usage：`thread/tokenUsage/updated` → `TokenUsageEvent`。
2. reasoning：`item/reasoning/textDelta`、`item/reasoning/summaryTextDelta` → `ThinkingDeltaEvent` / `ReasoningSummaryDeltaEvent`。
3. command event：`item/started`(type=command_execution) + `item/commandExecution/outputDelta` + `item/completed` → `CommandStartEvent` / `CommandOutputDeltaEvent` / `CommandEndEvent`。
4. file change event：`item/fileChange/outputDelta` + `item/fileChange/patchUpdated` + `turn/diff/updated` → `FileChangeEvent` / `TurnDiffEvent`。
5. approval handler：把 ServerRequest 转 `ApprovalRequestEvent` 暴露给 session，配 `session.respond_to_approval()`。
6. sandbox：`thread/start` 传 `sandbox`，`turn/start` 允许 per-turn 覆盖。
7. resume / fork：`thread/resume` / `thread/fork`。
8. compact：`thread/compact/start` + 等 `thread/compacted`。
9. inject_items / steer。
10. plan event：`turn/plan/updated`、`item/plan/delta` → `PlanUpdateEvent`。
11. custom tool：注册 + `item/tool/call` 路由。
12. warning 一族：`warning` / `guardianWarning` / `deprecationNotice` / `configWarning` → `WarningEvent`。

每一步都要：单测 mapper、加到 `CODEX_CAPABILITIES` 对应字段、更新协议快照（见 §17）。

---

## 16. 最终架构

```text
AgentSession
  |
  |-- create_adapter("pi", PiConfig)
  |       |
  |       |-- PiAdapter
  |       |-- JsonlSubprocessTransport
  |       |-- Pi event mapper
  |       |-- backends/pi/server.mjs
  |
  |-- create_adapter("codex", CodexConfig)
          |
          |-- CodexAdapter
          |-- JsonlSubprocessTransport
          |-- CodexRpcClient
          |-- Codex event mapper
          |-- codex app-server
```

一句话：

> `AgentSession` 负责"统一使用"；`Adapter` 负责"后端怎么跑"；`Transport` 负责"怎么通信"；`EventMapper` 负责"怎么翻译事件"；`Config` 负责"后端自己的参数"。

---

## 17. Codex 协议 schema 同步

`codex app-server` 是 experimental，方法和字段可能在小版本之间变化。agent-bridge 不要手抄协议，而是**让生成器做单一事实来源**。

### 17.1 仓库内放一份快照

```text
agent_bridge/
  backends/
    codex/
      protocol/
        codex_app_server_protocol.v2.schemas.json   # 当前支持的 schema 快照
        VERSION                                      # 对应的 codex-cli 版本号
```

快照只用于：

- 文档与代码生成的输入。
- CI 做兼容 diff。

**绝不**在运行时校验消息——运行时按"宽容解析、未知字段忽略"原则处理。

### 17.2 更新流程

```bash
# 1. 用目标版本的 codex 生成新 schema 到临时目录
codex app-server generate-json-schema --out /tmp/codex-schema-new

# 2. diff 关键文件
diff -u agent_bridge/backends/codex/protocol/codex_app_server_protocol.v2.schemas.json \
        /tmp/codex-schema-new/codex_app_server_protocol.v2.schemas.json | head -200

# 3. 若 diff 可接受 → 覆盖 + 更新 VERSION + 改 mapper / capability
cp /tmp/codex-schema-new/codex_app_server_protocol.v2.schemas.json \
   agent_bridge/backends/codex/protocol/
codex --version > agent_bridge/backends/codex/protocol/VERSION
```

### 17.3 CI 闸门

写一个轻量脚本（例如 `scripts/check_codex_schema.py`）：

1. 解析仓库内 schema 快照，提取所有 `ClientRequest` / `ClientNotification` / `ServerRequest` / `ServerNotification` method 名。
2. 对照 `event_mapper.py` 中显式处理的方法列表。
3. 找出"schema 有但 mapper 没处理的 method" — 输出 warning 但**不阻断**（增量演进友好）。
4. 找出"mapper 处理了但 schema 没有的 method" — **报错**，意味着 codex 删了或改名了这个方法，必须人工处理。

CI 在 PR 阶段跑这个脚本，让协议漂移立刻可见。

### 17.4 版本兼容矩阵

`CodexAdapter.__init__` 启动后先查实际 codex 版本：

```python
def _check_compat(self):
    res = self.rpc.request("initialize", ...)
    cli_ver = parse_version(res.get("userAgent"))   # "agent-bridge/0.0.1 (... 0.133.0 ...)"
    snapshot_ver = read_protocol_snapshot_version()
    if not is_compatible(cli_ver, snapshot_ver):
        warnings.warn(
            f"codex-cli {cli_ver} 与 agent-bridge 内置协议快照 {snapshot_ver} 跨版本，"
            f"可能存在未识别的方法或字段。"
        )
```

不强行拦截 — Codex 的演进策略是加字段、加方法，旧 client 通常仍能跑，只是吃不到新能力。
