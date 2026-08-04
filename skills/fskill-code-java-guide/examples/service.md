# Service（接口 + 实现）写法

> 第二层原子事例 · 后端 Java 规范
> 适用场景：业务编排、事务边界、对外业务能力封装
> 速查索引：见 [SKILL.md](../SKILL.md) §3 索引目录

---

## 1. 放置位置

| 类型 | 位置 | 命名 |
|------|------|------|
| Service 接口 | `service.service.{子域}` | `IXxxService` |
| Service 实现 | `service.service.{子域}.impl` | `XxxServiceImpl` |

## 2. 职责边界

| 应做 | 禁止 |
|------|------|
| 事务边界（优先 `TransactionTemplate`，纯 DB 方法可用 `@Transactional`） | 写可复用的纯逻辑（放 Manager） |
| 编排 Manager / Repository / Client / extension | 手写 Param↔Entity 转换（放 Assembler） |
| 调用 Assembler 完成对象转换 | 在 Manager 层加事务 |
| 调用其他 Service 接口 | 调用 Controller |
| 业务规则校验（可委托 Manager） | 直接调 Mapper（必须经 Repository） |
| 只依赖 Assembler 转换门面 | 直接散落 `FsBeanUtil` / MapStruct 调用 |
| 事务 + 外部调用并存时用 `TransactionTemplate` 切分边界 | `@Transactional` 方法内调 FeignClient/Redis/同步 MQ |

## 3. 编写规范

- Service 强制接口与实现分离：`IXxxService` + `XxxServiceImpl`，Controller 只依赖接口。
- 实现类用 `@Service`，依赖统一 `@Autowired` 字段注入。
- Service 层对象转换统一委托 `XxxAssembler`；禁止直接调用 `FsBeanUtil`、MapStruct `XxxConverter` 或手写大量 setter。
- **事务优先用 `TransactionTemplate`**：涉及事务的 Service 方法首选编程式事务，特别是事务 + 外部调用并存的场景，DB 操作包在 `transactionTemplate.execute(...)` 块内，外部调用写在块外。`@Transactional` 注解仅适合"方法内纯 DB 操作、无任何外部调用"的简单场景；查询默认不加 `@Transactional(readOnly = true)`。详见 [transaction-template.md](transaction-template.md)。
- 类、接口、方法、注入字段必须有 JavaDoc。
- 接口与实现一一对应，禁止接口里有方法但实现没写。

## 4. 完整示例

### 4.1 Service 接口

```java
package com.fshows.storemate.merchant.service.service.activity;

import com.fshows.storemate.merchant.common.response.PageResult;
import com.fshows.storemate.merchant.service.service.activity.model.ActivityModel;
import com.fshows.storemate.merchant.service.service.activity.param.ActivityCreateParam;
import com.fshows.storemate.merchant.service.service.activity.param.ActivityQueryParam;

/**
 * 活动业务服务。
 */
public interface IActivityService {

    /**
     * 创建活动。
     *
     * @param param 创建入参
     * @return 活动业务模型
     */
    ActivityModel createActivity(ActivityCreateParam param);

    /**
     * 查询活动详情。
     *
     * @param id 活动 ID
     * @return 活动业务模型
     */
    ActivityModel getActivity(Long id);

    /**
     * 分页查询活动。
     *
     * @param param 查询入参
     * @return 分页结果
     */
    PageResult<ActivityModel> pageActivity(ActivityQueryParam param);
}
```

### 4.2 Service 实现

```java
package com.fshows.storemate.merchant.service.service.activity.impl;

import com.fshows.storemate.merchant.client.coupon.CouponClient;
import com.fshows.storemate.merchant.client.coupon.dto.CouponGrantForm;
import com.fshows.storemate.merchant.client.coupon.dto.CouponGrantResult;
import com.fshows.storemate.merchant.common.response.PageResult;
import com.fshows.storemate.merchant.common.util.LogUtil;
import com.fshows.storemate.merchant.dal.primary.activity.criteria.ActivityQueryCriteria;
import com.fshows.storemate.merchant.dal.primary.activity.entity.ActivityEntity;
import com.fshows.storemate.merchant.dal.primary.activity.repository.ActivityRepository;
import com.fshows.storemate.merchant.service.manager.activity.ActivityAssembler;
import com.fshows.storemate.merchant.service.manager.activity.ActivityCreateManager;
import com.fshows.storemate.merchant.service.manager.activity.ActivityStatusManager;
import com.fshows.storemate.merchant.service.service.activity.IActivityService;
import com.fshows.storemate.merchant.service.service.activity.model.ActivityModel;
import com.fshows.storemate.merchant.service.service.activity.param.ActivityCreateParam;
import com.fshows.storemate.merchant.service.service.activity.param.ActivityQueryParam;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Service;
import org.springframework.transaction.support.TransactionTemplate;

/**
 * 活动业务服务实现。
 * 事务优先用 TransactionTemplate。
 */
@Slf4j
@Service
public class ActivityServiceImpl implements IActivityService {

    /** 事务模板（Spring Boot 自动配置，基于默认 TransactionManager） */
    @Autowired
    private TransactionTemplate transactionTemplate;

    /** 活动仓储 */
    @Autowired
    private ActivityRepository activityRepository;

    /** 活动创建领域逻辑 */
    @Autowired
    private ActivityCreateManager activityCreateManager;

    /** 活动状态领域逻辑 */
    @Autowired
    private ActivityStatusManager activityStatusManager;

    /** 活动对象转换逻辑 */
    @Autowired
    private ActivityAssembler activityAssembler;

    /** 优惠券外部服务 */
    @Autowired
    private CouponClient couponClient;

    /**
     * 创建活动。
     * 事务边界：用 TransactionTemplate 精确包裹 DB 操作，外部调用在事务块外。
     *
     * @param param 创建入参
     * @return 活动业务模型
     */
    @Override
    public ActivityModel createActivity(ActivityCreateParam param) {
        // 入口日志：方法名 >> 关键入参（用 LogUtil，不直调 SLF4J）。
        LogUtil.info(log, "createActivity >> 开始，name={}, type={}, stock={}",
                param.getName(), param.getType(), param.getStock());

        // 1. 事务外：领域校验 + 状态初始化（纯内存逻辑，无需事务）
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
        doGrantCoupon(entity.getId());

        // 4. 转换为业务模型返回
        ActivityModel result = activityAssembler.toModel(entity);
        // ✅ 出口日志：方法名 >> 关键出参（与入口成对）
        LogUtil.info(log, "createActivity >> 完成，activityId={}, status={}",
                result.getId(), result.getStatus());
        return result;
    }

    /**
     * 调用发券（事务外执行，失败不影响已落库的活动数据）。
     *
     * @param activityId 活动 ID
     */
    private void doGrantCoupon(Long activityId) {
        try {
            CouponGrantForm grantForm = new CouponGrantForm();
            grantForm.setActivityId(activityId);
            CouponGrantResult result = couponClient.grantCoupon(grantForm);
            if (result == null || !Boolean.TRUE.equals(result.getSuccess())) {
                LogUtil.warn(log, "createActivity >> 调发券失败，activityId={}, failReason={}",
                        activityId, result == null ? "null" : result.getFailReason());
                // 落补偿表或发 MQ 由下游补偿
            }
        } catch (Exception e) {
            LogUtil.error(log, "createActivity >> 调发券异常，activityId={}", e, activityId);
            // 落补偿表，由定时任务/人工补偿，不抛异常避免影响主流程返回
        }
    }

    /**
     * 查询活动详情（单次查询，不加事务）。
     *
     * @param id 活动 ID
     * @return 活动业务模型
     */
    @Override
    public ActivityModel getActivity(Long id) {
        ActivityEntity entity = activityRepository.findById(id);
        if (entity == null) {
            return null;
        }
        return activityAssembler.toModel(entity);
    }

    /**
     * 分页查询活动（单次查询，不加事务）。
     *
     * @param param 查询入参
     * @return 分页结果
     */
    @Override
    public PageResult<ActivityModel> pageActivity(ActivityQueryParam param) {
        // Service Param 先转换为 DAL Criteria，避免 Service DTO 下沉到 DAL。
        ActivityQueryCriteria criteria = activityAssembler.toCriteria(param);
        PageResult<ActivityEntity> entityPage = activityRepository.findPage(criteria);
        return PageResult.of(
                activityAssembler.toModelList(entityPage.getList()),
                entityPage.getTotal(),
                entityPage.getPageNum(),
                entityPage.getPageSize()
        );
    }
}
```

> **事务选型说明**：
> - `createActivity` 示例展示“需要事务的 DB 写 + 外部 RPC”如何切分边界；若实际只有单次插入且没有原子性要求，可不加事务。
> - `getActivity` / `pageActivity` 是**单次查询**，**不加事务**。仅当查询方法需要“一致性快照”或项目明确要求从库事务语义时才考虑 `@Transactional(readOnly = true)`。
> - 完整对比、复杂场景与"是否需要事务"决策树见 [transaction-template.md](transaction-template.md)。

## 5. 最佳实践提示

- **仅必须时才用事务**：事务不是默认选项，先判断方法是否满足"多步 DB 写需原子性 / 需 FOR UPDATE 行锁幂等 / 需一致性快照"任一条件，满足才用事务。单次查询、单次插入/更新/删除、纯内存计算、纯外部调用**不加事务**。禁止给所有 Service 方法默认加 `@Transactional`，禁止给查询方法默认加 `@Transactional(readOnly = true)`（除非走从库或需一致性快照）。详见 [transaction-template.md](transaction-template.md) 的"是否需要事务"决策树。
- **事务优先用 `TransactionTemplate`**：确认需要事务后，涉及事务的 Service 方法首选编程式事务。事务 + 外部调用并存时，用 `transactionTemplate.execute(status -> { 纯 DB 操作 })` 包 DB，RPC/MQ/Redis 写在 `execute` 之外，精确切分边界。`@Transactional` 注解仅适合方法内纯 DB 操作（如纯 DB 写方法）。详见 [transaction-template.md](transaction-template.md)。
- 纯 DB 写方法若用 `@Transactional`，加在**实现类方法**上，接口上不加（Spring 代理基于类），`rollbackFor = Exception.class`。
- Service 内禁止手写 Param↔Entity 的 setter 转换，禁止直接调用 `FsBeanUtil` / MapStruct Converter，统一委托 `XxxAssembler`，转换逻辑集中可复用。
- Service 调 Repository 前，复杂查询入参应由 Assembler 从 Service `XxxParam` 转为 DAL `XxxCriteria`，禁止把 Service Param 直接传给 DAL。
- 一个 Service 方法内的跨多个 Repository 写操作必须在同一事务内（同一 `TransactionTemplate.execute` 块或同一 `@Transactional` 方法），**禁止**在 Manager 层加事务。
- **事务内禁止网络阻塞点**：事务块内（`TransactionTemplate.execute` 块或 `@Transactional` 方法）**严禁**调 FeignClient/RPC、读写 Redis、同步发送 MQ（`syncSend`）。这些操作会因网络超时拖长数据库事务持锁时间，导致连接池耗尽和锁竞争。外部调用/同步 MQ/Redis 操作统一挪到事务块外（`TransactionTemplate` 模式天然支持）或 `afterCommit` 阶段。事务内**允许**异步 MQ（`asyncSend`，不阻塞调用线程，但消费端必须幂等）。
- **幂等写操作必须用 FOR UPDATE 行锁 + 状态判断**：业务上需要幂等的写方法（重复回调、重复消费、重复点击创建等），事务块内先 `SELECT ... FOR UPDATE` 锁行，再判状态是否已处理，已处理直接幂等返回。详见 [idempotent-write.md](idempotent-write.md)。禁止仅靠 Redis 去重表作为唯一幂等保障。
- 调用外部服务建议放在事务提交后用 `TransactionSynchronizationManager.registerSynchronization` 在 `afterCommit` 执行，避免事务回滚但外部副作用已发生。
- 业务校验尽量委托 Manager（纯逻辑可复用），Service 只做编排，避免 Service 变成"巨型事务方法"。
- **最小化事务范围**：确认需要事务后，事务块（`execute` lambda 或 `@Transactional` 方法体）尽量只包必要的 DB 写，把校验、转换、外部调用挪到块外，缩短持锁时间。
- **日志打点**：Service 主流程方法入口打 `INFO`（方法名+关键入参），出口（每个 return 分支）打 `INFO`（方法名+关键出参），成对出现；每个业务中断点（抛 `BusinessException` 前）打 `WARN`（中断原因+入参/当前状态）；捕获系统异常打 `ERROR` 且**必须传 Throwable** 打堆栈 + 入参上下文。
