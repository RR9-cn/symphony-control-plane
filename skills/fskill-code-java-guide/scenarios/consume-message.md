# 场景：消费 MQ 消息

本场景用于新增或修改 RocketMQ Consumer、消费幂等、失败重试和消费后落库。

## 初始必读

| 必读材料 | 用途 |
|---|---|
| `references/example-cards.md` | 快速确认 MQ Consumer、幂等、日志规则 |

## 条件读取

| 条件 | 再读取 |
|---|---|
| 新增或修改消息体字段 | `examples/dto-mq.md` |
| 新增或修改 Consumer 写法和重试规则 | `examples/mq-consumer.md` |
| 消费后调用 Service 编排 | `examples/service.md` |
| 消费后写 DB 或状态流转 | `references/runtime-guardrails.md`、`examples/idempotent-write.md` |
| 使用 Redis 做入口去重 | `examples/redis-utils.md`、`examples/constant.md` |
| 消费后需要分布式锁串行化 | `examples/redis-utils.md`、`examples/idempotent-write.md` |
| 需要新增 Mapper/Repository | `examples/mapper.md`、`examples/repository.md` |
| 消费失败需要业务异常或错误码 | `examples/exception.md` |

## 落地顺序

1. Consumer 只负责解析消息、打日志、做轻量入口校验并调用 Service。
2. 真正业务写入和幂等判断放 Service。
3. 幂等写使用事务内 `FOR UPDATE` + 状态判断；Redis 去重只做辅助。
4. 若消费后需要跨进程串行化，用 `RedisLockTemplate` 在事务外加锁，再调用独立事务方法。
5. 系统异常向外抛出，让 MQ 框架触发重试；禁止吞异常。
6. 消费入口、重复消费命中、失败异常都有日志。

## 最小完成标准

- Consumer 不直接写 Mapper。
- 失败不静默吞掉。
- 写操作幂等有 DB 兜底。
- 消息日志包含 topic/tag 和业务 ID。
