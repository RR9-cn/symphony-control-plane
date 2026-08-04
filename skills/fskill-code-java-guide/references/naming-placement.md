# 命名、放置与分层调用

当任务涉及新增类、移动类、判断包路径或检查跨层调用时读取本文件。

## 命名速查

| 类型 | 命名规则 | 示例 |
|---|---|---|
| 对外 HTTP Controller | `XxxController` | `ActivityController` |
| 对内 RPC Controller | `XxxRpcController` | `ActivityRpcController` |
| Feign 契约接口 | `XxxApi` | `ActivityApi` |
| Service 接口 | `IXxxService` | `IActivityService` |
| Service 实现 | `XxxServiceImpl` | `ActivityServiceImpl` |
| Manager | `XxxManager` | `ActivityCreateManager` |
| Assembler | `XxxAssembler` | `ActivityAssembler` |
| MapStruct Converter | `XxxConverter` / `XxxStructConverter` | `ActivityConverter` |
| Repository | `XxxRepository` | `ActivityRepository` |
| Mapper | `XxxMapper` | `ActivityMapper` |
| Entity | `XxxEntity` | `ActivityEntity` |
| DAL 查询条件 | `XxxCriteria` / `XxxQueryCriteria` | `ActivityQueryCriteria` |
| DAL SQL 结果 | `XxxResult` | `ActivitySummaryResult` |
| web/api 入参 DTO | `XxxRequest` / `XxxCreateRequest` | `ActivityCreateRequest` |
| web/api 出参 DTO | `XxxResponse` / `XxxVO` | `ActivityResponse` |
| Service 入参 DTO | `XxxParam` / `XxxCreateParam` | `ActivityCreateParam` |
| Service 出参 DTO | `XxxModel` | `ActivityModel` |
| Client 入参 DTO | `XxxForm` | `CouponGrantForm` |
| Client 出参 DTO | `XxxResult` | `CouponGrantResult` |
| MQ 消息体 | `XxxMessage` | `ActivityCreateMessage` |
| 枚举 | `XxxEnum` | `ActivityStatusEnum` |
| 常量类 | `XxxConstants` | `ActivityConstants` |
| 配置类 | `XxxProperties` / `XxxConfig` | `ActivityProperties` |
| FeignClient | `XxxClient` | `CouponClient` |
| FallbackFactory | `XxxClientFallbackFactory` | `CouponClientFallbackFactory` |
| MQ Producer | `XxxMessageProducer` | `ActivityMessageProducer` |
| MQ Consumer | `XxxConsumer` | `ActivityCreateConsumer` |

## 分层调用

| 调用方 | 可调用 | 禁止调用 |
|---|---|---|
| 对外 HTTP Controller | Service 接口 | Manager / Mapper / Repository / Client / Entity |
| 对内 RPC Controller | Service 接口，且实现 `api` 模块契约 | Manager / Mapper / Repository / Client / Entity |
| Service 实现 | Manager / Repository / Client / extension / 其他 Service | Controller / Mapper（默认禁止直连） |
| Manager | 其他 Manager / 纯领域逻辑 | Mapper / Client / Repository / Service / Controller |
| Assembler | MapStruct Converter / FsBeanUtil / 纯字段补充 | Mapper / Repository / Client / Controller |
| Repository | Mapper | Service / Manager / Client |
| Client | 外部 HTTP/RPC 服务 | Service / Manager / Mapper |

## 放置位置

| 类类型 | 放置位置 | 说明 |
|---|---|---|
| 对外 HTTP Controller | `web.controller.external.{子域}` | 路径前缀 `/api/**`，验证/临时接口可用业务前缀 |
| 对内 RPC Controller | `web.controller.internal.{子域}` | 路径前缀 `/rpc/**`，`implements api.{子域}.XxxApi` |
| Feign 契约接口 | `api.{子域}` | `@FeignClient` 接口，供其他应用依赖 |
| 外部服务 Client | `client.{子域}` | 调用外部服务 |
| Service 接口 | `service.service.xxx` | 业务编排、事务边界 |
| Service 实现 | `service.service.xxx.impl` | 实现 `IXxxService` |
| Manager / Assembler | `service.manager.xxx` | 纯领域逻辑和对象转换 |
| MapStruct Converter | `service.manager.xxx.converter` | Assembler 内部使用的编译期映射器，禁止命名为 `XxxMapper` |
| Repository | `dal.{数据源}.xxx.repository` | 可选，封装 Mapper |
| Mapper | `dal.{数据源}.xxx.mapper` | MyBatis Mapper 接口 |
| Entity | `dal.{数据源}.xxx.entity` | 数据库表实体 |
| DAL Criteria | `dal.{数据源}.xxx.criteria` | 复杂查询或更新条件 |
| DAL Result | `dal.{数据源}.xxx.result` | join/聚合/投影结果 |
| 多数据源配置 | `dal.config` | `DataSource` / `SqlSessionFactory` / `TransactionManager` |
| Web/API DTO | `api.xxx.dto` | HTTP Controller 和 RPC 契约共用入参/出参 |
| Service DTO | `service.service.xxx.param` / `service.service.xxx.model` | Service 入参/出参 |
| Client DTO | `client.xxx.dto` | FeignClient 入参/出参 |
| MQ DTO | `service.mq.xxx.dto` | 子域 MQ 消息体 |
| MQ Producer | `service.mq.xxx.producer` | 子域消息生产者 |
| MQ Consumer | `service.mq.xxx.consumer` | 子域消息消费者 |
| 通用工具/常量/枚举 | `common.util` / `common.constant` / `common.enums` | 跨模块共享；`common.util` 仅放静态无状态工具 |
| 业务枚举/常量 | `service.enums.xxx` / `service.constant.xxx` | 子域专属 |
| 业务配置 | `service.config` | Nacos 业务属性配置 |
| 外部组件封装 | `service.extension.{中间件}` | 依赖 Spring Bean 生命周期或外部组件的通用封装，如 `service.extension.redis.RedisUtils`、`service.extension.redis.RedisLockTemplate` |
| Web 配置 | `web.config` | 只放 `XxxConfig` / `XxxProperties`，如 MVC、Swagger、线程池、Feign 全局配置 |
| HTTP Filter | `web.filter` | 如 `TraceIdFilter` |
| Web Interceptor | `web.interceptor` | 如 `InternalApiInterceptor` |
| Feign 横切基础设施 | `web.feign` | Feign 全局 `RequestInterceptor` / `Decoder` / `ErrorDecoder`，如 `TraceIdFeignInterceptor`、`ResultFeignDecoder` |
| 任务执行横切组件 | `web.task` | `TaskDecorator` 等任务执行基础设施，如 `MdcTaskDecorator` |
| MQ/Job 基础设施扩展 | `service.extension.mq` / `service.extension.job` | 非业务 Consumer/JobHandler |

## 内外接口隔离

- 对外 HTTP 使用 `/api/**`，放 `web.controller.external.{子域}`，类名 `XxxController`。
- 对内 RPC 使用 `/rpc/**`，放 `web.controller.internal.{子域}`，类名 `XxxRpcController`。
- RPC 契约定义在 `api.{子域}.XxxApi`，Controller 通过 `implements XxxApi` 保持签名一致。
- `/rpc/**` 由 `InternalApiInterceptor` 校验 `X-Internal-Token`；对外 `/api/**` 不使用该拦截器。

## 模块边界

- `dal` 只允许被 `service` 依赖，禁止 `web` / `client` / `api` 直接依赖 `dal`。
- `web` 不直连数据库，所有数据访问经 Service。
- Service 默认通过 Repository 访问数据库，避免直接依赖 Mapper；只有项目既有模块明确没有 Repository 且本次不适合补建时，才可沿用局部直连 Mapper 写法。
- Service 层对象转换统一调用 `XxxAssembler`；`FsBeanUtil` 与 MapStruct `XxxConverter` 只作为 Assembler 内部实现细节。Controller 边界的 `Request -> Param`、`Model -> Response` 简单转换可直接使用 `FsBeanUtil`。
- MapStruct 接口禁止命名为 `XxxMapper`，避免与 MyBatis Mapper 混淆；统一命名 `XxxConverter` 或 `XxxStructConverter`，放在 `service.manager.xxx.converter`。
- `common.util` 只能放纯静态工具类；需要 `@Component`、`@Autowired`、Redisson/OSS/SMS/DataSource 等运行时组件的封装放 `service.extension.{中间件}`。Redis 缓存读写用 `RedisUtils`，分布式锁用 `RedisLockTemplate`。
- 所有类、接口、枚举独立文件；类、接口、方法、字段补充 JavaDoc。
