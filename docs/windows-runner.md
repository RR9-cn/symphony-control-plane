# Windows 原生 Symphony Runner

第三步主链采用 Python 实现语言无关 Symphony SPEC 的 Windows 原生运行层，不依赖 WSL、Elixir 或 Mix。Runner 直接在每个 WorkItem 工作区启动 Windows 版 `codex app-server`，通过默认 stdio JSONL 协议驱动 Thread 和 Turn。

## 架构

```text
SQLite / FastAPI Control Plane
        ↑ Bearer + Claim Token
WindowsSymphony
├── ControlPlaneTracker
├── WorkspaceManager（PowerShell Hooks）
└── CodexAppServer（Windows 子进程）
        ↕ JSONL over stdio
    codex app-server
```

Control Plane Bearer Token 和 WorkItem Claim Token 只由 Runner 持有。启动 Codex 子进程前会删除 `ACP_API_TOKEN`、`CONTROL_PLANE_TOKEN` 和配置引用的 Token 环境变量。

## 前置条件

- Windows 10/11；
- Python 3.11 或更高版本；
- Windows 版 Codex CLI；
- PowerShell 5.1 或 PowerShell 7；
- 目标仓库所需的 Git、构建和测试工具。

确认 Codex 已安装和登录：

```powershell
codex --version
codex login status
codex app-server --help
```

Codex App Server 协议说明见 [OpenAI Codex App Server](https://learn.chatgpt.com/docs/app-server)。

## 安装

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
```

复制并修改示例：

```powershell
Copy-Item WORKFLOW.windows.example.md WORKFLOW.md
```

`WORKFLOW.md` 使用 Symphony SPEC 的 YAML Front Matter 和 Liquid Prompt。配置读取严格模式：缺少环境变量、未知 Prompt 变量、不安全 HTTP 地址和越界参数都会在启动前失败。

## 启动

先启动 SQLite 控制面：

```powershell
$env:PYTHONPATH = "src"
$env:ACP_API_TOKEN = "replace-with-a-random-host-only-token"
alembic upgrade head
python -m uvicorn control_plane.app:app --host 127.0.0.1 --port 8080
```

另开 PowerShell 启动 Runner：

```powershell
$env:PYTHONPATH = "src"
$env:CONTROL_PLANE_TOKEN = $env:ACP_API_TOKEN
$env:SYMPHONY_WORKER_ID = "windows-symphony-01"
python -m symphony_windows .\WORKFLOW.md
```

只执行一次轮询并等待该批任务结束：

```powershell
python -m symphony_windows .\WORKFLOW.md --once
```

安装项目后也可使用入口命令：

```powershell
fshows-symphony-windows .\WORKFLOW.md
```

## 调度行为

每个 Poll Tick 执行：

1. 触发 Lease/Retry 维护；
2. 查询依赖已满足的 `Ready` WorkItem；
3. 按优先级、创建时间和 ID 排序；
4. 在全局并发槽位内执行原子 Claim；
5. 创建经过 Windows 保留名、路径穿越和根目录边界校验的工作区；
6. 执行 `after_create`、`before_run` PowerShell Hook；
7. 启动独立 `codex app-server` 并注册六个受限工具；
8. 后台 Heartbeat，Claim 丢失时终止 Codex 进程树；
9. 完成、Blocked 或 NeedsHuman 时清除本机 Claim；
10. 普通 Turn 结束但未交接时安排 1 秒 continuation，异常按 10 秒指数退避；
11. 执行 `after_run` Hook，保留工作区供下一次尝试复用。

## 安全边界

- Agent Tool 始终绑定当前 WorkItem，不接受 `work_item_id`；
- Agent Tool Schema 和输出不包含 Claim Token；
- 不暴露任意 HTTP 工具；
- 非 loopback HTTP Control Plane 地址必须显式允许，生产部署优先 HTTPS；
- PowerShell Hooks 来自仓库拥有的 `WORKFLOW.md`，应只在可信仓库运行；
- 默认审批策略拒绝沙箱提升、规则绕过和 MCP elicitation，并将需要输入的任务转换为 `NeedsHuman`。

当前第三步实现覆盖核心轮询、并发、Claim、Heartbeat、工作区、Hook、Codex stdio、动态工具和失败退避。SPEC 中的配置热更新、终态工作区清理、运行状态 HTTP Dashboard、跨主机 Worker 和第四步 Agent Profile Router 不在本阶段范围。

## 验收

```powershell
python -m pytest -q
python scripts/validate_protocol.py
ruff check src tests scripts
```

自动化测试使用假 Codex App Server 验证真实子进程 JSONL 往返，不产生模型调用或 API 费用。
