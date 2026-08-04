# Symphony Elixir Adapter

本目录是针对参考实现 OpenAI Symphony Elixir `0.0.2` 的源码叠加层，基于本机参考仓库当前 Tracker 契约编写。

## 安装到 Symphony Fork

从本项目根目录将三个模块复制到 Symphony Fork：

```powershell
$controlPlaneRoot = "D:\path\to\fshows-agent-control-plane"
$symphonyRoot = "D:\path\to\symphony"
$source = Join-Path $controlPlaneRoot "integrations\symphony_elixir\lib\symphony_elixir\fshows_control_plane"
$target = Join-Path $symphonyRoot "elixir\lib\symphony_elixir\fshows_control_plane"
New-Item -ItemType Directory -Force -Path $target
Copy-Item -Path "$source\*.ex" -Destination $target
git -C $symphonyRoot apply (Join-Path $controlPlaneRoot "integrations\symphony_elixir\patches\tracker-registry.patch")
```

测试文件应复制到 Symphony 的 `elixir/test/symphony_elixir/`，然后按照其 `AGENTS.md` 运行 `mix specs.check`、目标测试和完整 `make all`。

## WORKFLOW 配置

```yaml
tracker:
  kind: fshows_control_plane
  provider:
    endpoint: http://127.0.0.1:8080
    token: $CONTROL_PLANE_TOKEN
    worker_id: $SYMPHONY_WORKER_ID
    lease_seconds: 300
    # 非 loopback HTTP 地址必须显式开启；生产网络优先使用 HTTPS。
    # allow_insecure_http: true
  required_labels: []
  active_states:
    - ready
    - running
  terminal_states:
    - done
    - cancelled
```

Control Plane 同时配置相同 token：

```powershell
$env:ACP_API_TOKEN = "..."
$env:CONTROL_PLANE_TOKEN = $env:ACP_API_TOKEN
$env:SYMPHONY_WORKER_ID = "symphony-01"
```

`CONTROL_PLANE_TOKEN` 会由 `secret_environment_names/1` 标记为宿主秘密，从 Codex 子进程环境中移除。Claim token 只保存在 Symphony 宿主 ETS 中，不会出现在 Issue、Prompt、工具参数或环境变量中。

## Claim 生命周期

```text
fetch_issues_by_states(ready)
  → 只读取 candidates

Orchestrator dispatch 前 fetch_issues_by_ids(id)
  → Ready: 原子 claim，成功才标记 dispatchable
  → Running + 本机 token: heartbeat
  → Running + 无本机 token: dispatchable=false

Agent 动态工具
  → 宿主从 ETS 取 token
  → 完成、阻塞或人工确认后清除 claim

Symphony 进程退出
  → ETS token 消失
  → Control Plane Lease 到期后回收
```

只暴露六个受限工具，不暴露通用 HTTP：`work_item_get`、`work_item_add_event`、`work_item_add_artifact`、`work_item_request_human`、`work_item_complete`、`work_item_block`。工具始终绑定当前 Issue ID，Agent 不能传入其他 WorkItem ID，也看不到 claim token。
