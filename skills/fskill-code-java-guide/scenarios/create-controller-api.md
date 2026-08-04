# 场景：从零写一个 Controller 接口

本场景用于新增或重写一个后端接口。不要批量读取全部 examples；先按本文件判断会实际创建/修改哪些文件。

## 初始必读

| 必读材料 | 用途 |
|---|---|
| `references/naming-placement.md` | 确定 Controller、Service、DTO、DAO 包路径和命名 |
| `references/example-cards.md` | 快速确认涉及组件 |

## 条件读取

| 条件 | 再读取 |
|---|---|
| 从零写 Controller 或新增 RPC/HTTP 入口 | `examples/controller.md` |
| 新增 API Request/Response | `examples/dto-web-api.md` |
| 新增 Service 接口/实现或改编排逻辑 | `examples/service.md` |
| 新增 Service Param/Model | `examples/dto-service.md` |
| 需要对象转换或字段映射 | `examples/bean-util.md`，必要时读 `examples/manager.md` |
| 新增 DB 表实体、Mapper、Repository | `examples/entity.md`、`examples/mapper.md`、`examples/repository.md` |
| 涉及多步 DB 写、提交后副作用或事务边界 | `references/runtime-guardrails.md`、`examples/transaction-template.md` |
| 写操作需要幂等 | `examples/idempotent-write.md` |
| 调外部服务或新增 FeignClient | `examples/dto-client.md`、`examples/feign-client.md` |
| 发 MQ 或异步副作用 | `examples/dto-mq.md`、`examples/mq-producer.md` |
| 使用 Redis 缓存、分布式锁或入口去重 | `examples/redis-utils.md`、`examples/constant.md` |
| 新增业务枚举、常量、异常 | `examples/enum.md`、`examples/constant.md`、`examples/exception.md` |

## 落地顺序

1. 先确认接口是对外 HTTP 还是对内 RPC；对外放 `web.controller.external`，对内放 `web.controller.internal` 并实现 `api.XxxApi`。
2. 定义 API 层 Request/Response；格式校验放 Request，业务规则校验放 Service。
3. 定义 Service 接口和实现；Controller 只注入 Service 接口。
4. 若涉及 DB，补 Entity、Mapper XML、Repository，并保持强类型入参/返参。
5. 若涉及事务，先按运行时红线判断是否需要事务，再选择 `TransactionTemplate` 或 `@Transactional`。
6. 若涉及 Redis 缓存，用 `RedisUtils`；若涉及分布式锁，用 `RedisLockTemplate`，并保证 Redis/Redisson 在事务外。
7. 若涉及外部副作用，确保副作用在事务外或 `afterCommit`。
8. 按 `references/runtime-guardrails.md` 的 checklist 做最终检查。

## 最小完成标准

- Controller 不直连 DAO/Client/Manager。
- 返回业务 DTO 或 `PageResult<T>`，不手写统一响应包装。
- DTO 分层独立，不复用 Entity 或跨层 DTO。
- 事务块内无 Feign、Redis、同步 MQ。
- 写操作幂等有 DB 行锁或明确无需幂等的理由。
