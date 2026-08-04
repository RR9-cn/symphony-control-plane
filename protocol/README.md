# 调度协议 v1

本目录是 Fshows Agent Control Plane 第一个版本的协议真相源。后端、Symphony Adapter、Agent Profile 和 Skill 必须引用这里的机器可读定义，不得各自复制枚举或发明别名。

## 固定内容

- `agent-roles.yaml`：五种 Agent Role 的权限、Skill、沙箱和退出契约。
- `state-machine.yaml`：状态枚举、转换事件、执行主体和守卫条件。
- `artifact-layout.yaml`：Feature 产物的唯一规范路径。
- `schemas/work-item.schema.json`：WorkItem Draft 2020-12 JSON Schema，同样适用于解析后的 YAML。
- `schemas/handoff.schema.json`：Handoff Draft 2020-12 JSON Schema，同样适用于解析后的 YAML。
- `examples/FEATURE-001/`：包含异常恢复分支的完整五角色人工推演。

## 规范约定

1. 协议版本从整数 `1` 开始；不兼容修改必须增加 `schema_version`。
2. 枚举、字段名和持久化值使用小写 `snake_case`；界面展示名不属于协议。
3. `stage` 表示业务阶段，`status` 表示调度状态，二者不可混用。
4. WorkItem 是状态与 claim 的真相源；Handoff 是一次 Agent 尝试的结构化结果，不直接改变状态。
5. 所有路径都是相对 `<featureRoot>` 的 POSIX 路径，区分大小写，禁止绝对路径和 `..`。
6. Agent 之间只通过已记录 revision、Artifact、Handoff 和 WorkItem 事件交接，不依赖临时 workspace。
7. `git push`、合并、发布、外部批量写入和删除始终需要独立的人工作业授权，不能由 Role 的普通写权限隐式获得。

## 本地验收

在仓库根目录运行：

```powershell
python scripts/validate_protocol.py
```

校验器会验证 Schema、角色与阶段映射、状态转换、依赖有向无环、claim 不变量、Handoff 对应关系、产物路径，以及虚拟 Feature 是否覆盖五个角色和四种恢复路径。
