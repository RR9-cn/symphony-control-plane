# MQ 消息体 DTO 写法

> 第二层原子事例 · 后端 Java 规范
> 适用场景：RocketMQ 生产者发送 / 消费者接收的消息载体
> 速查索引：见 [SKILL.md](../SKILL.md) §3 索引目录

---

## 1. 放置位置

- 消息体 DTO 放 `service.mq.{子域}.dto`，与 Producer/Consumer 同子域包。

## 2. 命名规则

| 类型 | 命名 | 示例 |
|------|------|------|
| 消息体 | `XxxMessage` | `ActivityCreateMessage` |

## 3. 编写规范

- 使用 `@Data`，统一 JSON 序列化。
- 字段必须有 JavaDoc。
- 必须包含**业务唯一键**（如 `activityId`、`orderId`），用于消费端幂等去重。
- 包含**事件时间** `eventTime`，便于消费端按时间排序/补偿。
- 禁止把 Entity 直接当消息体，必须独立定义，避免内部字段变更波及消费方。
- 禁止在消息体里塞大对象（如完整 Entity 列表），消息体只放最小必要信息，消费端按 ID 回查业务数据。

## 4. 完整示例

### 4.1 活动创建消息体

```java
package com.fshows.storemate.merchant.service.mq.activity.dto;

import lombok.Data;

import java.time.LocalDateTime;

/**
 * 活动创建消息体。
 */
@Data
public class ActivityCreateMessage {

    /** 活动 ID（业务唯一键，用于消费端幂等去重） */
    private Long activityId;

    /** 活动名称 */
    private String name;

    /** 发生时间 */
    private LocalDateTime eventTime;
}
```

### 4.2 活动状态变更消息体

```java
package com.fshows.storemate.merchant.service.mq.activity.dto;

import lombok.Data;

import java.time.LocalDateTime;

/**
 * 活动状态变更消息体。
 */
@Data
public class ActivityStatusChangeMessage {

    /** 活动 ID */
    private Long activityId;

    /** 变更前状态 */
    private Integer fromStatus;

    /** 变更后状态 */
    private Integer toStatus;

    /** 发生时间 */
    private LocalDateTime eventTime;
}
```

## 5. 最佳实践提示

- 消息体字段类型必须是可 JSON 序列化的（POJO / 基础类型 / `LocalDateTime`），禁止放 `Map<Object, Object>` 这种难以反序列化的结构。
- 消费端反序列化依赖类路径一致，生产/消费两端 DTO 字段必须严格对齐，新增字段要保证旧消息能反序列化（消费端字段保持兼容，禁止直接改字段名/类型）。
- 消息体不要带可变状态字段过多，理想情况只带 `业务ID + 事件类型 + eventTime`，消费端按 ID 回查最新数据，避免消息体在队列里排队期间数据已变更导致消费时拿到脏值。
