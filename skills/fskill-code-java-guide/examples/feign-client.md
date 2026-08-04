# FeignClient + 异常转换写法

> 第二层原子事例 · 后端 Java 规范
> 适用场景：调用外部服务的 HTTP 接口（Feign 声明式调用 + 降级）
> 速查索引：见 [SKILL.md](../SKILL.md) §3 索引目录

---

## 1. 放置位置

Feign 接口按「对外暴露契约」与「对内调用外部」分两类，分别放不同模块：

| 类型 | 位置 | 命名 | 说明 |
|------|------|------|------|
| Feign 契约接口（对外暴露，供其他应用调用本应用） | `api.{子域}` | `XxxApi` | `@FeignClient(name="本服务名", path="/{context-path}/rpc/{子域}", configuration=..., fallbackFactory=...)`，Java 方法签名返回业务 DTO |
| FeignClient 接口（对内调用外部服务） | `client.{子域}` | `XxxClient` | `@FeignClient(name="外部服务名", fallbackFactory=XxxClientFallbackFactory.class)` |
| FallbackFactory 降级工厂 | `client.{子域}` | `XxxClientFallbackFactory` | 网络/超时/熔断类失败统一抛 `RemoteCallException`，禁止返回伪造成功结果 |
| Client 入参/出参 DTO | `client.{子域}.dto` | `XxxForm` / `XxxResult` | 仅 `client` 模块使用；`api` 模块契约接口的入参/出参复用 `api.{子域}.dto` 的 `XxxRequest`/`XxxResponse` |

> **两类 Feign 接口的区别**：
> - `api.{子域}.XxxApi`（对外暴露契约）：定义本服务**被别人调**的接口，路径前缀 `/rpc/**`，由 `web.controller.internal.{子域}.XxxRpcController implements XxxApi` 实现。接口签名返回业务 DTO，HTTP 线上 `Result<T>` 由统一 Feign Decoder 解包。详见 `examples/controller.md` §4.2。
> - `client.{子域}.XxxClient`（对内调用外部）：定义本服务**调别人**的接口，必须强类型化，网络/熔断异常通过 `FallbackFactory` 转 `RemoteCallException`。

## 2. 职责边界

| 应做 | 禁止 |
|------|------|
| 调用外部 HTTP 服务 | 调内部 Service / Manager / Mapper |
| 配 FallbackFactory 统一转换网络异常 | 在 FeignClient 接口里写业务逻辑 |
| 入参/出参用 Form/Result 独立定义 | 直接传 web 层 Request / Service 层 Param |
| 网络/熔断失败抛 `RemoteCallException` | 返回带 error 字段的伪造成功对象 |

## 3. 编写规范

- 使用 `@FeignClient(name = "xxx-service", fallbackFactory = XxxClientFallbackFactory.class)`。
- 接口方法用 Spring MVC 注解（`@PostMapping` / `@GetMapping` / `@RequestBody`），路径与外部服务接口一致。
- 方法入参和出参必须是 JavaBean 或 `List<JavaBean>`，禁止 `Map`、`Object`、`String`、`Integer` 等无结构类型；所有参数必须在 `client.{子域}.dto` 中以 `XxxForm` / `XxxResult` 独立定义。
- 必须配 FallbackFactory，网络/超时/熔断类失败统一抛 `RemoteCallException`；只有业务明确允许降级时，才可返回真实降级数据。
- 类、方法必须有 JavaDoc。
- **traceId 由 `TraceIdFeignInterceptor` 自动透传，业务代码禁止手动处理**：`TraceIdFeignInterceptor`（`@Component`，位于 `client.interceptor`，全局生效）在 `apply(RequestTemplate)` 时自动从 MDC 取 traceId 塞入请求 header `X-Trace-Id`。业务代码**禁止**在 FeignClient 接口方法上手动加 `@RequestHeader("X-Trace-Id")`，也**禁止**在调用方手动设 header。

## 4. 完整示例

### 4.1 FeignClient 接口

```java
package com.fshows.storemate.merchant.client.coupon;

import com.fshows.storemate.merchant.client.coupon.dto.CouponGrantForm;
import com.fshows.storemate.merchant.client.coupon.dto.CouponGrantResult;

import org.springframework.cloud.openfeign.FeignClient;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;

/**
 * 优惠券外部服务调用。
 */
@FeignClient(name = "coupon-service", fallbackFactory = CouponClientFallbackFactory.class)
public interface CouponClient {

    /**
     * 发放优惠券。
     *
     * @param form 发券请求
     * @return 发券结果
     */
    @PostMapping("/coupon/grant")
    CouponGrantResult grantCoupon(@RequestBody CouponGrantForm form);
}
```

### 4.2 FallbackFactory 异常转换

```java
package com.fshows.storemate.merchant.client.coupon;

import com.fshows.storemate.merchant.client.coupon.dto.CouponGrantForm;
import com.fshows.storemate.merchant.client.coupon.dto.CouponGrantResult;
import com.fshows.storemate.merchant.common.exception.RemoteCallException;
import com.fshows.storemate.merchant.common.util.LogUtil;
import lombok.extern.slf4j.Slf4j;
import org.springframework.cloud.openfeign.FallbackFactory;
import org.springframework.stereotype.Component;

/**
 * 优惠券服务降级工厂。
 */
@Slf4j
@Component
public class CouponClientFallbackFactory implements FallbackFactory<CouponClient> {

    /**
     * 创建降级代理。
     *
     * @param cause 触发降级的异常
     * @return 降级代理
     */
    @Override
    public CouponClient create(Throwable cause) {
        return form -> {
            LogUtil.warn(log, "grantCoupon >> 远程调用降级，activityId={}, reason={}",
                    form.getActivityId(), cause == null ? "unknown" : cause.getMessage());
            throw new RemoteCallException("调用优惠券服务失败", cause, "coupon-service",
                    "CouponClient#grantCoupon", null, null,
                    cause == null ? null : cause.getMessage(), null, true);
        };
    }
}
```

### 4.3 Service 中调用 FeignClient

```java
@Autowired
private CouponClient couponClient;

public void grantCouponAfterCreate(Long activityId) {
    CouponGrantForm form = new CouponGrantForm();
    form.setActivityId(activityId);
    CouponGrantResult result = couponClient.grantCoupon(form);
    // 网络/超时/熔断类失败已在 FallbackFactory 中统一转 RemoteCallException
    // 外部服务返回业务失败时，按对方协议在 client 适配层转 BusinessException / RemoteBusinessException
}
```

## 5. 最佳实践提示

- FeignClient **禁止**调用本应用内部 Service，仅用于调用外部服务；应用内调用直接注入 `IXxxService` 接口。
- FallbackFactory 默认**必须抛 `RemoteCallException`**，禁止返回带 `error` 字段或 `success=false` 的伪造成功对象；只有业务明确允许降级时，才能返回真实降级数据。
- 外部服务若也使用标准 `Result<T>` 协议，应通过统一 Decoder 解包；若是第三方私有协议，只能在 client 适配层转换成内部异常或业务 DTO，禁止把第三方响应壳泄漏到 Service。
- Feign 超时配置统一在 `bootstrap.yml` 或 Nacos 中管理，禁止在代码里硬编码超时。
- **事务内禁止调 FeignClient**：`@Transactional` 方法内**严禁**调用 FeignClient/RPC，网络超时会拖长数据库事务持锁时间，导致连接池耗尽和锁竞争。外部调用统一挪到事务 `afterCommit` 阶段（用 `TransactionSynchronizationManager.registerSynchronization`）或事务方法之外。若业务允许"主流程成功但发券失败"，把发券挪到 `afterCommit` 或异步发 MQ 由下游补偿。
- **traceId 出站透传由 `TraceIdFeignInterceptor` 统一处理**：业务代码无需也不应手动设 `X-Trace-Id` header。下游服务应从 `X-Trace-Id` header 取 traceId 放入自己的 MDC（若下游也遵循本规范则由其 `TraceIdFilter` 自动完成）。
