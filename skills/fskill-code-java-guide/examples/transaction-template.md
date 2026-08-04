# TransactionTemplate（编程式事务）写法

> 第二层原子事例 · 后端 Java 规范
> 适用场景：Service 方法需要"事务内 DB 操作 + 事务外外部调用（RPC/MQ/Redis）"并存的场景
> 速查索引：见 [SKILL.md](../SKILL.md) 的事务红线与 [runtime-guardrails.md](../references/runtime-guardrails.md) 的事务决策。
> **强约束**：
> 1. **先问"是否需要事务"**：仅"多步 DB 写需原子性 / 需 FOR UPDATE 行锁幂等 / 需一致性快照"时才用事务，其余不加。
> 2. **确认需要后优先用 `TransactionTemplate`**：`@Transactional` 注解仅适合"方法内纯 DB 操作、无任何外部调用"的简单场景。

---

## 0. 是否需要事务（决策树）

> 这是使用任何事务工具前的**第一道决策**。事务不是默认选项，而是"必须保证多步 DB 操作原子性"时才启用的资源。

```
该方法是否满足以下任一条件？
├─ 多步 DB 写需要原子性（落库 + 扣库存 + 写流水等）
├─ 需要 FOR UPDATE 行锁保证幂等（见 idempotent-write.md）
└─ 需要一致性快照（同一事务内多次读同一行数据，禁止幻读）
   ├─ 是 → 需要事务
   │     ├─ 方法内纯 DB 操作、无外部调用 → @Transactional（简单场景）
   │     └─ 方法内有外部调用（RPC/MQ/Redis） → TransactionTemplate（DB 包 execute，外部在块外）
   └─ 否 → 不加事务
        ├─ 单次查询 → 直接调 Repository，无 @Transactional
        ├─ 单次插入/更新/删除 → 直接调 Repository，无 @Transactional
        ├─ 纯内存计算 / 纯外部调用 → 无 DB 即无事务
        └─ 多次独立查询（不需一致性快照） → 无 @Transactional
```

### 0.1 不需要事务的常见场景

| 场景 | 错误做法 | 正确做法 |
|------|---------|---------|
| 单次 `findById` 查询 | `@Transactional(readOnly = true)` + 调 Repository | 直接调 Repository，无事务 |
| 单次 `insert` / `update` / `delete` | `@Transactional` + 调 Repository | 直接调 Repository，无事务（单条 DB 操作本身原子） |
| 纯外部 RPC 调用 + 落库（无多步原子性要求） | `@Transactional` 包整个方法 | 不加事务，直接调 Repository + Client |
| 多次独立查询（不同表/不同行，不需一致性快照） | `@Transactional(readOnly = true)` 包整个方法 | 无事务，各自调 Repository |
| 纯内存计算 / 对象转换 | 无（一般不会误加） | 无事务 |

### 0.2 需要事务的常见场景

| 场景 | 为什么需要事务 |
|------|--------------|
| 创建订单（写订单表 + 扣库存 + 写流水） | 多步 DB 写需原子性，任一失败全回滚 |
| 支付回调（FOR UPDATE 锁订单 + 状态判断 + 更新状态） | 需 FOR UPDATE 行锁保证幂等 |
| 转账（扣 A 账户 + 加 B 账户） | 多步 DB 写需原子性 |
| 同一事务内多次读同一行做一致性判断 | 需一致性快照，禁止幻读 |

> **判断口诀**：能不用就不用；单条 DB 操作天然原子无需事务；只有"多步 DB 写"或"FOR UPDATE 锁行"才需要事务。

---

## 1. 为什么用 TransactionTemplate 而非 @Transactional

### 1.1 @Transactional 的局限

`@Transactional` 是**声明式**事务，整个方法体都是事务边界。一旦方法被代理调用，Spring 在方法入口开启事务、方法出口提交/回滚——**方法内所有代码都在事务内**。

这导致一个根本矛盾：当 Service 方法既要落库又要调外部服务时，外部调用被迫也落在事务内，违反“事务内禁止网络阻塞点”的红线。

```java
// ❌ @Transactional 的局限：整个方法都是事务，外部调用被拖进事务
@Transactional(rollbackFor = Exception.class)
public ActivityModel createActivity(ActivityCreateParam param) {
    activityRepository.save(entity);       // 事务内（OK）
    couponClient.grantCoupon(form);        // 也被拖进事务（违反事务内禁止网络阻塞点）
    return activityAssembler.toModel(entity);
}
```

变通方案是注册 `afterCommit` 回调，但只适合"事务后单点副作用"（如发 MQ），不适合事务中间穿插 RPC、依赖 RPC 结果决定后续 DB 写的复杂场景。

### 1.2 TransactionTemplate 的优势

`TransactionTemplate.execute(...)` 是**编程式**事务，只把 lambda 块内的代码包进事务，块外的代码天然在事务之外。可以精确切分"DB 操作走事务 / 外部调用走事务外"的边界。

```java
// ✅ TransactionTemplate：DB 操作在事务块内，外部调用在事务块外
public ActivityModel createActivity(ActivityCreateParam param) {
    // 事务外：调用外部服务查询前置数据（不阻塞事务）
    PreCheckResult preCheck = couponClient.preCheck(...);

    // 事务内：纯 DB 操作
    ActivityEntity entity = transactionTemplate.execute(status -> {
        ActivityEntity e = activityAssembler.toEntity(param, initStatus);
        activityRepository.save(e);
        return e;
    });

    // 事务外：调用外部服务（事务已提交，不阻塞）
    couponClient.grantCoupon(...);
    return activityAssembler.toModel(entity);
}
```

---

## 2. 编写规范

- **优先用 `TransactionTemplate`**：涉及事务的 Service 方法首选编程式事务，特别是事务 + 外部调用并存的场景。
- **`@Transactional` 仅限纯 DB 方法**：方法内只有 Mapper/Repository 调用、内存计算、对象转换，无任何 FeignClient/Redis/同步 MQ 时，可用 `@Transactional` 注解。
- **注入方式**：`@Autowired private TransactionTemplate transactionTemplate;`（Spring Boot 自动配置已提供 `TransactionTemplate` Bean，基于默认 `PlatformTransactionManager`）。
- **多数据源场景**：需为每个 `TransactionManager` 创建独立的 `TransactionTemplate` Bean，按数据源注入。
- **异常回滚**：`execute` 块内抛 `RuntimeException` 自动回滚；抛 checked 异常默认不回滚，需 `status.setRollbackOnly()` 或用 `execute(action -> {...})` 配合 `setRollbackOnly`。
- **返回值**：`execute` 有返回值，适合"事务内落库后返回主键 Entity"的场景；无返回值用 `executeWithoutResult`（Spring 5.2+）。
- **事务传播/隔离级别**：可通过 `TransactionTemplate` 的 setter 配置（`setPropagationBehavior` / `setIsolationLevel`），或构造时传入 `TransactionDefinition`。

---

## 3. 完整示例

### 3.1 注入 TransactionTemplate

```java
package com.fshows.storemate.merchant.service.service.activity.impl;

import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Service;
import org.springframework.transaction.support.TransactionTemplate;

/**
 * 活动业务服务实现。
 * 使用 TransactionTemplate 编程式事务。
 */
@Slf4j
@Service
public class ActivityServiceImpl implements IActivityService {

    /** 事务模板（Spring Boot 自动配置，基于默认 TransactionManager） */
    @Autowired
    private TransactionTemplate transactionTemplate;

    @Autowired
    private ActivityRepository activityRepository;

    @Autowired
    private ActivityCreateManager activityCreateManager;

    @Autowired
    private ActivityStatusManager activityStatusManager;

    @Autowired
    private ActivityAssembler activityAssembler;

    @Autowired
    private CouponClient couponClient;

    // ...
}
```

### 3.2 事务 + 外部调用并存（核心模式）

```java
/**
 * 创建活动。
 * 事务边界：用 TransactionTemplate 精确包裹 DB 操作，外部调用在事务块外。
 *
 * @param param 创建入参
 * @return 活动业务模型
 */
@Override
public ActivityModel createActivity(ActivityCreateParam param) {
    LogUtil.info(log, "createActivity >> 开始，name={}", param.getName());

    // 1. 事务外：领域校验（纯内存逻辑，无需事务）
    activityCreateManager.validate(param.getStartTime(), param.getEndTime());
    Integer initStatus = activityStatusManager.initStatus();

    // 2. 事务内：组装实体 + 落库（纯 DB 操作，无网络调用）
    ActivityEntity entity = transactionTemplate.execute(status -> {
        ActivityEntity e = activityAssembler.toEntity(param, initStatus);
        activityRepository.save(e);   // 主键回填到 e
        return e;
    });
    // 事务在此提交，行锁释放

    // 3. 事务外：调用外部发券服务（网络 RPC 不阻塞事务）
    try {
        CouponGrantForm grantForm = new CouponGrantForm();
        grantForm.setActivityId(entity.getId());
        CouponGrantResult result = couponClient.grantCoupon(grantForm);
        if (result == null || !Boolean.TRUE.equals(result.getSuccess())) {
            LogUtil.warn(log, "createActivity >> 调发券失败，activityId={}, failReason={}",
                    entity.getId(), result == null ? "null" : result.getFailReason());
            // 落补偿表，由定时任务/人工补偿
        }
    } catch (Exception e) {
        LogUtil.error(log, "createActivity >> 调发券异常，activityId={}", e, entity.getId());
        // 落补偿表，不影响已落库的活动数据
    }

    // 4. 返回（事务外）
    return activityAssembler.toModel(entity);
}
```

### 3.3 事务内依赖 RPC 结果决定后续写（复杂场景）

```java
/**
 * 创建活动（依赖外部预校验结果的复杂场景）。
 * 模式：事务外查 RPC → 事务内落库 → 事务外发后置副作用。
 *
 * @param param 创建入参
 * @return 活动业务模型
 */
@Override
public ActivityModel createActivityWithPreCheck(ActivityCreateParam param) {
    // 1. 事务外：调外部服务做前置校验（拿到校验结果）
    PreCheckResult preCheck = couponClient.preCheck(param.getUserId());
    if (preCheck == null || !Boolean.TRUE.equals(preCheck.getAllowCreate())) {
        throw new BusinessException(ErrorCodeEnum.ACTIVITY_PRE_CHECK_FAIL);
    }

    // 2. 事务内：落库（用预校验结果决定 initStatus）
    Integer initStatus = preCheck.getInitStatus();
    ActivityEntity entity = transactionTemplate.execute(status -> {
        ActivityEntity e = activityAssembler.toEntity(param, initStatus);
        activityRepository.save(e);
        return e;
    });

    // 3. 事务外：发 MQ 通知下游（异步，不阻塞）
    ActivityCreateMessage message = new ActivityCreateMessage();
    message.setActivityId(entity.getId());
    message.setEventTime(LocalDateTime.now());
    activityMessageProducer.sendActivityCreate(message);

    return activityAssembler.toModel(entity);
}
```

### 3.4 无返回值事务（executeWithoutResult）

```java
/**
 * 更新活动状态（无返回值事务）。
 *
 * @param id     活动 ID
 * @param status 目标状态
 */
@Override
public void updateStatus(Long id, Integer status) {
    transactionTemplate.executeWithoutResult(statusHolder -> {
        ActivityEntity entity = activityRepository.findByIdForUpdate(id);  // FOR UPDATE 幂等
        if (entity == null) {
            throw new BusinessException(ErrorCodeEnum.ACTIVITY_NOT_FOUND);
        }
        if (!activityStatusManager.canTransit(entity.getStatus(), status)) {
            throw new BusinessException(ErrorCodeEnum.ACTIVITY_STATUS_INVALID);
        }
        activityRepository.updateStatus(id, status);
    });
}
```

### 3.5 checked 异常手动回滚

```java
/**
 * 处理外部协议抛 checked 异常的场景（默认不回滚，需手动 setRollbackOnly）。
 */
public void doImport() {
    transactionTemplate.execute(status -> {
        try {
            externalDataImporter.importBatch();   // 抛 IOException（checked）
            activityRepository.batchInsert(...);
        } catch (IOException e) {
            LogUtil.error(log, "importBatch >> 导入失败，手动回滚", e);
            status.setRollbackOnly();   // checked 异常默认不回滚，需显式标记
            return null;
        }
        return null;
    });
}
```

### 3.6 多数据源场景：注入指定 TransactionManager 的 TransactionTemplate

```java
@Configuration
public class TransactionTemplateConfig {

    @Bean("primaryTransactionTemplate")
    public TransactionTemplate primaryTransactionTemplate(
            @Qualifier("primaryTransactionManager") PlatformTransactionManager tm) {
        return new TransactionTemplate(tm);
    }

    @Bean("reportTransactionTemplate")
    public TransactionTemplate reportTransactionTemplate(
            @Qualifier("reportTransactionManager") PlatformTransactionManager tm) {
        return new TransactionTemplate(tm);
    }
}
```

```java
@Service
public class ReportServiceImpl implements IReportService {

    @Autowired
    @Qualifier("reportTransactionTemplate")
    private TransactionTemplate reportTransactionTemplate;

    @Override
    public ReportModel refreshReport(Long id) {
        // 报表库事务（指定 reportTransactionManager）
        return reportTransactionTemplate.execute(status -> {
            ReportEntity entity = reportRepository.findByIdForUpdate(id);
            // ... 更新报表
            reportRepository.updateById(entity);
            return reportAssembler.toModel(entity);
        });
    }
}
```

---

## 4. 与其它事务模式的协同

| 场景 | 推荐方式 | 说明 |
|------|---------|------|
| 方法内纯 DB 操作 | `@Transactional` 注解 | 简单场景，注解够用 |
| 方法内 DB + 外部调用并存 | `TransactionTemplate`（本文件） | 精确切分边界，外部调用在 `execute` 块外 |
| 主流程落库 + 事务后单点副作用（发 MQ/刷缓存） | `@Transactional` + `afterCommit` 或 `TransactionTemplate` + 块外调用 | 两种均可，TransactionTemplate 更直观 |
| 事务内依赖 RPC 结果决定 DB 写 | `TransactionTemplate`（RPC 在前，DB 在 `execute` 块内） | RPC 必须在事务外先执行拿结果 |
| 幂等写（FOR UPDATE + 状态判断） | `TransactionTemplate` 或 `@Transactional` 均可 | FOR UPDATE 行锁在事务块内即可，详见 [idempotent-write.md](idempotent-write.md) |
| 多数据源 | 指定 TransactionManager 的 `TransactionTemplate` | 每个数据源独立模板，避免误用默认 |

---

## 5. 与 afterCommit 的对比

| 维度 | `TransactionTemplate` + 块外调用 | `@Transactional` + `afterCommit` 回调 |
|------|--------------------------------|--------------------------------------|
| 事务边界控制 | 精确（只包 DB 块） | 粗（整个方法都是事务） |
| 外部调用位置 | `execute` 块外，直观 | 回调内，需 `registerSynchronization`，间接 |
| 事务中间穿插 RPC | 支持（RPC 在 `execute` 前/后） | 不支持（整个方法都在事务内） |
| 事务内依赖 RPC 结果 | 支持（RPC 先执行拿结果，再 `execute` 落库） | 不支持（afterCommit 是事务后，拿不到事务中需要的结果） |
| 代码可读性 | 高（线性流程） | 中（回调嵌套） |
| 适用复杂度 | 复杂场景首选 | 简单"事务后副作用"场景 |

> **结论**：新代码优先用 `TransactionTemplate`；已有 `@Transactional` + `afterCommit` 的简单场景可保留，但事务中间需穿插 RPC 时必须重构为 `TransactionTemplate`。

---

## 6. 最佳实践提示

- **先判断是否需要事务**：本文件的"是否需要事务决策树"是使用任何事务工具前的第一道决策。单次查询、单次插入/更新/删除、纯外部调用、纯内存计算都不加事务。禁止给所有 Service 方法默认加 `@Transactional`，禁止给查询方法默认加 `@Transactional(readOnly = true)`（除非走从库或需一致性快照）。
- **事务块尽量小**：确认需要事务后，`transactionTemplate.execute(...)` 的 lambda 块内只放纯 DB 操作（Mapper/Repository 调用），外部调用、内存计算、对象转换尽量放在块外，缩短事务持锁时间。
- **不要在事务块内调 self 方法**：`execute` 块内的代码就是普通 lambda，不走 Spring 代理，所以块内调本类的 `@Transactional` 方法不会生效嵌套事务——需要嵌套事务时显式再 `transactionTemplate.execute(...)` 或调外部 Service 代理。
- **checked 异常需手动回滚**：`execute` 块内抛 `RuntimeException` 自动回滚；抛 checked 异常（如 `IOException`）默认不回滚，需 `status.setRollbackOnly()`。
- **避免在事务块内 `return` 提前退出**：`execute` 块内 `return` 会正常提交事务，确保提前退出前业务一致性已达成。
- **多数据源必须用对应模板**：注入时用 `@Qualifier` 指定 `TransactionManager`，禁止用默认 `TransactionTemplate` 操作非主库。
- **与幂等写协同**：FOR UPDATE 行锁必须在事务块内（`execute` 块内调 `findByIdForUpdate`），锁随事务提交/回滚释放，详见 [idempotent-write.md](idempotent-write.md)。
- **与异步 MQ 协同**：发 MQ 放在 `execute` 块外（事务已提交），避免事务回滚消息已发；若必须在块内发，只能用 `asyncSend` 且消费端幂等。
- **配置传播行为**：默认 `PROPAGATION_REQUIRED`，需嵌套/独立事务时用 `transactionTemplate.setPropagationBehavior(...)` 配置，或在构造时传 `TransactionDefinition`。
