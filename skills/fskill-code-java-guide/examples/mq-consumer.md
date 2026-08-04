# MQ 消费者写法

> 第二层原子事例 · 后端 Java 规范
> 适用场景：消费 RocketMQ 消息，业务处理 + 幂等 + 重试
> 速查索引：见 [SKILL.md](../SKILL.md) §3 索引目录

---

## 1. 放置位置

- Consumer 放 `service.mq.{子域}.consumer`，与 DTO / Producer 同子域包。

## 2. 编写规范

- 使用 `@RocketMQMessageListener` 注解声明 topic / tag / consumerGroup。
- 实现 `RocketMQListener<XxxMessage>` 接口，泛型为消息体 DTO。
- **必须**实现幂等（基于业务 ID + Redis 去重，或数据库唯一索引）。
- 消费失败**必须**记录日志，重试次数耗尽后进入死信队列，**禁止**吞掉异常。
- 消费逻辑禁止写在大方法里，按职责拆分私有方法。
- Topic/Tag 必须引用 `MqConstants` 常量，consumerGroup 用 `应用-子域-动作-consumer` 命名。
- **traceId 由 `TraceIdRocketMqConsumerHook` 自动恢复，业务代码无需感知**：`TraceIdMqHookRegistrar` 在容器启动时为所有 `DefaultRocketMQListenerContainer` 注册 `TraceIdRocketMqConsumerHook`，`consumeMessageBefore` 从 `MessageExt.getProperty("traceId")` 取 traceId 放入 MDC（为空则生成），`consumeMessageAfter` 自动清理。业务 `onMessage` 内**无需且禁止**手动处理 traceId。
- **禁止在 consumer 内自建 `ThreadLocal` 传 traceId**：虚拟线程（Java 25 + Spring Boot 4.1）下 `MessageConverter` 与 `onMessage` 不保证同载体执行，自定义 ThreadLocal 会丢失。traceId 透传只能由 `TraceIdRocketMqConsumerHook`（同 consume 调用栈内 set/clear）或 `MdcTaskDecorator`（异步子任务）处理。

## 3. 完整示例

### 3.1 消费者（Redis 幂等去重）

```java
package com.fshows.storemate.merchant.service.mq.activity.consumer;

import com.fshows.storemate.merchant.common.constant.MqConstants;
import com.fshows.storemate.merchant.common.constant.RedisKeyConstants;
import com.fshows.storemate.merchant.common.util.LogUtil;
import com.fshows.storemate.merchant.service.extension.redis.RedisUtils;
import com.fshows.storemate.merchant.service.mq.activity.dto.ActivityCreateMessage;
import lombok.extern.slf4j.Slf4j;
import org.apache.rocketmq.spring.annotation.RocketMQMessageListener;
import org.apache.rocketmq.spring.core.RocketMQListener;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Component;

import java.time.Duration;

/**
 * 活动创建消息消费者。
 */
@Slf4j
@Component
@RocketMQMessageListener(
        topic = MqConstants.TOPIC_ACTIVITY,
        selectorExpression = MqConstants.TAG_ACTIVITY_CREATE,
        consumerGroup = "storemate-merchant-activity-create-consumer"
)
public class ActivityCreateConsumer implements RocketMQListener<ActivityCreateMessage> {

    /** Redis 工具，用于幂等去重 */
    @Autowired
    private RedisUtils redisUtils;

    /** 业务服务（按需注入） */
    // @Autowired
    // private IActivityService activityService;

    /**
     * 消费活动创建消息。
     *
     * @param message 消息体
     */
    @Override
    public void onMessage(ActivityCreateMessage message) {
        LogUtil.info(log, "onMessage >> 收到消息，activityId={}", message.getActivityId());

        // 1. 幂等校验：基于活动 ID 去重
        String dedupKey = RedisKeyConstants.MQ_DEDUP_ACTIVITY_CREATE + message.getActivityId();
        boolean acquired = redisUtils.setIfAbsent(dedupKey, "1", Duration.ofHours(24));
        if (!acquired) {
            LogUtil.info(log, "onMessage >> 幂等跳过，activityId={}", message.getActivityId());
            return;
        }

        // 2. 业务处理
        try {
            doProcess(message);
        } catch (Exception e) {
            // 失败时清除幂等标记，允许重试
            redisUtils.delete(dedupKey);
            LogUtil.error(log, "onMessage >> 处理失败，activityId={}", e, message.getActivityId());
            throw e;
        }
    }

    /**
     * 实际业务处理。
     *
     * @param message 消息体
     */
    private void doProcess(ActivityCreateMessage message) {
        // TODO 业务处理逻辑，按职责拆分私有方法
        LogUtil.info(log, "doProcess >> 处理消息，activityId={}", message.getActivityId());
    }
}
```

> 注：示例中 `redisUtils.setIfAbsent` 是 Redisson 的 `trySet` 封装（仅当 key 不存在时设置），返回 `true` 表示抢占成功。

## 4. 最佳实践提示

- 幂等去重 key 必须带命名空间（如 `storemate-merchant:mq:dedup:activity-create:`），避免与缓存 key 冲突。
- 幂等 TTL 设为消息可能重发的最大间隔（一般 24h），过期后允许同 ID 消息重新处理（极少出现）。
- **失败时必须清除幂等标记**，否则消息重试时会被幂等拦截导致永远无法消费成功。
- 消费大方法拆分私有方法（`doProcess`、`doValidate`、`doPersist`），便于单测和异常定位。
- 业务处理失败抛异常让 RocketMQ 重试；重试次数（默认 16 次）耗尽后进入死信队列，需配合监控告警人工处理死信。
- **禁止**用 `try-catch` 吞掉异常返回成功，否则消息丢失且无日志可查。
- 消费者内的业务调用如果涉及事务，事务方法独立写在 Service，Consumer 只做编排，不要把 `@Transactional` 加在 `onMessage` 上（消息ACK 与事务边界混乱）。
- **Redis 幂等去重只是辅助**：Redis `setIfAbsent` 用于过滤明显重复，但 Redis 故障/过期会破幂等。真正的幂等保障在消费端调用的 Service 事务方法内，用 `SELECT ... FOR UPDATE` 行锁 + 状态判断实现。详见 [idempotent-write.md](idempotent-write.md)。
- **事务方法禁忌同样适用**：消费端调用的 Service `@Transactional` 方法内，禁止调 FeignClient/Redis/同步 MQ；如需发 MQ 通知下游，用 `afterCommit`。
- **traceId 透传是基础设施职责**：consumer 业务代码只管业务逻辑，traceId 由 `TraceIdRocketMqConsumerHook` 自动恢复到 MDC，`LogUtil` 打日志自动带 traceId。如需在 consumer 内发起异步子任务，注入 `web.config.ThreadPoolConfig` 的 `taskExecutor` 提交，MDC 由 `MdcTaskDecorator` 自动透传——**禁止**自建 `ThreadLocal` 或 `new ThreadPoolExecutor`。
