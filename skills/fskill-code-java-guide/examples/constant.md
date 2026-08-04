# 常量（Constant）写法

> 第二层原子事例 · 后端 Java 规范
> 适用场景：定义 Redis Key、MQ Topic/Tag、业务常量、错误码常量等
> 速查索引：见 [SKILL.md](../SKILL.md) §3 索引目录

---

## 1. 放置位置

| 类型 | 位置 | 适用场景 |
|------|------|---------|
| 通用常量 | `common.constant` | 跨业务复用，如 `CommonConstants`、`RedisKeyConstants`、`MqConstants` |
| service 模块内通用常量 | `service.common.constant` | 仅 service 模块跨子域共享，不对外暴露 |
| 业务子域常量 | `service.constant.{子域}` | 子域专属，如 `service.constant.activity.ActivityConstants` |
| 对外常量 | `api.constant` | 供外部调用方使用 |

## 2. 编写规范

- 常量类为 `final`，构造器私有化，禁止实例化。
- 常量统一 `public static final`，命名 `UPPER_SNAKE_CASE`。
- Redis Key、MQ Topic/Tag、缓存 Key 必须带命名空间前缀，避免冲突。
- 同一类常量按业务分组，用注释分隔。

## 3. 完整示例

### 3.1 通用常量

```java
package com.fshows.storemate.merchant.common.constant;

/**
 * 通用常量。
 */
public final class CommonConstants {

    private CommonConstants() {
    }

    // ====== 系统级 ======
    /** 系统默认时区 */
    public static final String DEFAULT_TIME_ZONE = "Asia/Shanghai";

    /** 默认分页大小 */
    public static final int DEFAULT_PAGE_SIZE = 20;

    /** 单次 IN 查询最大数量 */
    public static final int MAX_IN_SIZE = 1000;
}
```

### 3.2 Redis Key 常量

```java
package com.fshows.storemate.merchant.common.constant;

/**
 * Redis Key 常量。
 * 命名规范：业务域:子域:用途[:动态部分占位符 +]
 */
public final class RedisKeyConstants {

    private RedisKeyConstants() {
    }

    // ====== 活动域 ======
    /** 活动详情缓存，后接活动 ID */
    public static final String ACTIVITY_DETAIL = "storemate-merchant:activity:detail:";

    /** 活动库存分布式锁，后接活动 ID */
    public static final String ACTIVITY_STOCK_LOCK = "storemate-merchant:activity:stock:lock:";

    // ====== MQ 幂等去重 ======
    /** 活动创建消息幂等键，后接活动 ID */
    public static final String MQ_DEDUP_ACTIVITY_CREATE = "storemate-merchant:mq:dedup:activity-create:";
}
```

### 3.3 MQ Topic/Tag 常量

```java
package com.fshows.storemate.merchant.common.constant;

/**
 * RocketMQ Topic / Tag 常量。
 */
public final class MqConstants {

    private MqConstants() {
    }

    // ====== 活动域 Topic ======
    /** 活动主题 */
    public static final String TOPIC_ACTIVITY = "storemate-merchant-activity";

    /** 活动创建 Tag */
    public static final String TAG_ACTIVITY_CREATE = "activity-create";

    /** 活动状态变更 Tag */
    public static final String TAG_ACTIVITY_STATUS_CHANGE = "activity-status-change";
}
```

### 3.4 业务子域常量

```java
package com.fshows.storemate.merchant.service.constant.activity;

/**
 * 活动子域常量。
 */
public final class ActivityConstants {

    private ActivityConstants() {
    }

    /** 活动名称最大长度 */
    public static final int NAME_MAX_LENGTH = 50;

    /** 单用户活动库存上限 */
    public static final int MAX_STOCK_PER_USER = 100;
}
```

## 4. 最佳实践提示

- **禁止**在代码中硬编码 Redis Key 字符串，统一引用 `RedisKeyConstants`，拼接动态部分时用 `+` 而非 `String.format`，性能更好且语义清晰。
- **禁止**在代码中硬编码 MQ Topic/Tag，统一引用 `MqConstants`，便于消费端 producer/consumer 引用同一个常量避免拼错。
- Redis Key 命名空间前缀统一以 `storemate-merchant:` 开头，避免与其他应用冲突。
- 同一常量类按业务域分组并用 `// ====== xxx ======` 注释分隔，方便定位。
