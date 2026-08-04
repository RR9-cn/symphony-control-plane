# 场景：发送异步消息

本场景用于新增或修改 RocketMQ Producer、消息体 DTO、发送时机或提交后副作用。

## 初始必读

| 必读材料 | 用途 |
|---|---|
| `references/example-cards.md` | 快速确认 MQ、常量、事务规则 |

## 条件读取

| 条件 | 再读取 |
|---|---|
| 新增或修改消息体字段 | `examples/dto-mq.md` |
| 新增或修改 Producer 和发送方式 | `examples/mq-producer.md` |
| 新增 Topic/Tag 常量 | `examples/constant.md` |
| 发送依赖 DB 事务提交成功 | `references/runtime-guardrails.md`、`examples/transaction-template.md` |
| 消费端会落库或触发写操作 | `examples/idempotent-write.md` |
| 消息内容来自 Service DTO 转换 | `examples/bean-util.md` |
| 需要新增消费者配套逻辑 | `scenarios/consume-message.md` |

## 落地顺序

1. 定义轻量 Message，只包含业务 ID、事件时间和消费所需最小字段。
2. 定义 Topic/Tag 常量。
3. Producer 使用 `TransactionalMqSender` 发送消息，保留 traceId 透传能力。
4. 如果消息必须在 DB 提交后发送，由 `TransactionalMqSender` 自动判断事务并注册 `afterCommit`。
5. 消费端必须具备幂等能力；Producer 不把 Redis 去重当作唯一保障。

## 最小完成标准

- 事务内不使用同步 MQ。
- 消息不塞大对象。
- 发消息使用统一 helper。
- Topic/Tag 不散落硬编码。
