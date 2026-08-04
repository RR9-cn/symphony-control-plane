# RedisUtils / RedisLockTemplate（Redisson）写法

> 第二层原子事例 · 后端 Java 规范
> 适用场景：缓存读写、分布式锁、原子计数
> 速查索引：见 [SKILL.md](../SKILL.md) §3 索引目录

---

## 1. 放置位置

- `RedisUtils` 放 `service.extension.redis`，负责缓存读写、删除、存在判断、原子计数等 Redis 基础操作，底层使用 **Redisson**。
- `RedisLockTemplate` 放 `service.extension.redis`，负责分布式锁模板，底层使用 Redisson `RLock`。
- Redis Key 常量放 `common.constant.RedisKeyConstants`。
- 业务缓存逻辑放 Service 层，禁止在 Controller / Manager 直接操作 Redis。
- `common.util` 只放纯静态无状态工具，禁止放 `@Component` + `@Autowired RedissonClient` 这类 Spring Bean。

## 2. 编写规范

- **统一使用 Redisson**，禁止直接用 `StringRedisTemplate` / `RedisTemplate`。
- **分布式锁统一使用 `RedisLockTemplate`**，业务代码禁止直接注入 `RedissonClient`、直接操作 `RLock`，也禁止在 `RedisUtils` 里新增锁方法。
- Key 必须引用 `RedisKeyConstants` 常量，禁止硬编码。
- 缓存写入必须设置过期时间，禁止写不设 TTL 的缓存。
- 序列化统一用 JSON，禁止 JDK 序列化。
- 分布式锁使用 Redisson `RLock`，支持可重入、看门狗自动续期；禁止裸 `setIfAbsent` 实现锁。
- 单个 Value 不超过 10KB，List/Hash 元素不超过 1 万，禁止大 Key。
- Redis/Redisson 是网络调用，禁止放在 `@Transactional` 方法内；分布式锁必须在事务外获取。

## 3. 完整示例

### 3.1 RedisLockTemplate（分布式锁模板）

```java
package com.fshows.storemate.merchant.service.extension.redis;

import com.fshows.storemate.merchant.common.util.LogUtil;
import lombok.extern.slf4j.Slf4j;
import org.redisson.api.RLock;
import org.redisson.api.RedissonClient;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Component;

import java.util.concurrent.TimeUnit;
import java.util.function.Supplier;

/**
 * Redis 分布式锁模板。
 * 基于 Redisson RLock 封装锁获取、异常处理和 finally 释放，业务代码禁止直接操作 RLock。
 */
@Slf4j
@Component
public class RedisLockTemplate {

    /** Redisson 客户端 */
    @Autowired
    private RedissonClient redissonClient;

    /**
     * 使用默认参数获取分布式锁并执行任务。
     * 默认最多等待 3 秒，leaseTime 为 -1，启用 Redisson 看门狗自动续期。
     *
     * @param key  锁的 key
     * @param task 待执行任务
     * @param <T>  返回类型
     * @return 任务结果，获取锁失败或线程中断时返回 null
     */
    public <T> T executeWithLock(String key, Supplier<T> task) {
        return executeWithLock(key, 3, -1, TimeUnit.SECONDS, task);
    }

    /**
     * 尝试获取分布式锁并执行任务。
     *
     * @param key       锁的 key
     * @param waitTime  等待时间
     * @param leaseTime 持有时间，-1 表示启用 Redisson 看门狗自动续期
     * @param unit      时间单位
     * @param task      待执行任务
     * @param <T>       返回类型
     * @return 任务结果，获取锁失败或线程中断时返回 null
     */
    public <T> T executeWithLock(String key, long waitTime, long leaseTime, TimeUnit unit, Supplier<T> task) {
        RLock lock = redissonClient.getLock(key);
        boolean locked = false;
        try {
            locked = lock.tryLock(waitTime, leaseTime, unit);
            if (!locked) {
                LogUtil.warn(log, "executeWithLock >> 获取锁失败，key={}", key);
                return null;
            }
            return task.get();
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            LogUtil.error(log, "executeWithLock >> 获取锁被中断，key={}", e, key);
            return null;
        } finally {
            if (locked && lock.isHeldByCurrentThread()) {
                lock.unlock();
            }
        }
    }
}
```

### 3.2 RedisUtils（缓存/计数工具）

```java
package com.fshows.storemate.merchant.service.extension.redis;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.SerializationFeature;
import com.fasterxml.jackson.datatype.jsr310.JavaTimeModule;
import com.fshows.storemate.merchant.common.util.LogUtil;
import lombok.extern.slf4j.Slf4j;
import org.redisson.api.RBucket;
import org.redisson.api.RedissonClient;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Component;

import java.time.Duration;
import java.util.List;
import java.util.concurrent.TimeUnit;

/**
 * Redis 缓存与原子操作工具类。
 * 分布式锁统一使用 RedisLockTemplate，禁止在本类新增锁 API。
 */
@Slf4j
@Component
public class RedisUtils {

    /** Redisson 客户端 */
    @Autowired
    private RedissonClient redissonClient;

    /** JSON 序列化 */
    private final ObjectMapper objectMapper = new ObjectMapper()
            .registerModule(new JavaTimeModule())
            .disable(SerializationFeature.WRITE_DATES_AS_TIMESTAMPS);

    public void set(String key, String value, Duration ttl) {
        RBucket<String> bucket = redissonClient.getBucket(key);
        if (ttl == null) {
            bucket.set(value);
            return;
        }
        bucket.set(value, ttl.toMillis(), TimeUnit.MILLISECONDS);
    }

    public boolean setIfAbsent(String key, String value, Duration ttl) {
        RBucket<String> bucket = redissonClient.getBucket(key);
        if (ttl == null) {
            return bucket.setIfAbsent(value);
        }
        return bucket.setIfAbsent(value, ttl);
    }

    public void setObject(String key, Object value, Duration ttl) {
        try {
            set(key, objectMapper.writeValueAsString(value), ttl);
        } catch (Exception e) {
            LogUtil.error(log, "setObject >> 写入失败，key={}", e, key);
            throw new RuntimeException("Redis 写入失败", e);
        }
    }

    public String get(String key) {
        return redissonClient.getBucket(key).get();
    }

    public <T> T getObject(String key, Class<T> valueType) {
        String json = get(key);
        if (json == null || json.isEmpty()) {
            return null;
        }
        try {
            return objectMapper.readValue(json, valueType);
        } catch (Exception e) {
            LogUtil.error(log, "getObject >> 反序列化失败，key={}", e, key);
            return null;
        }
    }

    public <T> List<T> getList(String key, TypeReference<List<T>> typeReference) {
        String json = get(key);
        if (json == null || json.isEmpty()) {
            return null;
        }
        try {
            return objectMapper.readValue(json, typeReference);
        } catch (Exception e) {
            LogUtil.error(log, "getList >> 反序列化失败，key={}", e, key);
            return null;
        }
    }

    public boolean delete(String key) {
        return redissonClient.getBucket(key).delete();
    }

    public boolean exists(String key) {
        return redissonClient.getBucket(key).isExists();
    }

    public long increment(String key, long delta) {
        return redissonClient.getAtomicLong(key).addAndGet(delta);
    }
}
```

### 3.3 业务缓存示例（带防穿透）

```java
@Service
public class ActivityQueryServiceImpl implements IActivityQueryService {

    @Autowired
    private ActivityRepository activityRepository;

    @Autowired
    private RedisUtils redisUtils;

    @Autowired
    private ActivityAssembler activityAssembler;

    @Override
    // 不加 @Transactional：单次查询 + 缓存读本就不需要事务；事务内禁止调 Redis。
    public ActivityModel getActivity(Long id) {
        String cacheKey = RedisKeyConstants.ACTIVITY_DETAIL + id;
        ActivityModel cached = redisUtils.getObject(cacheKey, ActivityModel.class);
        if (cached != null) {
            return cached;
        }

        ActivityEntity entity = activityRepository.findById(id);
        if (entity == null) {
            redisUtils.set(cacheKey, "", Duration.ofSeconds(30));
            throw new BusinessException(ErrorCodeEnum.ACTIVITY_NOT_FOUND);
        }

        ActivityModel model = activityAssembler.toModel(entity);
        redisUtils.setObject(cacheKey, model, Duration.ofMinutes(10));
        return model;
    }
}
```

### 3.4 分布式锁 + 事务拆分示例

> **关键约束**：分布式锁必须在事务外获取，事务方法内禁止调 Redis。
> 模式：外层方法获取分布式锁（无事务） → 锁内调用独立的 `@Transactional` 内层方法（纯 DB 操作，用 FOR UPDATE 幂等）。

```java
@Service
public class ActivityStockServiceImpl implements IActivityStockService {

    @Autowired
    private RedisLockTemplate redisLockTemplate;

    @Autowired
    private ActivityRepository activityRepository;

    /** 自注入代理，确保 doDeductStockInTx 的 @Transactional 生效 */
    @Autowired
    private IActivityStockService self;

    @Override
    public boolean deductStock(Long activityId, Integer quantity) {
        String lockKey = RedisKeyConstants.ACTIVITY_LOCK + activityId;

        Boolean result = redisLockTemplate.executeWithLock(lockKey, 3, -1, TimeUnit.SECONDS,
                () -> self.doDeductStockInTx(activityId, quantity));

        return Boolean.TRUE.equals(result);
    }

    @Override
    @Transactional(rollbackFor = Exception.class)
    public boolean doDeductStockInTx(Long activityId, Integer quantity) {
        ActivityEntity entity = activityRepository.findByIdForUpdate(activityId);
        if (entity == null) {
            throw new BusinessException(ErrorCodeEnum.ACTIVITY_NOT_FOUND);
        }
        if (entity.getStock() < quantity) {
            throw new BusinessException(ErrorCodeEnum.ACTIVITY_STOCK_NOT_ENOUGH);
        }
        entity.setStock(entity.getStock() - quantity);
        activityRepository.updateById(entity);
        return true;
    }
}
```

> **`doDeductStockInTx` 必须声明在 `IActivityStockService` 接口中**，否则 `self` 代理调用不到。

## 4. 最佳实践提示

- **缓存穿透**：查询为空时缓存空值（短 TTL，如 30s），避免恶意请求穿透到数据库。
- **缓存击穿**：热点 Key 用 `RedisLockTemplate` 或逻辑过期，避免同时回源压垮数据库。
- **分布式锁**：`leaseTime = -1` 启用 Redisson 看门狗自动续期；执行完必须在 finally 中 `unlock()` 且判断 `isHeldByCurrentThread()`，这部分由 `RedisLockTemplate` 统一处理。
- **锁失败语义**：`RedisLockTemplate#executeWithLock` 获取锁失败或线程中断时返回 `null`，业务代码必须显式处理 `null`，不要把锁失败当成业务成功。
- **大 Key 禁止**：单个 Value > 10KB 或 List/Hash 元素 > 1 万时拆分，否则会阻塞 Redis 单线程。
- 缓存更新策略推荐“先更新数据库再删缓存”（Cache Aside），避免先删缓存再更库导致的脏读。
- **事务内禁止调 Redis**：`@Transactional` 方法内严禁任何 `RedisUtils`、`RedisLockTemplate`、`RedissonClient` 调用。缓存读写在事务外完成；分布式锁在事务外获取，锁内调用独立的纯 DB 事务方法。
- **幂等写用 FOR UPDATE 行锁**：分布式锁只负责跨进程串行化，不替代 DB 幂等；事务内扣减/状态变更仍要用 `SELECT ... FOR UPDATE` 锁行 + 状态判断。
