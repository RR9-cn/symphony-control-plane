# Business Code Review Rules

用于 Business Code Reviewer sub-agent。只在 diff 命中业务代码、接口契约、消息/RPC、权限、安全、状态流转、跨层业务链路时读取。

## 审查目标

检查业务代码是否满足编码规范、需求语义、接口契约、安全边界和运行时可靠性。发现必须映射到 `Standards`、`Spec` 或 `Both`。

## 通用必查项

| 分类 | 检查点 | HIGH 信号 |
|------|--------|-----------|
| 需求语义 | Spec 要求、边界条件、异常路径、scope creep | Spec 要求缺失；实现语义相反；新增需求外副作用 |
| 分层职责 | Controller、Service、Mapper、Client、DTO 边界 | Controller 写复杂业务；Mapper 承载业务判断；跨层直接依赖 |
| 状态机 | 状态转换、终态、中间态、超时、并发 | 非法状态可流转；终态可回退；并发状态更新无防护 |
| 幂等 | 提交、回调、MQ 消费、重试 | 重试导致重复写/重复扣减；幂等键不覆盖业务唯一性 |
| 异常处理 | catch、日志、降级、返回码 | 吞异常；丢堆栈；错误码语义变化；失败后状态不一致 |
| 外部调用 | RPC、Feign、HTTP、MQ、Redis | 事务内外部调用；无超时/降级；消息发送和 DB 状态不一致 |
| API 契约 | 入参、出参、必填、错误码、兼容 | 删除响应字段；改字段类型；新增必填无默认；错误码破坏兼容 |
| 安全权限 | 鉴权、越权、输入校验、敏感日志 | 跳过权限校验；用户输入拼接；敏感信息明文日志/返回 |

## 审查范围

- Java 实现：优先检查分层、异常、事务、幂等、MQ/Redis/Feign 使用；Java 规范细节以 `fskill-code-java-guide` 为准。
- 业务语义：优先对照 Spec 检查缺失实现、部分实现、scope creep、实现有误；无 Spec 时只检查业务不变量和 diff 自洽性。
- 接口契约：优先检查 API/RPC/MQ 契约兼容、字段语义、错误码、上下游影响。
- 安全权限：优先检查鉴权、越权、注入、敏感日志、密钥和审计风险；可直接标 HIGH。

## 降级条件

命中高风险区域但存在充分防御时可降级：

- 状态流转有完整前置状态、终态保护和并发防护。
- 提交/回调/MQ 消费有稳定幂等键和重复处理分支。
- 外部调用有超时、降级、重试边界，并且不破坏事务一致性。
- API 变更有兼容字段、默认值、版本化或上下游同步证据。
- 权限和输入校验在入口层集中执行，并覆盖对象级权限。

## 输出要求

按统一 Finding Schema 输出。`reference` 写本文件规则分类；Java 规范问题同时引用 REF-G example + 规则编号。例如：

```yaml
- reviewer: BusinessCode
  file: backend/.../TaskServiceImpl.java
  line: 88
  axis: Spec
  severity: HIGH
  type: missing implementation
  evidence: "Spec 要求审批拒绝后通知发起人，但 reject 分支只更新状态"
  reference: "Spec L42；references/business-code-review-rules.md#需求语义"
  recommendation: "在拒绝分支补充通知发送或说明由异步链路承担，并补充失败处理"
  confidence: high
```

无发现时输出：

```text
NO_FINDINGS: 覆盖 <文件列表/范围>，未发现业务代码规范、需求语义、契约或安全风险。
```
