# 幂等写操作（FOR UPDATE 行锁 + 状态判断）写法

> 第二层原子事例 · 后端 Java 规范
> 适用场景：业务上需要幂等的写操作（重复点击创建、重复支付回调、重复消费消息触发的落库、重复状态变更等）
> 速查索引：见 [SKILL.md](../SKILL.md) 的事务/幂等红线与 [runtime-guardrails.md](../references/runtime-guardrails.md) 的事务禁忌。
> **强约束**：
> 1. 这是所有事务写方法的标配规范，凡事务方法涉及幂等写，必须遵循本模式。
> 2. **幂等写是"需要事务"的合法理由之一**：FOR UPDATE 行锁依赖事务生效，所以幂等写方法必须加事务。但反过来，**不需要幂等的单次写操作（如首次插入无并发风险）不加事务**。判断流程：先按 [transaction-template.md](transaction-template.md) 的"是否需要事务决策树"判断，确认需要事务（多步写或需幂等）后才用本模式。

---

## 1. 核心原则

1. **幂等必须在事务内通过数据库行锁 + 状态判断实现**，不依赖外部组件（Redis/MQ）作为唯一保障。
2. 步骤固定：`SELECT ... FOR UPDATE` 获取行锁 → 判断状态是否允许操作 → 已处理则直接返回（天然幂等）→ 未处理则执行业务写。
3. **禁止**仅靠 Redis 去重表做幂等（Redis 故障/过期会破幂等）；Redis 去重只能作为**辅助**（如过滤明显重复），不能替代 FOR UPDATE 行锁。
4. **禁止**仅靠"先查后写"不加锁（并发下两个事务都查到未处理，都执行写，破坏幂等）。

---

## 2. 适用与不适用场景

| 场景 | 是否用本模式 | 说明 |
|------|------------|------|
| 重复点击创建（同一业务单号创建记录） | 是 | 按**业务唯一键** FOR UPDATE 锁已有记录；无记录则靠唯一索引兜底防并发插入 |
| 重复支付回调（同一支付单号回调多次） | 是 | 按支付单号 FOR UPDATE 锁记录，判断状态（待支付→已支付才处理） |
| 重复消费 MQ 消息触发的落库 | 是 | 消费端调 Service，Service 内按业务 ID FOR UPDATE + 状态判断 |
| 重复状态变更（如重复下架活动） | 是 | 按主键 FOR UPDATE，判断当前状态是否允许变更到目标状态 |
| 纯查询 | 否 | 无写操作无需幂等 |
| 无并发风险的初始化数据写入 | 否 | 如系统启动初始化，无并发 |

---

## 3. 编写规范

- 幂等写方法**必须**加 `@Transactional(rollbackFor = Exception.class)`，FOR UPDATE 行锁依赖事务生效。
- FOR UPDATE 查询**必须**走命中索引的字段（业务唯一键/主键），避免锁全表。
- 状态判断要覆盖所有"已处理"状态，已处理直接 `return` 不抛异常（幂等的本意是重复请求返回成功语义）。
- 无记录场景（如首次创建）靠**数据库唯一索引**兜底防并发插入，捕获 `DuplicateKeyException` 转为查询已存在记录后走幂等返回。
- FOR UPDATE 查询写在事务方法**第一步**，锁持有到事务提交/回滚才释放。

---

## 4. 完整示例

### 4.1 重复支付回调幂等（按支付单号）

```java
package com.fshows.storemate.merchant.service.service.payment.impl;

import com.fshows.storemate.merchant.common.exception.BusinessException;
import com.fshows.storemate.merchant.common.exception.ErrorCodeEnum;
import com.fshows.storemate.merchant.dal.primary.payment.entity.PaymentOrderEntity;
import com.fshows.storemate.merchant.dal.primary.payment.repository.PaymentOrderRepository;
import com.fshows.storemate.merchant.service.service.payment.IPaymentCallbackService;
import com.fshows.storemate.merchant.service.service.payment.model.PaymentCallbackModel;
import com.fshows.storemate.merchant.service.service.payment.param.PaymentCallbackParam;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

/**
 * 支付回调业务服务实现。
 * 幂等保证：FOR UPDATE 行锁 + 状态判断。
 */
@Slf4j
@Service
public class PaymentCallbackServiceImpl implements IPaymentCallbackService {

    @Autowired
    private PaymentOrderRepository paymentOrderRepository;

    /**
     * 处理支付回调（幂等）。
     * 同一支付单号多次回调，只处理一次。
     *
     * @param param 回调入参
     * @return 回调处理结果
     */
    @Override
    @Transactional(rollbackFor = Exception.class)
    public PaymentCallbackModel handleCallback(PaymentCallbackParam param) {
        // 1. FOR UPDATE 获取行锁（按支付单号，必须命中唯一索引）
        PaymentOrderEntity order = paymentOrderRepository.findByPayNoForUpdate(param.getPayNo());
        if (order == null) {
            throw new BusinessException(ErrorCodeEnum.PAY_ORDER_NOT_FOUND);
        }

        // 2. 状态判断：已支付直接返回成功（幂等）
        if (PaymentStatusEnum.isSuccess(order.getStatus())) {
            LogUtil.info(log, "handleCallback >> 幂等返回，payNo={}, 已是成功状态，幂等返回", param.getPayNo());
            return toCallbackModel(order);
        }

        // 3. 校验状态流转：只有待支付才能转为已支付
        if (!PaymentStatusEnum.isPending(order.getStatus())) {
            throw new BusinessException(ErrorCodeEnum.PAY_STATUS_INVALID);
        }

        // 4. 执行业务写（事务内只做本地 DB 操作，禁止 RPC/Redis/同步MQ）
        order.setStatus(PaymentStatusEnum.SUCCESS.getCode());
        order.setPaidAmount(param.getPaidAmount());
        order.setPaidTime(param.getPaidTime());
        paymentOrderRepository.updateById(order);

        return toCallbackModel(order);
    }

    private PaymentCallbackModel toCallbackModel(PaymentOrderEntity order) {
        // 转换逻辑委托 Assembler，此处省略
        ...
    }
}
```

### 4.2 Repository 提供 FOR UPDATE 方法

```java
package com.fshows.storemate.merchant.dal.primary.payment.repository;

import com.fshows.storemate.merchant.dal.primary.payment.entity.PaymentOrderEntity;
import com.fshows.storemate.merchant.dal.primary.payment.mapper.PaymentOrderMapper;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Repository;

/**
 * 支付订单仓储。
 */
@Repository
public class PaymentOrderRepository {

    @Autowired
    private PaymentOrderMapper paymentOrderMapper;

    /**
     * 根据支付单号查询并加行锁（FOR UPDATE）。
     * 必须在事务内调用，锁持有到事务结束。
     *
     * @param payNo 支付单号（必须命中唯一索引）
     * @return 支付订单实体，不存在返回 null
     */
    public PaymentOrderEntity findByPayNoForUpdate(String payNo) {
        return paymentOrderMapper.selectByPayNoForUpdate(payNo);
    }

    public int updateById(PaymentOrderEntity entity) {
        return paymentOrderMapper.updateById(entity);
    }
}
```

### 4.3 Mapper + XML（FOR UPDATE 查询）

```java
@Mapper
public interface PaymentOrderMapper {

    /**
     * 根据支付单号查询并加行锁。
     *
     * @param payNo 支付单号
     * @return 支付订单实体
     */
    PaymentOrderEntity selectByPayNoForUpdate(@Param("payNo") String payNo);

    int updateById(PaymentOrderEntity entity);
}
```

```xml
<select id="selectByPayNoForUpdate" resultMap="BaseResultMap">
    SELECT <include refid="Base_Column_List"/>
    FROM t_payment_order
    WHERE pay_no = #{payNo}
    FOR UPDATE
</select>
```

> `pay_no` 必须建唯一索引，否则 FOR UPDATE 会锁全表或走间隙锁，影响并发。

### 4.4 重复创建幂等（业务唯一键 + 唯一索引兜底）

```java
@Override
@Transactional(rollbackFor = Exception.class)
public OrderModel createOrder(OrderCreateParam param) {
    // 1. 先按业务唯一键查（不加锁，快速判断是否已存在）
    OrderEntity exist = orderRepository.findByBizNo(param.getBizNo());
    if (exist != null) {
        // 已存在：幂等返回（可能是重复点击创建）
        LogUtil.info(log, "createOrder >> 幂等返回，bizNo={}, 幂等返回已存在订单", param.getBizNo());
        return orderAssembler.toModel(exist);
    }

    // 2. 首次创建：靠唯一索引兜底防并发插入
    OrderEntity entity = orderAssembler.toEntity(param);
    try {
        orderRepository.insert(entity);
    } catch (DuplicateKeyException e) {
        // 并发下另一事务先插入成功，本次插入失败，转查询已存在记录后幂等返回
        LogUtil.warn(log, "createOrder >> 并发冲突，bizNo={}, 转幂等查询", param.getBizNo());
        OrderEntity concurrent = orderRepository.findByBizNo(param.getBizNo());
        return orderAssembler.toModel(concurrent);
    }

    return orderAssembler.toModel(entity);
}
```

> `biz_no` 必须建唯一索引，DuplicateKeyException 才能兜底。

### 4.5 重复状态变更幂等（按主键 FOR UPDATE + 状态机判断）

```java
@Override
@Transactional(rollbackFor = Exception.class)
public void disableActivity(Long activityId) {
    // 1. FOR UPDATE 锁活动记录
    ActivityEntity entity = activityRepository.findByIdForUpdate(activityId);
    if (entity == null) {
        throw new BusinessException(ErrorCodeEnum.ACTIVITY_NOT_FOUND);
    }

    // 2. 状态判断：已是下架状态直接幂等返回
    if (ActivityStatusEnum.DISABLED.getCode().equals(entity.getStatus())) {
        LogUtil.info(log, "disableActivity >> 幂等返回，activityId={}, 幂等返回", activityId);
        return;
    }

    // 3. 校验状态流转是否允许
    if (!activityStatusManager.canTransit(entity.getStatus(), ActivityStatusEnum.DISABLED.getCode())) {
        throw new BusinessException(ErrorCodeEnum.ACTIVITY_STATUS_INVALID);
    }

    // 4. 执行更新
    activityRepository.updateStatus(activityId, ActivityStatusEnum.DISABLED.getCode());
}
```

---

## 5. 与其它规范的协同

- **与事务禁忌协同**：幂等写方法在事务内只做"FOR UPDATE 查询 + 状态判断 + 本地 DB 写"，**禁止**在事务内调 FeignClient/Redis/同步 MQ；外部副作用（发券、发 MQ、刷缓存）统一挪到 `afterCommit`。
- **与 MQ 消费幂等协同**：MQ 消费端去重（Redis `setIfAbsent`）只是**辅助**过滤明显重复，真正幂等保障在消费端调用的 Service 方法内用 FOR UPDATE + 状态判断。Redis 失效后仍能靠 DB 行锁保证幂等。
- **与分布式锁协同**：分布式锁（Redisson RLock）用于**跨进程串行化**（如库存扣减的并发控制），与 FOR UPDATE 行锁不互斥；分布式锁在事务**外**获取，事务内再用 FOR UPDATE 保证 DB 层幂等。典型模式：`redisLockTemplate.executeWithLock(key, () -> service.idempotentWrite(...))`。

---

## 6. 最佳实践提示

- FOR UPDATE 查询字段**必须命中索引**（唯一索引/主键），否则 MySQL 会锁全表（非索引字段行锁退化为表锁）或加间隙锁影响并发。
- 幂等返回时**不要抛异常**，返回成功语义（重复请求得到与首次相同的成功结果），否则前端会误判为失败重试。
- 状态判断要覆盖**所有已处理状态**，不只是目标状态。例如支付回调，已成功/已退款都算"已处理"，直接幂等返回。
- 无记录场景靠**唯一索引** + `DuplicateKeyException` 兜底，不要靠"先查无记录再插入"（并发下两个事务都查到无记录，都插入成功破坏唯一性）。
- FOR UPDATE 行锁持有时间 = 事务持锁时间，所以事务内**必须**遵守“禁止网络阻塞点”，否则锁持有时间被拖长，影响并发。
- 死锁风险：多个幂等方法交叉锁定多表时，**固定加锁顺序**（如按表名/主键 ID 升序加锁），避免循环等待。
