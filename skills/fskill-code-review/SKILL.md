---
name: "fskill-code-review"
description: "基于 Git diff 的三 Agent 代码审查。用于 code review、代码审查、提交前检查、合入前风险检查、CR、帮我看看改动。主 Agent 负责分诊、归并、定级和汇总；Data Reviewer 与 Business Code Reviewer 两个 sub-agent 分别按数据规范模板和业务代码规范模板并行审查；最终按 Standards（规范符合度）和 Spec（需求符合度）两轴呈现；Java 代码渐进式读取 fskill-code-java-guide。"
---

# Code Review

固定三 Agent 执行：

- 主 Agent：预检、分诊、sub-agent 调度、归并去重、定级、落报告。
- Data Reviewer sub-agent：按数据规范模板审查数据层改动。
- Business Code Reviewer sub-agent：按业务代码规范模板审查业务代码、接口契约、安全和需求语义。

结果仍按 Standards 轴（规范符合度）和 Spec 轴（需求符合度）分开呈现。一轴通过不代表另一轴通过。sub-agent 不做最终裁决。

## 引用来源

| 引用 | 来源 | 读取时机 |
|------|------|----------|
| REF-G | `fskill-code-java-guide` skill | Java 相关改动时渐进式读取；报告只引用 example 文件名 + 规则编号 |
| REF-1 | 项目本地 `docs/code-review-risk-patterns.md` | 存在时读取 RP-xx 快速索引；不存在则用内置风险框架 |
| REF-2 | 项目本地 `docs/coding-standards/README.md` | 存在时按需读取项目本地规范 |

不要把 REF-G 的规范正文复制到本 Skill 或报告里。

## 输入收集

先执行：

```powershell
fshows --json workflow status
```

用 `data.current.featureRoot` 作为报告目录父目录和 Spec 自动识别来源；失败则回退到 `docs/reviews/`。

收集以下输入：

- 基准分支：默认自动检测 `main`/`master`，也支持 commit SHA / tag / `HEAD~5`。
- 需求名称：用于报告目录命名；缺省时用分支名转换。
- 需求文档：优先自动识别；识别失败再询问。
- Review 范围：默认全部改动，也可限定模块或文件列表。

## 执行流程

### 阶段 1：预检、分诊、初始化

执行固定点预检；坏 ref 或空 diff 直接停止：

```bash
git rev-parse <base-branch>
git diff <base-branch>...HEAD --stat
git log <base-branch>..HEAD --oneline
git branch --show-current
```

使用三点 diff（`...`）对比 merge-base 与 HEAD。记录文件数和行数；超过 30 个文件时建议缩小范围或分批 review。

按优先级自动识别 Spec：

1. 从 `git log <base>..HEAD` 提取 `#123`、`Closes #45`、云效工作项 ID、`PRD: https://...` 等引用。
2. 查找 `<featureRoot>/prd/original.md`。
3. 在 `docs/specs/`、`.scratch/`、`docs/` 搜索匹配分支名或需求名的文件。
4. 仍失败则询问用户；用户明确没有时跳过 Spec 轴。

识别到的 Spec 候选必须给用户确认，不要直接当作权威 Spec。

Java 改动的 REF-G 加载规则：

- 基线：有 `backend/` 下 `.java`、`*Mapper.xml`、`application*.yml`、`bootstrap.yml` 时，读取 REF-G §1 总则 + §2 速查表。
- 按组件：扫描每个 Java 文件时，按 Controller / Service / Mapper / FeignClient / MQ / Redis 等组件读取 REF-G §3 对应 examples。
- 端到端：diff 跨多组件时，按 REF-G §3.2 场景表读取相关 scenarios。
- 最终核对：阶段 5 使用 REF-G §4 Checklist。

逐文件读取 diff：

```bash
git diff <base-branch>...HEAD -- <file-path>
```

给每个文件标注风险等级和初始轴：

- HIGH：命中高风险区域且缺乏防御措施。
- MEDIUM：命中高风险区域但已有部分防御，或低风险但影响面大。
- LOW：命名、注释、格式、简单 CRUD、已有充分防御。
- Standards：命中 REF-G/REF-2 规范条款。
- Spec：偏离 Spec 预期。
- Both：规范和需求都涉及。

生成 Reviewer Matrix：

| Reviewer | 启动信号 | 固定职责 | 审查模板 |
|----------|----------|----------|----------|
| Data Reviewer | DDL/DML、`*Mapper.xml`、实体字段、索引、事务、数据迁移 | 表结构兼容、SQL 正确性/性能、索引、事务边界、历史数据兼容 | 读取 `references/data-review-rules.md`，把命中的数据规范分类传入 prompt |
| Business Code Reviewer | `.java`、Java 配置、Controller/DTO/VO/Feign/MQ/Redis、权限、安全、Spec 相关业务链路 | Java 规范、业务语义、接口契约、状态机、幂等、异常、安全权限 | 读取 `references/business-code-review-rules.md`，把命中的业务代码规范分类传入 prompt |

只允许这两个 sub-agent。至少启动一个；若两类改动都命中，则两个并行启动。无 Spec 时，Business Code Reviewer 只能审业务不变量和 diff 自洽性，不得输出 Spec fail。

创建 `<featureRoot>/reviews/<YYYYMMDD>_<需求名称>/report.md`；featureRoot 不可用时回退 `docs/reviews/`。从 `template/report-template.md` 复制并填充报告头、分诊清单、Reviewer Matrix。

进入阶段 2 前，向用户展示 HIGH 项、Reviewer Matrix、Spec 候选，确认是否调整。

### 阶段 2：双 sub-agent 并行审查

在同一轮并行启动 Reviewer Matrix 中命中的 sub-agent。最多两个：Data Reviewer 和 Business Code Reviewer。每个 sub-agent 只审自己的固定职责。

所有 reviewer prompt 必须包含：

- diff 命令、commit 列表、负责文件/风险清单。
- Spec 来源和内容；无 Spec 时明确说明。
- 已加载的本 reviewer 审查模板分类、REF-G examples 清单、REF-1/REF-2 路径或缺失状态。
- 输出要求：使用统一 Finding Schema；无发现输出 `NO_FINDINGS` 并说明覆盖范围。
- 输出限制：≤600 字，只报告职责内风险。

统一 Finding Schema：

```yaml
- reviewer: Data | BusinessCode
  file: path/to/file
  line: 123
  axis: Standards | Spec | Both
  severity: HIGH | MEDIUM | LOW
  type: hard violation | judgement call | missing implementation | partial implementation | scope creep | wrong implementation | compatibility | security
  evidence: "关键证据"
  reference: "规范出处或 Spec 原文行；无则写 none"
  recommendation: "修复建议"
  confidence: high | medium | low
```

Reviewer 重点和模板：

- Data Reviewer：按 `references/data-review-rules.md` 审历史数据、并发写、慢查询、字段默认值、DDL 兼容、事务边界。
- Business Code Reviewer：按 `references/business-code-review-rules.md` 审 Java 分层、异常、幂等、事务、MQ/Redis/Feign、业务语义、Spec 偏差、API/RPC/MQ 契约、鉴权、越权、敏感日志和密钥；Java 细则引用 REF-G。

把 reviewer 返回内容先写入 `report.md` 的「专家审查原始发现」区域，不直接写 Standards / Spec 轴。

### 阶段 3：归并、去重、定级

主 Agent 处理所有 reviewer 输出：

1. 规范化不完整 Finding Schema；无法定位文件/行号的降级为 NEEDS_DISCUSSION 候选。
2. 合并同一文件、同一调用链、同一业务风险的重复发现，保留所有来源 reviewer 和证据。
3. 将 finding 映射到 Standards / Spec / Both；Both 同时写入两轴，使用同一个 canonical finding id。
4. 复核 severity；上调或下调必须写明原因。
5. 初判状态：CONFIRMED、FALSE_POSITIVE、NEEDS_DISCUSSION。

归并后的 findings 写入 `report.md` 的 Standards 轴和 Spec 轴。最终报告不按 reviewer 分组呈现，但每条 finding 保留 `来源 Reviewer`。

### 阶段 4：HIGH 风险深入

仅对归并后的 HIGH 级硬违反、高置信需求偏差、Critical 安全风险启动深入 sub-agent。MEDIUM 由主 Agent 自查；LOW 不投入额外 sub-agent。

一次只调查一个风险点。prompt 包含：

- canonical finding id、文件、行号、风险等级、所属轴、来源 reviewer。
- 归并后的发现描述。
- Standards 关联：REF-G example + 规则编号，或 REF-1 RP-xx / REF-2 章节。
- Spec 关联：Spec 原文行。
- 需求背景。
- 调查要求：读完整上下文、追调用链、检查防御措施、确认风险是否真实。
- 输出：CONFIRMED / FALSE_POSITIVE / NEEDS_DISCUSSION + 证据 + 修复建议。

返回后立即追加到对应轴的 HIGH 条目下。单风险点最多 1 轮 sub-agent；无法确认则 NEEDS_DISCUSSION。

### 阶段 5：汇总

读取 `report.md` 并追加汇总。Standards 和 Spec 两轴保持独立，不跨轴选唯一最严重项。

汇总必须包含：

- 总扫描项、深度研究项、确认问题、排除误报、整体风险评级。
- Reviewer 覆盖小结：启动了哪些 sub-agent，覆盖了哪些文件/风险。
- 跨 reviewer 重复/冲突处理。
- Standards 轴小结：发现总数、最严重问题。
- Spec 轴小结：发现总数、最严重问题。
- 必须修复项：所有 CONFIRMED 的 HIGH 和 Critical。
- 规范关联分析：数据规范模板、业务代码规范模板、REF-G、REF-1、REF-2 命中情况。

同时在对话中输出摘要。

## 风险判断框架

优先使用项目本地 REF-1。REF-1 不存在或未覆盖时：

- 数据层风险按 `references/data-review-rules.md` 判断。
- 业务代码、接口契约、安全风险按 `references/business-code-review-rules.md` 判断。
- Java 规范细则按 REF-G 判断。
- 项目专属规范按 REF-2 判断。

防御措施可降低风险等级，但必须检查是否充分；具体降级条件以对应审查模板分类为准。

判定规则：

```text
命中高风险区域？
├── 否 → 是否架构/分层违规？
│   ├── 是 → MEDIUM，主 Agent 自查
│   └── 否 → LOW，快速记录
└── 是 → 防御措施是否充分？
    ├── 充分 → MEDIUM 或 LOW
    ├── 部分充分 → MEDIUM，主 Agent 自查
    └── 不充分 → HIGH，进入深入 sub-agent
```

## 护栏

- Review 全程只读，不修改源代码。
- 修复建议必须经用户确认后才执行。
- diff 超过 30 个文件时建议缩小范围或分批。
- LOW 禁止启动 sub-agent。
- 单风险点最多 1 轮深入 sub-agent。
- 最终报告按 Standards / Spec 两轴呈现；sub-agent 只是专家执行维度。
- 不复制 REF-G 规范正文，只引用 example 文件名和规则编号。
