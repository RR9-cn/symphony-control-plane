# MQ 生产者写法

> 第二层原子事例 · 后端 Java 规范
> 适用场景：发送 RocketMQ 消息（同步/异步）
> 速查索引：见 [SKILL.md](../SKILL.md) §3 索引目录

---

## 1. 放置位置

- 业务 Producer 放 `service.mq.{子域}.producer`，与 DTO / Consumer 同子域包。
- 事务感知 MQ 发送封装放 `service.extension.mq`，命名 `TransactionalMqSender`，由它统一注入 `RocketMQTemplate`。

## 2. 编写规范

- 业务 Producer 注入 `TransactionalMqSender`，不要直接注入 `RocketMQTemplate`；只有统一封装内部可以直接调用 `RocketMQTemplate`。
- 消息体使用 JSON 序列化，DTO 用 `@Data`。
- Topic/Tag 必须引用 `MqConstants` 常量，禁止硬编码。
- **必须**设置 `keys`，便于 RocketMQ 轨迹追踪（一般用业务 ID）；`TransactionalMqSender` 会校验 keys 非空。
- **必须**由 `TransactionalMqSender` 统一构建消息，它内部调用 `MqMessageHelper.buildMessage(payload)` 自动从 MDC 取 traceId 塞入 message property `traceId`，并追加业务 `KEYS` header。
- 业务 Producer 对外方法通常返回 `void`：如果处于事务中，消息要到 `afterCommit` 才真实发送，调用点拿不到真实 `msgId`。
- 若业务必须持久化 `msgId` 或要求“DB 提交成功但 MQ 发送失败”可补偿，必须设计 outbox / send_log 表；不要依赖 afterCommit 回调返回值。
- **事务内发送 MQ 的硬约束**：
  - **禁止**业务代码在 `@Transactional` 方法内直接 `RocketMQTemplate.syncSend()`。
  - **禁止**业务 Service 手写 `TransactionSynchronizationManager.registerSynchronization`；统一调用 Producer，Producer 再调用 `TransactionalMqSender`。
  - `TransactionalMqSender` 发现当前有真实事务时自动注册 `afterCommit`；无事务时立即发送。
  - 消费端仍必须做幂等：afterCommit 只能避免“事务回滚但消息已发”，不能解决“提交成功但 MQ 发送失败”。

## 3. 完整示例

### 3.1 统一发送封装

```java
package com.fshows.storemate.merchant.service.extension.mq;

import com.fshows.storemate.merchant.common.util.LogUtil;
import com.fshows.storemate.merchant.service.common.util.MqMessageHelper;
import lombok.extern.slf4j.Slf4j;
import org.apache.rocketmq.client.producer.SendCallback;
import org.apache.rocketmq.client.producer.SendResult;
import org.apache.rocketmq.spring.core.RocketMQTemplate;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.messaging.Message;
import org.springframework.stereotype.Component;
import org.springframework.transaction.support.TransactionSynchronization;
import org.springframework.transaction.support.TransactionSynchronizationManager;

/**
 * 事务感知的 RocketMQ 发送封装。
 */
@Slf4j
@Component
public class TransactionalMqSender {

    /** RocketMQ 模板 */
    @Autowired
    private RocketMQTemplate rocketMQTemplate;

    /**
     * 同步发送 MQ；有事务时延后到 afterCommit，无事务时立即发送。
     *
     * @param destination 发送目标，格式为 topic:tag
     * @param payload     消息体
     * @param keys        业务追踪键
     * @param <T>         消息体类型
     */
    public <T> void syncSendAfterCommit(String destination, T payload, Object keys) {
        String keyValue = requireKeys(keys);
        Message<T> message = buildMessage(payload, keyValue);
        executeAfterCommit("syncSendAfterCommit", destination, keyValue, () -> {
            SendResult sendResult = rocketMQTemplate.syncSend(destination, message);
            LogUtil.info(log, "syncSendAfterCommit >> MQ 同步发送成功, destination={}, keys={}, msgId={}",
                    destination, keyValue, sendResult.getMsgId());
        });
    }

    /**
     * 异步发送 MQ；有事务时延后到 afterCommit，无事务时立即发送。
     *
     * @param destination 发送目标，格式为 topic:tag
     * @param payload     消息体
     * @param keys        业务追踪键
     * @param callback    发送回调
     * @param <T>         消息体类型
     */
    public <T> void asyncSendAfterCommit(String destination, T payload, Object keys, SendCallback callback) {
        String keyValue = requireKeys(keys);
        Message<T> message = buildMessage(payload, keyValue);
        executeAfterCommit("asyncSendAfterCommit", destination, keyValue,
                () -> rocketMQTemplate.asyncSend(destination, message, callback));
    }

    private <T> Message<T> buildMessage(T payload, String keys) {
        return MqMessageHelper.buildMessage(payload)
                .setHeader("KEYS", keys)
                .build();
    }

    private void executeAfterCommit(String operation, String destination, String keys, Runnable sendAction) {
        if (!TransactionSynchronizationManager.isSynchronizationActive()
                || !TransactionSynchronizationManager.isActualTransactionActive()) {
            sendAction.run();
            return;
        }
        TransactionSynchronizationManager.registerSynchronization(new TransactionSynchronization() {
            @Override
            public void afterCommit() {
                sendAction.run();
            }
        });
    }

    private String requireKeys(Object keys) {
        if (keys == null || String.valueOf(keys).isBlank()) {
            throw new IllegalArgumentException("MQ keys must not be blank");
        }
        return String.valueOf(keys);
    }
}
```

### 3.2 业务 Producer

```java
package com.fshows.storemate.merchant.service.mq.activity.producer;

import com.fshows.storemate.merchant.common.constant.MqConstants;
import com.fshows.storemate.merchant.common.util.LogUtil;
import com.fshows.storemate.merchant.service.extension.mq.TransactionalMqSender;
import com.fshows.storemate.merchant.service.mq.activity.dto.ActivityCreateMessage;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Component;

/**
 * 活动消息生产者。
 */
@Slf4j
@Component
public class ActivityMessageProducer {

    /** 事务感知 MQ 发送器 */
    @Autowired
    private TransactionalMqSender transactionalMqSender;

    /**
     * 发送活动创建消息。
     * 有事务时在 afterCommit 阶段同步发送；无事务时立即同步发送。
     *
     * @param message 消息体
     */
    public void sendActivityCreate(ActivityCreateMessage message) {
        String destination = MqConstants.TOPIC_ACTIVITY + ":" + MqConstants.TAG_ACTIVITY_CREATE;
        LogUtil.info(log, "sendActivityCreate >> 发送消息, destination={}, activityId={}",
                destination, message.getActivityId());
        transactionalMqSender.syncSendAfterCommit(destination, message, message.getActivityId());
    }
}
```

### 3.3 Service 中调用 Producer

```java
@Transactional(rollbackFor = Exception.class)
public ActivityModel createActivity(ActivityCreateParam param) {
    // ... 业务逻辑，落库
    ActivityEntity entity = activityAssembler.toEntity(param, initStatus);
    activityRepository.save(entity);

    ActivityCreateMessage message = new ActivityCreateMessage();
    message.setActivityId(entity.getId());
    message.setName(entity.getName());
    message.setEventTime(LocalDateTime.now());

    // Service 不手写 afterCommit；由 Producer + TransactionalMqSender 判断发送时机
    activityMessageProducer.sendActivityCreate(message);
    return activityAssembler.toModel(entity);
}
```

## 4. 最佳实践提示

- 统一发送封装是基础设施职责：`TransactionalMqSender` 负责 `RocketMQTemplate`、`MqMessageHelper`、KEYS、traceId 和 afterCommit 判断。
- 业务 Producer 只负责选择 Topic/Tag、组装业务 keys 和补充业务日志。
- `syncSendAfterCommit` 不返回 `msgId`；发送成功后由统一封装记录 `msgId`。确需持久化 `msgId` 的关键消息，必须引入 outbox / send_log 表和补偿任务。
- `KEYS` 必须设为业务唯一键（如 `activityId`），便于在 RocketMQ 控制台按 key 查消息轨迹。
- Producer 发送失败时必须有错误日志；提交后失败无法回滚主事务，业务关键消息要落库走补偿，禁止吞掉异常。
- traceId 透传是基础设施职责：业务代码只调用 Producer / `TransactionalMqSender`，禁止手动 `setHeader("traceId", ...)` 或自建 ThreadLocal 传 traceId。
