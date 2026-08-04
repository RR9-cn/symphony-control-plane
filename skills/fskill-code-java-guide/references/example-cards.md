# Java 组件轻量卡片

先读本文件判断需要哪个完整示例。只有从零生成、修改复杂实现或对具体写法不确定时，再读取 `examples/*.md`。

| 组件/场景 | 默认规则 | 需要完整示例时读取 |
|---|---|---|
| Enum | 字段名优先 `code`/`desc`，提供 `getByCode`；落库状态用 `Integer`。 | `examples/enum.md` |
| Constant | 常量类 `final` + 私有构造；Redis Key 带业务命名空间；MQ Topic/Tag 统一常量。 | `examples/constant.md` |
| Result/PageResult | Controller 返回业务 DTO 或 `PageResult<T>`；禁止手写 `Result.success(...)`。 | `examples/result.md` |
| Exception | 业务异常抛 `BusinessException`；Controller 不 catch 业务异常；错误码按分段管理。 | `examples/exception.md` |
| Web/API DTO | Request/Response 放 `api.xxx.dto`；Request 字段带 JavaDoc 和 JSR-303；跨字段校验放 Service。 | `examples/dto-web-api.md` |
| Service DTO | Param/Model 放 Service 层；字段名尽量与 Request/Response 对齐；不带 JSR-303。 | `examples/dto-service.md` |
| Client DTO | Feign Form/Result 独立于 web/service DTO；Result 显式表达远端业务结果。 | `examples/dto-client.md` |
| MQ DTO | Message 必须含业务 ID 和 `eventTime`；禁止塞大对象。 | `examples/dto-mq.md` |
| Controller | 只做接收、校验触发、协议 DTO 简单转换、调 Service、返回业务 DTO；对外 `/api/**`，对内 `/rpc/**`。 | `examples/controller.md` |
| Service | 接口 + 实现分离；负责编排和事务边界；转换委托 Assembler。 | `examples/service.md` |
| Manager/Assembler | Manager 写纯领域逻辑；Assembler 是业务层转换门面，内部按复杂度选 `FsBeanUtil` 或 MapStruct `XxxConverter`。 | `examples/manager.md` |
| Repository | 封装 Mapper，方法名业务语义化；不向上暴露 MyBatis 细节。 | `examples/repository.md` |
| Mapper/XML | Mapper 入参/返参强类型；XML 用 `resultMap`、`Base_Column_List`，禁止 `SELECT *`。 | `examples/mapper.md` |
| Entity | `@Data`；主键 `Long`；时间 `LocalDateTime`；状态落库用 `Integer`。 | `examples/entity.md` |
| FeignClient | 入参/返参用强类型 DTO；FallbackFactory 转远程异常；事务内禁止调用。 | `examples/feign-client.md` |
| MQ Producer | 发消息用 `TransactionalMqSender`；业务 Producer 不直连 `RocketMQTemplate`；有事务时由统一封装 afterCommit 发送。 | `examples/mq-producer.md` |
| MQ Consumer | 消费入口有日志；失败抛出触发重试；落库幂等靠 Service 内 DB 行锁兜底。 | `examples/mq-consumer.md` |
| Config | 配置集中在 `service.config` 或 `web.config`；业务类注入配置类，不直接 `@Value`。 | `examples/config-properties.md` |
| Multi datasource | 按数据源分包；每源独立 `DataSource`/`SqlSessionFactory`/`TransactionManager`。 | `examples/multi-datasource-config.md` |
| Redis | 缓存用 `RedisUtils`，分布式锁用 `RedisLockTemplate`；Key 引用常量；事务内禁止 Redis/Redisson。 | `examples/redis-utils.md` |
| Bean copy / MapStruct | 简单同名字段用 `FsBeanUtil`；复杂稳定映射用 MapStruct；Service 只调用 Assembler。 | `examples/bean-util.md` |
| 幂等写 | 事务内先 `SELECT ... FOR UPDATE`，再判断状态；Redis 去重只能辅助。 | `examples/idempotent-write.md` |
| TransactionTemplate | 事务 + 外部调用并存时优先编程式事务；纯 DB 方法才考虑 `@Transactional`。 | `examples/transaction-template.md` |
| Logging | 业务日志用 `LogUtil`；入口/出口成对；中断点 WARN；系统异常 ERROR 带堆栈。 | `examples/logging.md` |
| Thread pool | 异步任务用 `taskExecutor` 或 `@Async` 默认 executor；禁止自建线程池。 | `examples/thread-pool.md` |

## 使用顺序

1. 先用本卡片确认涉及哪些组件。
2. 只读取会被创建或修改的组件完整示例。
3. 若只是小改现有文件，优先遵循现有代码风格和本卡片红线。
4. 若生成端到端功能，先读对应 `scenarios/*.md`，再按条件读取完整示例。
