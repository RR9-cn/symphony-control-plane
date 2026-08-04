# Code Review Report

**时间**：<YYYY-MM-DD HH:mm:ss>
**分支**：<current-branch> vs <base-branch>
**diff 命令**：`git diff <base>...HEAD`
**commit 列表**：<fixed-point>..HEAD 的提交清单
**改动文件数**：N
**改动行数**：+X / -Y
**Spec 来源**：<自动识别到的 Spec 路径 / "无 Spec 可对照">
**规范来源**：`references/data-review-rules.md` + `references/business-code-review-rules.md` + fskill-code-java-guide（渐进式加载） + 项目本地 REF-1/REF-2（如有）
**启动 Reviewer**：<Data / BusinessCode>

---

## 分诊清单

| # | 文件 | 风险等级 | 所属轴 | 疑似问题 |
|---|------|---------|--------|----------|
| 1 | path/to/file.java | HIGH | Standards | 余额扣减逻辑缺少分布式锁保护 |
| 2 | path/to/other.java | MEDIUM | Spec | Spec 要求的回调通知未实现 |
| 3 | path/to/low.java | LOW | Standards | 命名不符合 §2.1 速查表 |

> 所属轴：Standards（规范违反）/ Spec（需求偏离）/ Both（两轴都涉及）

---

## Reviewer Matrix

| Reviewer | 是否启动 | 覆盖范围 | 触发原因 | 审查模板 |
|----------|----------|----------|----------|----------|
| Data | 是/否 | <SQL/Mapper/实体/事务/迁移相关文件> | <DDL/DML、索引、数据兼容等> | `references/data-review-rules.md` |
| BusinessCode | 是/否 | <Java/接口/消息/权限/业务链路相关文件> | <业务代码、接口契约、安全、Spec 相关改动> | `references/business-code-review-rules.md` |

---

## 专家审查原始发现

> 各 reviewer 的原始 Finding Schema 输出先记录在这里。主 Agent 归并去重后，再写入 Standards / Spec 两轴。无发现的 reviewer 写 `NO_FINDINGS` 并说明覆盖范围。

### Data Reviewer

<NO_FINDINGS 或 Finding Schema 列表>

### Business Code Reviewer

<NO_FINDINGS 或 Finding Schema 列表>

---

## Standards 轴审查结果

> 由主 Agent 从专家 reviewer 原始发现中归并去重后产出。逐文件/hunk 报告每处违反已文档化规范的改动，引用规范出处，区分硬违反（hard violation）与判断题（judgement call），跳过工具已强制的项。

### [HIGH] 文件名:行号 — 问题标题

- **Finding ID**：<CR-STD-001>
- **来源 Reviewer**：<Data / BusinessCode>
- **疑似问题**：<主 Agent 的初步判断>
- **规范出处**：<fskill-code-java-guide 的 example 文件 + 规则编号，或项目本地规范文件 + 章节>
- **违反类型**：hard violation / judgement call
- **深度研究结论**：<subagent 的确认/排除结论，仅 HIGH 项有>
- **证据**：<关键代码片段或调用链分析>
- **修复建议**：<如果确认为问题>
- **状态**：CONFIRMED / FALSE_POSITIVE / NEEDS_DISCUSSION

### [MEDIUM/LOW] 文件名 — 问题标题

- **Finding ID**：<CR-STD-002>
- **来源 Reviewer**：<Data / BusinessCode>
- **疑似问题**：<描述>
- **规范出处**：<引用>
- **违反类型**：hard violation / judgement call
- **状态**：CONFIRMED / FALSE_POSITIVE

（Standards 轴所有发现在此区域列完，不与 Spec 轴合并）

---

## Spec 轴审查结果

> 由主 Agent 从专家 reviewer 原始发现中归并去重后产出。三类发现：(a) Spec 要求但缺失或部分实现；(b) diff 中出现但 Spec 未要求的（scope creep）；(c) 看似实现但实现有误。每条引用 Spec 原文行。
>
> 如无 Spec 可对照，本区域写「无 Spec 可对照，Spec 轴跳过」。

### [HIGH] 文件名:行号 — 问题标题

- **Finding ID**：<CR-SPEC-001>
- **来源 Reviewer**：<BusinessCode / Data>
- **发现类型**：缺失实现 / 部分实现 / scope creep / 实现有误
- **Spec 原文引用**：<引用 Spec 中对应的需求行>
- **疑似问题**：<主 Agent 的初步判断>
- **深度研究结论**：<subagent 的确认/排除结论，仅 HIGH 项有>
- **证据**：<关键代码片段或调用链分析>
- **修复建议**：<如果确认为问题>
- **状态**：CONFIRMED / FALSE_POSITIVE / NEEDS_DISCUSSION

### [MEDIUM/LOW] 文件名 — 问题标题

- **Finding ID**：<CR-SPEC-002>
- **来源 Reviewer**：<BusinessCode / Data>
- **发现类型**：<类型>
- **Spec 原文引用**：<引用>
- **状态**：CONFIRMED / FALSE_POSITIVE

（Spec 轴所有发现在此区域列完，不与 Standards 轴合并）

---

## 汇总

- **总扫描项**：N（HIGH: X, MEDIUM: Y, LOW: Z）
- **深度研究项**：M
- **确认问题**：A（Critical: p, Warning: q）
- **排除误报**：B
- **整体风险评级**：<可合入 / 需修复后合入 / 建议重新设计>

### Reviewer 覆盖小结

- 启动 Reviewer：<Data / BusinessCode>
- 覆盖文件：<N 个>
- 未覆盖原因：<如某 reviewer 未启动，说明原因>

### 跨 Reviewer 归并记录

- <canonical finding id>：合并 <Data/BusinessCode> 的重复发现，原因 <同一文件/同一调用链/同一业务风险>
- <冲突项>：<如 reviewer 严重级别或结论冲突，记录主 Agent 仲裁原因>

### Standards 轴小结

- 发现总数：<X>
- 最严重问题：<描述，若无写「无」>

### Spec 轴小结

- 发现总数：<X>
- 最严重问题：<描述，若无写「无」>

> 不跨轴选唯一最严重项——两轴分离的目的就是防止一轴掩盖另一轴。

### 必须修复项

（列出所有 CONFIRMED 的 HIGH 和 Critical 问题，附修复建议摘要）

### 规范关联分析

**数据规范模板命中**：
- <references/data-review-rules.md#分类 — 简述违反内容>

**业务代码规范模板命中**：
- <references/business-code-review-rules.md#分类 — 简述违反内容>

**fskill-code-java-guide 命中**：
- <example 文件 / 规则编号 — 简述违反内容>

**REF-1（项目本地高风险模式库）命中**：
- <RP-xx 模式 1>
- <RP-xx 模式 2>

（如 REF-1 不存在则写「REF-1 文档不存在，使用内置框架」）

**REF-2（项目本地规范）违反项**：
- <规范文档名 / 章节 — 简述违反内容>

（仅列出本次改动违反的具体规范条款，无则写「无」）
