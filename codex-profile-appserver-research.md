# Codex CLI `--profile` 与 app-server provider 动态切换 — 调研结论

**日期：** 2026-05-27 | **环境：** Codex CLI v0.130.0 (Rust)

---

## 1. `--profile` 是什么

`--profile`（简写 `-p`）是 Codex CLI 的全局 flag，从 `~/.codex/config.toml` 中选取一个命名配置预设。本质是把多套配置写在一个文件里，一键切换。

```toml
# ~/.codex/config.toml
model = "gpt-5.5"
approval_policy = "on-request"

[profiles.ci]
approval_policy = "never"
sandbox_mode = "read-only"

[profiles.local]
model_provider = "ollama"
model = "codestral:22b"
```

```bash
codex --profile ci          # CI 模式
codex --profile local       # 本地开源模型
codex -p ci "fix the build"
```

---

## 2. 核心问题：profile/app-server 能否动态控制 provider？

场景：外部 wrapper 通过 app-server JSON-RPC 协议驱动 Codex，希望每个 session/thread 使用不同 provider。

### 2.1 `--profile` 启动 app-server：❌ 不工作

GitHub [issue #23417](https://github.com/openai/codex/issues/23417) 确认：`codex -p <profile> app-server` 启动后，profile 里的 `model_provider` **不会被传播到 thread/start**。同一 profile 在 `codex exec` 下正常工作，但 app-server 路径总是 `modelProvider: "openai"`。

### 2.2 `-c` 覆写启动 app-server：✅ 工作

本机实测验证：`-c`（config override）走不同代码路径，**确实能传进去**。

| 命令 | thread/start 返回的 modelProvider |
|------|------|
| `codex app-server -c model_provider="openai"` | ✅ openai |
| `codex app-server -c model_provider="cpa"` | ✅ cpa（自定义 provider，指向 CPA 网关） |

### 2.3 thread/start 协议参数：❌ 不能动态改

实测在 thread/start 的 JSON-RPC params 里传 `model_provider`，**被忽略**。provider 在 app-server 进程启动时锁定，所有 thread 共享。

| thread/start 参数 | 结果 |
|---|---|
| `{"model_provider": "cpa"}` | ❌ 忽略，仍用进程级默认 provider |
| `{"model": "gpt-4o"}` | ❌ 忽略 |

---

## 3. 当前能力矩阵

| 方式 | 能控制 provider？ | 粒度 |
|------|:--:|------|
| `codex -p <profile> exec ...` | ✅ | 单次命令 |
| `codex -p <profile> app-server` | ❌ bug #23417 | — |
| `codex app-server -c model_provider="X"` | ✅ | 进程级（所有 thread 共用） |
| `thread/start` 协议参数 | ❌ 不支持 | — |
| `thread/settings/update`（实验性） | ❓ 文档不明确 | — |

---

## 4. 给 wrapper 开发者的建议

### 方案 A：按 session 独立 spawn app-server 进程（最干净）

每个 session 起一个独立 app-server 进程，通过 `-c` 注入不同 provider。Rust 实现的 app-server 内存开销小，多进程可行。

```bash
# Session 1 → DeepSeek
codex app-server -c model_provider="deepseek" -c model="deepseek-chat"

# Session 2 → OpenAI
codex app-server -c model_provider="openai" -c model="gpt-5.5"
```

### 方案 B：统一走 AI 网关（最省资源）

所有 thread 走同一个 provider（如 CPA），网关层根据 model 名路由到不同后端。wrapper 只需一个 app-server 进程，切 model 即切 provider。

---

> **结论：** app-server 协议层目前**不支持** per-thread provider 切换。provider 是进程级配置。wrapper 若需要多 provider，推荐「多进程 spawn」或「AI 网关路由」两种方案。
