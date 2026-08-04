# 场景：Redis 缓存与分布式锁

本场景用于新增或修改 Redis 缓存、分布式锁、Key 常量或缓存旁路逻辑。

## 初始必读

| 必读材料 | 用途 |
|---|---|
| `references/example-cards.md` | 快速确认 Redis、常量、异常规则 |

## 条件读取

| 条件 | 再读取 |
|---|---|
| 新增或修改 Redisson 封装、缓存和锁模板写法 | `examples/redis-utils.md` |
| 新增 Redis Key 常量 | `examples/constant.md` |
| 缓存逻辑与 DB 事务混用 | `references/runtime-guardrails.md`、`examples/transaction-template.md` |
| 锁保护的是幂等写或状态变更 | `examples/idempotent-write.md` |
| 缓存 miss 后查询 DB | `examples/repository.md`、必要时读 `examples/mapper.md` |
| 需要业务异常 | `examples/exception.md` |

## 落地顺序

1. 定义 Redis Key 常量，包含清晰命名空间和业务 ID。
2. 缓存读写使用 `RedisUtils`，分布式锁使用 `RedisLockTemplate`，禁止直接 `StringRedisTemplate` / `RedisTemplate` / `RedissonClient`。
3. 缓存读写放事务外；事务内不要访问 Redis。
4. 分布式锁只保护临界区，不替代 DB 幂等。
5. `RedisLockTemplate#executeWithLock` 返回 `null` 表示获取锁失败或线程中断，业务代码必须显式处理。
6. 锁竞争失败、系统异常和关键分支按日志规则记录。

## 场景用例

### 用例 1：普通缓存旁路查询

- 使用 `RedisUtils.getObject()` 先查缓存；命中直接返回。
- 缓存未命中时查 Repository，结果经 `XxxAssembler` 转成 Model 后用 `RedisUtils.setObject()` 写缓存。
- 查询为空时可写短 TTL 空值防穿透。
- 方法通常不加事务；缓存读写和 DB 单次查询都不需要事务。

### 用例 2：热点 Key 回源保护

- 缓存未命中且该 Key 可能高并发回源时，使用 `RedisLockTemplate.executeWithLock(lockKey, ...)` 包住回源逻辑。
- 锁内可二次查缓存，避免其他线程已回填后重复查 DB。
- 获取锁失败时返回降级结果、短暂重试或直接提示稍后重试，禁止把 `null` 当业务成功。
- 该场景不替代 DB 幂等；它只减少并发回源压力。

### 用例 3：分布式锁 + DB 幂等写

- 外层 Service 方法不加 `@Transactional`，先用 `RedisLockTemplate` 在事务外获取锁。
- 锁内通过接口代理调用独立的 `@Transactional` 方法。
- 事务方法内只做本地 DB 操作：`SELECT ... FOR UPDATE`、状态判断、写入；禁止 Redis/Feign/同步 MQ。
- 分布式锁负责跨进程串行化，DB 行锁和状态判断负责最终幂等兜底。

## 最小完成标准

- 无直接 `StringRedisTemplate` / `RedisTemplate` / `RedissonClient`。
- 分布式锁统一走 `RedisLockTemplate`，业务代码不直接操作 `RLock`。
- 事务内无 Redis 调用。
- Redis 去重不作为唯一幂等保障。
- 锁竞争失败或线程中断的 `null` 返回已被显式处理。
- Key 不硬编码散落业务代码。
