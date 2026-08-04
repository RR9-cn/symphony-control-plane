# 场景：调用外部服务

本场景用于新增或修改 FeignClient、外部服务 DTO、FallbackFactory 或 Service 中的远程调用。

## 初始必读

| 必读材料 | 用途 |
|---|---|
| `references/example-cards.md` | 快速确认 Feign/DTO/异常规则 |

## 条件读取

| 条件 | 再读取 |
|---|---|
| 新增或修改外部服务 Form/Result | `examples/dto-client.md` |
| 新增或修改 FeignClient / FallbackFactory | `examples/feign-client.md` |
| Service 中新增或调整远程调用编排 | `examples/service.md` |
| 需要新增远程异常类型或错误码 | `examples/exception.md` |
| 远程调用与 DB 写在同一业务流程 | `references/runtime-guardrails.md`、`examples/transaction-template.md` |
| 远程调用后发 MQ、写 Redis 或加分布式锁 | 对应读取 `examples/mq-producer.md` 或 `examples/redis-utils.md` |
| 需要对外暴露 RPC 契约 | `references/naming-placement.md`、`examples/controller.md` |

## 落地顺序

1. 在 `client.{子域}.dto` 定义 Form/Result，禁止复用 Request/Param 或使用 `Map`。
2. 定义 `XxxClient` 和 `XxxClientFallbackFactory`。
3. FallbackFactory 区分远端业务失败、网络超时、熔断和 5xx，转换为明确异常。
4. Service 在事务外调用 Feign；若必须和 DB 写编排，使用 `TransactionTemplate` 精确切分事务边界。
5. 远程调用后写缓存用 `RedisUtils`，加分布式锁用 `RedisLockTemplate`，均不得放入事务块。
6. 调用前后按日志规则记录关键上下文，敏感信息脱敏。

## 最小完成标准

- Feign 入参/返参是强类型 JavaBean 或 `List<JavaBean>`。
- 事务块内不调用 Feign。
- Fallback 不伪造成功结果。
- DTO 不跨层复用。
