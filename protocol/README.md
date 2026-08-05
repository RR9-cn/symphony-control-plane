# Symphony Issue Protocol v2

调度单位只有 `Issue`。一个 Issue 由一个通用 Coding Agent 在一个持久 Workspace 中完成分析、实现与验证，不再拆成 Feature、WorkItem、Role 或 Stage。

Issue 对调度器暴露稳定的标准字段：不透明 `id`、人类可读且唯一的 `identifier`、归一化 `state`、小写 `labels`、`blocked_by`、非敏感 `native_ref` 和 Adapter 派生的 `dispatchable`。调度器而不是 Tracker 查询端负责最终的状态、标签、路由和并发过滤。

一次领取创建一个 `Attempt`。正常执行可在同一 Attempt、同一 Codex Thread 和同一 Lease 中连续运行多个 Turn；Turn 完成不改变 Issue 状态。只有以下语义事件结束运行：

- `issue_complete`：进入 `reviewing`，等待一次最终人工验收；
- `issue_request_human`：进入 `needs_human`，答复后回到 `ready` 并恢复原 Thread；
- `issue_block`：进入 `blocked`；
- Runner 异常或达到单进程 Turn 上限：进入短暂 `retry_queued`，复用 Workspace 与 Thread 继续。

人工验收后依次执行本地 Commit、授权 Push 并创建 GitHub PR/GitLab MR、确认 Review Request 已合并。只有合并后 Issue 才进入 `done`。Agent 没有 Push、创建 PR/MR 或合并权限。

协议文件：

- `schemas/issue.schema.json`：Issue 输入模型；
- `state-machine.yaml`：Issue 生命周期；
- `artifact-layout.yaml`：可选 Artifact 路径规则。
