---
name: fskill-knowledge-query
description: "在本地工程知识缓存存在时检索 Current Ontology 与迭代归档溯源，为技术分析、技术方案、存量功能改造、接口或数据模型设计、故障原因追踪补充工程事实；缓存不存在或未覆盖问题时自动回退到实际代码、配置、数据库和迭代材料。用户要求做技术分析、了解现有系统能力/领域/业务规则/数据实体/接口/依赖，或询问某项设计由来和历史变化时使用。"
---

# 工程知识检索

## 目标

在分析代码和迭代材料时，把已有的本地知识缓存作为可选输入证据。知识缓存不存在、损坏或未覆盖当前问题时，不阻塞任务，直接查询实际代码实现、配置、数据库和迭代材料。

严格区分：

- Current Ontology：当前系统事实的唯一知识入口。
- Provenance 和 Archive：解释知识由哪次迭代引入、为什么变化，只用于历史追溯。

不得用旧 Release 或 Archive 中的结论替代、补全或覆盖 Current Ontology。

## 检索流程

### 1. 检查本地知识缓存

检查以下文件是否存在：

```text
.fshows/knowledge-staging/ontology/current.yaml
```

- 存在且可解析：继续检索当前 Release。
- 不存在、指向的 Release 不存在或内容损坏：跳过知识检索，按原任务流程查询实际代码、配置、数据库和迭代材料。

不要执行 `knowledge pull`、`knowledge bind` 或其他远端同步命令，也不要因为缺少知识缓存向用户索要绑定信息。

### 2. 定位当前 Release

读取：

```text
.fshows/knowledge-staging/ontology/current.yaml
```

只使用其中 `release` 指向的目录：

```text
.fshows/knowledge-staging/ontology/releases/<release>/
```

先读 `index.yaml` 获取导航信息。需要快速理解全局结构时读 `views/index.html`；只查询局部事实时优先读取对应 YAML 或 Markdown View，避免加载整个知识库。

### 3. 按问题检索

按以下顺序缩小范围：

1. 从 `index.yaml` 定位相关 Domain 和 Capability。
2. 读取 `views/domains/<domain>/README.md` 获取领域概览。
3. 读取 `views/domains/<domain>/capabilities/<capability>.md` 获取能力视图。
4. 回到 `objects/`、`facts/`、`links/` 中核对机器事实和关系。
5. 使用 `rg` 搜索业务名、对象 ID、表名、接口名或规则关键词；不得仅凭文件名推断结论。

技术分析至少检查与本次范围相关的：

- Domain、Capability 和跨域依赖；
- 当前有效的业务规则、状态约束和异常约束；
- DataEntity、API、Event 等对象；
- `reads`、`writes`、`calls`、`publishes`、`subscribes` 等关系；
- 与 PRD、DDL、接口文档、任务拆分或当前代码之间的冲突和缺口。

### 4. 按需追溯历史

只有问题涉及“为什么这样设计、由哪次需求引入、何时变化、原始材料是什么”时，读取当前 Release 下的：

```text
provenance/index.yaml
provenance/iterations/<iteration>.yaml
views/provenance/<iteration>.md
```

使用 Provenance 中的 Archive ID、远端路径和 Node ID 定位历史材料。Archive 只作为历史证据；判断现状时以实际代码实现为准。

若本地没有所需历史材料且当前工具无法读取对应远端 Node，明确列出 Archive ID 和 Node ID，请用户授权或提供材料，不得从旧摘要猜测。

## 输出要求

把命中的知识事实和来源路径自然并入当前分析，不单独输出知识检索报告。知识缓存不存在或未命中时无需说明，直接按原流程调研。知识结论与实际代码冲突时，明确指出差异，并以实际代码实现为准。

## Guardrails

- 禁止主动同步、绑定或修复知识库；只读取已经存在的本地缓存。
- 不得把本地缓存称为最新；只说明当前读取到的 Release ID。
- 缓存缺失、损坏或无相关知识时必须无阻塞回退到实际实现调研。
- 不修改 Release、Manifest、Current Pointer 或 Archive 文件。
- 不从 Archive 推断当前行为，不从旧 Release 补齐当前事实。
- 知识结论与当前代码冲突时，明确记录差异并以实际代码实现为准。
- 本 Skill 只负责本地知识检索与缺失回退，不替代技术分析、DDL、任务拆分、代码实现或知识归档 Skill。
