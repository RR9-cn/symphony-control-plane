---
name: "fskill-code-java-guide"
description: "后端 Java 编码规范入口。仅在即将创建或修改 backend 下 Java/XML/YML 后端代码时使用；纯调研、设计讨论、需求分析、前端/文档/DevOps 不触发。触发后先读本入口，按任务只加载相关 scenario、example 或 reference。"
---

# 后端 Java 开发规范守卫

本 Skill 是轻量入口，不是完整规范正文。目标是在 agent 编码前提供必须遵守的红线，并把细节按任务类型延迟加载。

## 适用范围

- 技术栈：Java 25 + Spring Boot 4.1.0 + Spring Cloud 2025.1.1 + MyBatis + Nacos + Redisson + RocketMQ + SchedulerX。
- 基础包名：适用于 `com.fshows.<业务域>` 或 `com.fshows.<业务域>.<应用>` 下的后端业务工程；使用前先从当前仓库已有 package、构建配置或启动类推断真实基础包名，禁止把示例中的 `com.fshows.storemate.merchant` 当作固定值。
- 后端模块：适用于职责基本符合 `common`、`api`、`client`、`dal`、`service`、`web` 分层的工程；模块可以位于 `backend/` 下，也可以是同等职责的 Gradle/Maven 多模块，具体 artifact/module 名以当前项目为准。
- 文件类型：`.java`、`*Mapper.xml`、`application*.yml`、`bootstrap.yml`、`logback-spring.xml`。
- 不适用：纯前端、纯文档、纯 DevOps 脚本、只读调研。

> 说明：本 Skill 中的 `storemate-merchant-service`、`com.fshows.storemate.merchant`、`activity` 等仅作为示例工程、示例基础包和示例子域。应用到其他 fshows 项目时，必须替换为当前项目真实服务名、基础包和业务子域；分层职责优先于示例名称。

## 触发边界

触发：当你即将创建或修改后端 Java/XML/YML 代码，或用户明确要求写接口、Controller、Service、DAO、MQ、Redis、Feign、Nacos、多数据源等后端实现。

不触发：只读代码调研、需求分析、设计方案讨论、PRD 评审、前端任务、文档任务。若用户从调研/设计转为“按方案实现”，编码前再触发本 Skill。

## 加载纪律

1. 先读本入口，判断任务类型。
2. 端到端任务只读 1 个最贴近的 `scenarios/*.md`。
3. 单组件任务优先读 `references/example-cards.md` 的对应卡片；只有要从零生成、修改复杂实现或不确定时，才读对应 `examples/*.md` 完整示例。
4. 初始详细文档最多读 1 个 scenario 或 1-3 个 examples；后续只在实际会创建/修改对应文件时扩展。
5. 不要批量读取所有 examples/scenarios。若旧式 include 注释仍存在，只把它当作候选提示，不按注释批量读取。
6. 事务、幂等、MQ、Redis、Feign、多数据源、线程池、日志等专项规则只在任务涉及该能力时加载。

## 加载预算

| 阶段 | 默认预算 | 扩展条件 |
|---|---|---|
| 识别任务 | 只读本文件 | 任务边界不清时读 `references/example-cards.md` |
| 单组件修改 | 1 张组件卡片或 1 个完整 example | 要新增同类文件、改复杂逻辑或找不到现有模式 |
| 端到端实现 | 1 个 scenario + 1-3 个核心 examples | 实际创建/修改 DAO、Feign、MQ、Redis、事务代码时再读对应 example |
| 事务/幂等 | 先读 `references/runtime-guardrails.md` | 需要完整代码模板时再读 transaction/idempotent example |
| 收尾检查 | 本文件最终红线检查 | 复杂改动读完整 checklist |

## 何时扩展读取

- 你要创建一个新文件，而本入口只给了原则，没有给出该文件的模板。
- 你要修改的代码涉及事务边界、幂等、远程调用、缓存、消息、线程池或多数据源。
- 现有代码风格和本入口红线冲突，需要读 reference 判断取舍。
- 用户要求“从零实现”一个端到端能力，而不是小修现有方法。
- 你准备写 Mapper XML、FallbackFactory、Consumer、配置类等容易出错的基础设施代码。

## 何时不要扩展读取

- 只是阅读代码、梳理调用链、做方案设计。
- 只是改一个已存在方法里的简单条件、常量或文案。
- 现有文件已有清晰同类写法，且不触碰事务、DAO、MQ、Redis、Feign 等高风险边界。
- 只是检查某个类是否符合命名或放置规则；此时读 `references/naming-placement.md` 即可。

## 编码流程

1. 明确会创建或修改哪些文件类型。
2. 用任务路由选择 1 个 scenario 或少量 example。
3. 先遵循现有代码局部风格，再用本 Skill 红线修正不合规点。
4. 先设计事务边界，再写 DB、外部调用、MQ/Redis 副作用。
5. 先定义强类型 DTO/Criteria/Result，再写接口签名。
6. 写完后执行最终红线检查；复杂任务再读完整 checklist。

## 13 条编码红线

1. **包路径先行**：类必须放在规范包下，禁止越界放包；需要明细时读 `references/naming-placement.md`。
2. **分层不可跨越**：Controller 只调 Service 接口；Service 编排 Manager/Repository/Client/extension；Repository 只封装 Mapper；Service 默认不直连 Mapper；Service 模块禁止依赖 api 模块，禁止引用 API DTO 或 Feign 契约接口。
3. **Service 接口与实现分离**：Service 使用 `IXxxService` + `XxxServiceImpl`；Controller 依赖接口。
4. **DTO 分层隔离与转换收口**：Request/Response、Param/Model、Form/Result、DAL Criteria/Result 独立定义；Controller 可用 `FsBeanUtil` 做协议 DTO 简单转换，Service 层转换统一委托 `XxxAssembler`，MapStruct `XxxConverter` 只作为 Assembler 内部实现，禁止跨层复用 DTO。
5. **统一返回与异常**：Controller 返回业务 DTO 或 `PageResult<T>`，由响应拦截统一包装；业务异常抛 `BusinessException`。
6. **事务不是默认项**：只有多步 DB 写需原子性、需 `FOR UPDATE` 幂等或需一致性快照时才加事务；优先 `TransactionTemplate`。
7. **事务内禁止网络阻塞**：事务块内禁止 Feign/HTTP、Redis/Redisson、同步 MQ、大循环；MQ 生产者统一走 `TransactionalMqSender`，由它在有事务时 afterCommit 发送。
8. **幂等写靠 DB 兜底**：需要幂等的写操作必须在事务内 `SELECT ... FOR UPDATE` 锁行并判断状态，禁止只靠 Redis 去重。
9. **基础设施统一使用**：Redis 缓存走 `RedisUtils`，分布式锁走 `RedisLockTemplate`，均放 `service.extension.redis`；配置经配置类；线程池用 `taskExecutor`；traceId 由基础设施透传；日志用 `LogUtil`。
10. **DAO/Feign 强类型**：Mapper/Repository/Feign 入参和返参使用 JavaBean、集合或明确结果类，禁止 `Map`、`Object`、`JSONObject` 等无结构类型。
11. **注释必须使用中文**：类、接口、方法、全局变量以及关键逻辑的注释必须使用中文，注释要简洁明了。
12. **抛异常前必打日志**：每个 `throw` 语句前必须有对应的日志打印（`LogUtil.warn` 用于业务中断，`LogUtil.error` 用于系统异常），日志须包含方法名前缀、中断原因和关键入参；禁止裸 `throw` 不留排查痕迹。编写 Service 层代码时，必须先读 `examples/logging.md` 确认日志写法。
13. **数据库枚举字段必须建 Enum 类**：数据库中存储离散值（如状态、类型、标志位）的字段，在代码中使用时**禁止**用裸 `Integer`/`String` 魔法值硬编码，必须先定义对应的 Enum 类。命名规则：`{表名关键字前缀}{字段名}Enum`，如 `t_activity` 的 `status` 字段 → `ActivityStatusEnum`，`t_order` 的 `pay_type` 字段 → `OrderPayTypeEnum`。Enum 的 `code`/`desc` 字段、放置位置、`getByCode` 方法等具体写法见 `examples/enum.md`。

## 红线解释优先级

- 业务代码与本 Skill 冲突时，以项目现有基础设施类和本 Skill 红线共同约束；不要为套模板破坏已存在的统一封装。
- 若现有代码存在不合规写法，小范围修改时不要扩大重构；只保证本次新增/触碰代码不继续复制明显违规模式。
- 若用户明确要求“按规范重构”，再按 reference 和 examples 扩大整改范围。
- 若缺少某个基础设施类，例如 `LogUtil` 或 `MqMessageHelper`，先在项目中搜索确认真实 API，再决定是否沿用现有等价封装。
- 不能为了省上下文跳过事务、幂等、远程调用和 Mapper XML 的专项规则；这些属于高风险扩展条件。

## 任务路由

| 任务类型 | 初始读取 | 条件扩展 |
|---|---|---|
| 不确定该读哪个示例 | `references/example-cards.md` | 按卡片再读对应完整 example |
| 命名、包路径、分层调用 | `references/naming-placement.md` | 对应组件 example |
| 写一个接口/Controller | `scenarios/create-controller-api.md` | 涉及 DB、Feign、事务、幂等时按 scenario 条件扩展 |
| 分页查询 | `scenarios/query-paged-list.md` | 新增 SQL 时读 mapper/repository full examples |
| 调外部服务/Feign | `scenarios/call-external-service.md` | 复杂降级时读 `examples/feign-client.md` |
| 发送 MQ | `scenarios/send-async-message.md` | 涉及事务提交后发送时读事务规则 |
| 消费 MQ | `scenarios/consume-message.md` | 涉及落库幂等时读幂等规则 |
| Redis/缓存/分布式锁 | `scenarios/cache-with-redis.md` | 涉及 DB 幂等写时读幂等规则 |
| 多数据源 | `scenarios/multi-datasource-operation.md` | 需要新增配置时读完整 multi-datasource example |
| Nacos/配置类 | `scenarios/read-nacos-config.md` | 需要 yml 示例时读完整 config example |
| 事务、日志、线程池、最终检查 | `references/runtime-guardrails.md` | 不确定具体写法时读专项 example |

## Reference 索引

- `references/example-cards.md`：轻量组件卡片，优先读取。
- `references/naming-placement.md`：命名、放置位置、分层调用明细。
- `references/runtime-guardrails.md`：事务决策、日志、traceId、线程池、最终 checklist。
- `scenarios/*.md`：端到端任务的条件加载路线。
- `examples/*.md`：完整代码示例，只在从零生成、复杂修改或不确定时读取。

## 单组件快速路由

| 只改这个组件 | 优先读取 | 典型扩展 |
|---|---|---|
| Controller | `examples/controller.md` | DTO、Service、内部 RPC 契约 |
| Service | `examples/service.md` + `examples/logging.md` | 事务、幂等、Feign、MQ、Redis |
| DTO | `references/example-cards.md` | web/service/client/mq 对应 DTO example |
| Mapper/XML | `examples/mapper.md` | Entity、Repository、事务/幂等 |
| Repository | `examples/repository.md` | Mapper、Entity |
| FeignClient | `examples/feign-client.md` | client DTO、异常 |
| MQ Producer | `examples/mq-producer.md` | MQ DTO、常量、统一发送封装 |
| MQ Consumer | `examples/mq-consumer.md` | 幂等、Service、Redis 辅助去重 |
| Redis | `examples/redis-utils.md` | 常量、事务红线 |
| 配置类/YML | `examples/config-properties.md` | 多数据源或 Web 配置 |

## 端到端快速路由

| 用户意图 | 读取 |
|---|---|
| “写一个接口” | `scenarios/create-controller-api.md` |
| “分页查询” | `scenarios/query-paged-list.md` |
| “调外部服务/Feign” | `scenarios/call-external-service.md` |
| “发 MQ/异步消息” | `scenarios/send-async-message.md` |
| “消费 MQ” | `scenarios/consume-message.md` |
| “加缓存/分布式锁” | `scenarios/cache-with-redis.md` |
| “多数据源” | `scenarios/multi-datasource-operation.md` |
| “读 Nacos/加配置” | `scenarios/read-nacos-config.md` |

## 最终红线检查

编码后至少检查：

- 包路径和命名是否符合 `references/naming-placement.md`。
- Controller 是否未直连 Manager/Mapper/Repository/Client。
- Service 模块是否未依赖 api 模块，Service 代码是否未引用 API DTO 或 Feign 契约接口。
- DTO 是否按层隔离，未裸用 `Map`/Entity 对外返回，Service 是否未直接散落 `FsBeanUtil`/MapStruct 调用。
- 事务是否确有必要，且事务块内无 Feign/Redis/直接同步 MQ。
- 幂等写是否用 DB 行锁 + 状态判断兜底。
- Mapper XML 是否无 `SELECT *`，入参/返参强类型。
- Feign 是否强类型 DTO，异常转换不伪造成功。
- Redis、分布式锁、配置、线程池、traceId、日志是否走统一基础设施。
- 业务异常、系统异常和关键中断点是否有合规日志。
- 每个 `throw` 前是否有 `LogUtil.warn`（业务中断）或 `LogUtil.error`（系统异常）日志，且包含方法名前缀、原因和关键入参。
- 数据库枚举字段（状态、类型等）是否已定义对应 Enum 类，命名是否符合 `{表名关键字前缀}{字段名}Enum` 规则，代码中是否存在裸魔法值。
- 类、接口、方法、全局变量和关键逻辑的注释是否使用中文。
- 若任务较复杂，读 `references/runtime-guardrails.md` 的完整 checklist 后再收尾。
