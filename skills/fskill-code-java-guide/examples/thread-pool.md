# 线程池 / @Async 异步任务写法

> 第二层原子事例 · 后端 Java 规范
> 适用场景：异步任务、`@Async`、需要线程池的后台处理
> 速查索引：见 [SKILL.md](../SKILL.md) §2.7 线程池使用速查

---

## 1. 放置位置

| 类型 | 位置 | 命名 |
|------|------|------|
| 线程池配置 | `web.config` | `ThreadPoolConfig` |
| MDC 透传装饰器 | `web.config` | `MdcTaskDecorator` |

> 线程池配置统一放 `web.config`，由 web 模块提供 Bean，service 模块通过注入使用。禁止在 service / manager 内自建线程池。

## 2. 编写规范

- **必须用 `web.config.ThreadPoolConfig` 提供的 `taskExecutor` Bean**（`@Bean("taskExecutor") ThreadPoolTaskExecutor`）。业务代码注入它，或用 `@Async`（默认走 `taskExecutor`）。
- **禁止自建线程池**：禁止 `new ThreadPoolExecutor` / `new ThreadPoolTaskExecutor` / `Executors.newXxx` / `CompletableFuture.runAsync(task)`（默认走 ForkJoinPool，无 MDC 透传）。如需多池子，在 `ThreadPoolConfig` 内新增 `@Bean("xxxExecutor")` 并 `@Async("xxxExecutor")` 指定。
- **MDC 透传由 `MdcTaskDecorator` 自动完成**：`taskExecutor` 已配 `MdcTaskDecorator`（capture-replay 模式：提交方线程 capture MDC → 异步线程 run 前 set → run 完 clear），traceId 自动透传，业务无需处理。
- **虚拟线程兼容**：`spring.threads.virtual.enabled=true` 时 `taskExecutor` 自动 `setVirtualThreads(true)`，业务代码无感。`MdcTaskDecorator` 在虚拟线程下正常工作（同 Runnable 内 set/use/clear）。
- **异步任务异常必须处理**：`@Async` 方法返回 `void` 时异常静默丢失，必须 try-catch 记录 `LogUtil.error(log, "...", e, ...)` 传 Throwable；或返回 `CompletableFuture` 让调用方处理；或配全局 `AsyncUncaughtExceptionHandler`。
- **禁止自建 `ThreadLocal` 传 traceId 到异步线程**：虚拟线程下不保证同载体执行，会丢失。traceId 透传只能由 `MdcTaskDecorator` 处理。
- 类、方法必须有 JavaDoc。

## 3. 完整示例

### 3.1 MdcTaskDecorator（MDC 透传装饰器）

```java
package com.fshows.storemate.merchant.web.config;

import org.slf4j.MDC;
import org.springframework.core.task.TaskDecorator;

import java.util.Map;

/**
 * MDC 透传装饰器。
 * 在提交方线程 capture MDC 上下文，在异步线程 run 前 set、run 完 clear，
 * 实现 traceId 等 MDC 变量跨线程透传。虚拟线程兼容。
 */
public class MdcTaskDecorator implements TaskDecorator {

    /**
     * 装饰 Runnable，包装 MDC 透传逻辑。
     *
     * @param delegate 原始 Runnable
     * @return 包装后的 Runnable
     */
    @Override
    public Runnable decorate(Runnable delegate) {
        // 在提交方线程 capture MDC（traceId 等）
        Map<String, String> contextMap = MDC.getCopyOfContextMap();
        return () -> {
            // 在异步线程 run 前 set MDC
            if (contextMap != null) {
                MDC.setContextMap(contextMap);
            } else {
                MDC.clear();
            }
            try {
                delegate.run();
            } finally {
                // run 完清理，避免虚拟线程/线程池载体复用导致 traceId 串日志
                MDC.clear();
            }
        };
    }
}
```

### 3.2 ThreadPoolConfig（线程池配置）

```java
package com.fshows.storemate.merchant.web.config;

import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.scheduling.annotation.EnableAsync;
import org.springframework.scheduling.concurrent.ThreadPoolTaskExecutor;

import java.util.concurrent.ThreadPoolExecutor;

/**
 * 线程池配置。
 * 提供应用统一的异步任务线程池，配 MdcTaskDecorator 实现 traceId 透传。
 * 虚拟线程开启时（spring.threads.virtual.enabled=true）自动切换虚拟线程。
 */
@Configuration
@EnableAsync
public class ThreadPoolConfig {

    /** 默认异步任务线程池核心线程数 */
    private static final int CORE_POOL_SIZE = 8;

    /** 默认异步任务线程池最大线程数 */
    private static final int MAX_POOL_SIZE = 64;

    /** 默认异步任务线程池队列容量 */
    private static final int QUEUE_CAPACITY = 256;

    /**
     * 默认异步任务线程池。
     * @Async 默认走此 Bean；业务代码也可直接注入使用。
     *
     * @return 配置了 MdcTaskDecorator 的 ThreadPoolTaskExecutor
     */
    @Bean("taskExecutor")
    public ThreadPoolTaskExecutor taskExecutor() {
        ThreadPoolTaskExecutor executor = new ThreadPoolTaskExecutor();
        executor.setCorePoolSize(CORE_POOL_SIZE);
        executor.setMaxPoolSize(MAX_POOL_SIZE);
        executor.setQueueCapacity(QUEUE_CAPACITY);
        executor.setThreadNamePrefix("storemate-merchant-async-");
        executor.setRejectedExecutionHandler(new ThreadPoolExecutor.CallerRunsPolicy());
        // ✅ 配 MdcTaskDecorator，traceId 自动透传
        executor.setTaskDecorator(new MdcTaskDecorator());
        executor.initialize();
        return executor;
    }
}
```

### 3.3 业务 @Async 用法

```java
package com.fshows.storemate.merchant.service.service.activity.impl;

import com.fshows.storemate.merchant.common.util.LogUtil;
import com.fshows.storemate.merchant.service.service.activity.IActivityService;
import lombok.extern.slf4j.Slf4j;
import org.springframework.scheduling.annotation.Async;
import org.springframework.stereotype.Service;

/**
 * 活动服务实现（异步任务示例片段）。
 */
@Slf4j
@Service
public class ActivityServiceImpl implements IActivityService {

    /**
     * 异步处理活动后续逻辑。
     * traceId 由 MdcTaskDecorator 自动透传，无需手动处理。
     * 异步任务异常必须 try-catch 记录，否则静默丢失。
     *
     * @param activityId 活动 ID
     */
    @Async  // 默认走 taskExecutor
    public void processActivityAsync(Long activityId) {
        try {
            // 此处 MDC 中已有 traceId，LogUtil 打日志自动带 traceId
            LogUtil.info(log, "processActivityAsync >> 开始，activityId={}", activityId);
            // 异步任务内发 MQ / 调 Feign，traceId 已在 MDC，
            // MqMessageHelper / TraceIdFeignInterceptor 自动取用
            // ... 业务逻辑
            LogUtil.info(log, "processActivityAsync >> 完成，activityId={}", activityId);
        } catch (Exception e) {
            // ✅ 异步任务异常必须记录，传 Throwable 打堆栈
            LogUtil.error(log, "processActivityAsync >> 异常，activityId={}", e, activityId);
            // 按业务决定是否抛出（@Async void 抛出也无人捕获，建议记录后吞掉或落库补偿）
        }
    }
}
```

### 3.4 显式提交任务到 taskExecutor（非 @Async 场景）

```java
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.scheduling.concurrent.ThreadPoolTaskExecutor;

@Slf4j
@Service
public class XxxServiceImpl implements IXxxService {

    /** 默认异步任务线程池 */
    @Autowired
    @Qualifier("taskExecutor")
    private ThreadPoolTaskExecutor taskExecutor;

    /**
     * 同步方法内提交异步子任务。
     *
     * @param bizId 业务 ID
     */
    public void doSomething(Long bizId) {
        LogUtil.info(log, "doSomething >> 提交异步任务，bizId={}", bizId);
        // ✅ 用 taskExecutor 提交，MDC 由 MdcTaskDecorator 自动透传
        taskExecutor.execute(() -> {
            try {
                LogUtil.info(log, "asyncSubTask >> 开始，bizId={}", bizId);
                // ... 异步逻辑
            } catch (Exception e) {
                LogUtil.error(log, "asyncSubTask >> 异常，bizId={}", e, bizId);
            }
        });
    }
}
```

## 4. 最佳实践提示

- **traceId 透传是基础设施职责**：`MdcTaskDecorator` 自动处理 MDC 透传，业务代码只需用 `taskExecutor` 或 `@Async`，**禁止**自建 `ThreadLocal` 或 `new ThreadPoolExecutor`。
- **异步任务内若再发 MQ / 调 Feign**，traceId 已在 MDC，`MqMessageHelper` / `TraceIdFeignInterceptor` 自动取用，无需额外处理。
- **异步任务异常必须处理**：`@Async void` 方法异常静默丢失，必须 try-catch 记录 `LogUtil.error` 传 Throwable；或返回 `CompletableFuture` 让调用方处理；或配全局 `AsyncUncaughtExceptionHandler`。**禁止**吞异常。
- **禁止 `CompletableFuture.runAsync(task)` / `supplyAsync(task)`**：默认走 ForkJoinPool，无 `MdcTaskDecorator`，traceId 丢失。如需 CompletableFuture，用 `CompletableFuture.runAsync(task, taskExecutor)` 显式传 `taskExecutor`。
- **多池子场景**：如需独立线程池（如 IO 密集 vs CPU 密集），在 `ThreadPoolConfig` 内新增 `@Bean("xxxExecutor")`，业务用 `@Async("xxxExecutor")` 或 `@Qualifier("xxxExecutor")` 指定。每个 executor 都必须 `setTaskDecorator(new MdcTaskDecorator())`。
- **虚拟线程**：`spring.threads.virtual.enabled=true` 时 `ThreadPoolTaskExecutor` 自动用虚拟线程，`MdcTaskDecorator` 仍正常工作（同 Runnable 内 set/use/clear）。业务代码无需为虚拟线程做任何适配。
