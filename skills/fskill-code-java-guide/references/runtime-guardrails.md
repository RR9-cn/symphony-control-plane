# 运行时红线与最终检查

当任务涉及事务、幂等、日志、traceId、线程池、MQ 副作用或最终 review 时读取本文件。

## 事务决策

先判断是否真的需要事务：

```text
满足任一条件才需要事务：
- 多步 DB 写需要原子性，如落库 + 扣库存 + 写流水
- 需要 SELECT ... FOR UPDATE 行锁保证幂等
- 需要一致性快照，且多次读取必须在同一事务视图内

不满足则不加事务：
- 单次查询
- 单次插入/更新/删除
- 纯内存计算
- 纯外部调用
- 多次独立查询且不需要一致性快照
```

事务工具选择：

| 方式 | 适用场景 | 注意 |
|---|---|---|
| `TransactionTemplate.execute(...)` | 方法内同时存在事务内 DB 和事务外 RPC/MQ/Redis | 推荐默认选择 |
| `@Transactional` | 方法体内只有纯 DB 操作 | 整个方法都是事务边界 |
| `@Transactional` + `afterCommit` | 纯主流程落库 + 提交后单点副作用 | 副作用必须在提交后 |

事务块内禁止：

| 禁止操作 | 原因 | 正确做法 |
|---|---|---|
| FeignClient / RestTemplate / HTTP RPC | 网络超时拖长持锁 | 挪到事务外 |
| RedisUtils / RedisLockTemplate / RedissonClient | 网络阻塞拖长持锁 | 挪到事务外 |
| 同步 MQ `syncSend` | 等 Broker ACK 阻塞 | 业务 Producer 调 `TransactionalMqSender.syncSendAfterCommit` |
| 长循环 / 大批量计算 | 拖长事务 | 拆批，单事务小步快走 |

事务块内允许：本地 DB 操作、内存计算、对象转换、非阻塞异步 MQ 发送；但消费端必须幂等。

## 幂等写

需要幂等的写操作统一使用：

```text
事务内：
1. SELECT ... FOR UPDATE 获取目标行锁
2. 判断记录状态是否允许操作
3. 已处理则直接返回，未处理才执行业务写入
```

- Redis `setIfAbsent` 可做入口削峰或辅助去重，但不能作为唯一幂等保障。
- 唯一索引可作为兜底约束，但业务状态判断仍要显式表达。
- 复杂场景读取 `examples/idempotent-write.md`。

## 日志与 traceId

- 业务日志统一用 `LogUtil`，禁止直接调用 `log.info()` / `log.warn()` / `log.error()`。
- 类可用 `@Slf4j` 提供 Logger，但输出走 `LogUtil.xxx(log, ...)`。
- 日志 message 以 Service 主入口方法名开头，格式：`methodName >> 具体描述`。
- Service 主流程入口/出口成对打 `INFO`。
- 抛 `BusinessException` 前打 `WARN`，包含原因和关键入参。
- 捕获系统异常打 `ERROR`，传 Throwable，保留堆栈和上下文。
- 外部调用前打 `INFO`，降级/远端业务失败打 `WARN`。
- MQ 发送后记录 msgId 和业务 ID；消费入口记录 topic/tag 和业务 ID。
- 禁止 `e.printStackTrace()`、吞异常、循环内大量 INFO、打印敏感信息。
- traceId 由 `TraceIdFilter`、`TraceIdFeignInterceptor`、`TraceIdRocketMqConsumerHook`、`SchedulerxJobTraceIdAspect`、`MdcTaskDecorator` 统一放入 MDC。
- 业务代码禁止自建 `ThreadLocal` 传 traceId，禁止手动拼 traceId，禁止手动给 Feign 塞 `X-Trace-Id`。
- 发 MQ 统一走 `service.extension.mq.TransactionalMqSender`，由它调用 `MqMessageHelper.buildMessage(payload)` 并在有事务时自动注册 afterCommit。

## 线程池

- 异步任务使用 `web.config.ThreadPoolConfig` 提供的 `taskExecutor` Bean，或使用默认 `@Async`。
- 禁止业务代码 `new ThreadPoolExecutor`、`new ThreadPoolTaskExecutor`、`Executors.newXxx`。
- 需要多个线程池时，在 `ThreadPoolConfig` 增加命名 Bean，并用 `@Async("xxxExecutor")` 指定。
- `taskExecutor` 已配 `MdcTaskDecorator`，traceId 自动跨线程透传。
- 异步任务必须处理异常或确保异常被统一处理，禁止 `try-catch` 后吞掉。

## 最终 Checklist

每次后端 Java 修改后检查：

- [ ] 包路径符合 `references/naming-placement.md`，无越界放包。
- [ ] 命名符合 Controller/Service/Manager/Repository/Mapper/Entity/DTO 等规则。
- [ ] 对外 HTTP 与对内 RPC 物理隔离，路径和包名正确。
- [ ] Controller 不直连 Manager/Mapper/Repository/Client。
- [ ] Controller 返回业务 DTO / `PageResult<T>`，无手写 `Result.success(...)`，无裸 `Map`/Entity。
- [ ] 业务异常抛 `BusinessException`，Controller 不 catch 业务异常。
- [ ] 已判断是否需要事务，未盲目添加 `@Transactional`。
- [ ] 事务 + 外部调用并存时使用 `TransactionTemplate`。
- [ ] 事务块内无 Feign/Redis/同步 MQ/长循环。
- [ ] 幂等写使用 `FOR UPDATE` 行锁 + 状态判断。
- [ ] DTO / DAL Bean 分层独立；Controller 简单协议转换可用 `FsBeanUtil`；Service 层转换统一调用 `XxxAssembler`，未直接散落 `FsBeanUtil` 或 MapStruct。
- [ ] Mapper/XML 强类型入参返参，无 `SELECT *`。
- [ ] Feign 入参/返参强类型，远端业务失败和网络失败转换为明确异常，不伪造成功。
- [ ] Redis 缓存使用 `RedisUtils`，分布式锁使用 `RedisLockTemplate`，Key 使用常量，事务内无 Redis/Redisson 调用。
- [ ] 配置经配置类注入，Service/Manager/Controller 不直接 `@Value`。
- [ ] 类、接口、方法、字段有 JavaDoc；无内部类。
- [ ] 日志使用 `LogUtil`，入口/出口/中断点/系统异常覆盖到位。
- [ ] traceId 未被业务代码手动处理或破坏。
- [ ] MQ 生产者未直接注入 `RocketMQTemplate`，统一通过 `TransactionalMqSender` 发送；消息通过 `MqMessageHelper.buildMessage(payload)` 构建。
- [ ] 异步任务使用统一 `taskExecutor`，异常有处理。
