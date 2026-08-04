# Client 层 DTO（Form / Result）写法

> 第二层原子事例 · 后端 Java 规范
> 适用场景：FeignClient 调用外部服务的入参/出参
> 速查索引：见 [SKILL.md](../SKILL.md) §3 索引目录

---

## 1. 放置位置

- `Form` / `Result` 放 `client.{子域}.dto`。
- 与 FeignClient 接口同子域包，便于查找。

## 2. 命名规则

| 类型 | 命名 | 示例 |
|------|------|------|
| Client 入参 | `XxxForm` | `CouponGrantForm` |
| Client 出参 | `XxxResult` | `CouponGrantResult` |

## 3. 编写规范

- 使用 `@Data`。
- 字段必须有 JavaDoc。
- Form 字段名与外部服务接口约定的请求体字段名一致（驼峰），由 Feign 序列化为 JSON。
- FeignClient 的入参和出参必须是 JavaBean 或 `List<JavaBean>`；禁止 `Map`、`Object`、`String`、`Integer` 等无结构类型，避免序列化/反序列化结果不可控。
- 禁止把 web 层 `Request` 或 Service 层 `Param` 直接传给 FeignClient，必须独立定义 Form，避免外部协议变更波及内部 DTO。

## 4. 完整示例

### 4.1 发券请求 Form

```java
package com.fshows.storemate.merchant.client.coupon.dto;

import lombok.Data;

/**
 * 优惠券发放请求 DTO。
 */
@Data
public class CouponGrantForm {

    /** 活动 ID */
    private Long activityId;

    /** 发券数量 */
    private Integer quantity;
}
```

### 4.2 发券响应 Result

```java
package com.fshows.storemate.merchant.client.coupon.dto;

import lombok.Data;

/**
 * 优惠券发放结果 DTO。
 */
@Data
public class CouponGrantResult {

    /** 发券流水号 */
    private String grantNo;

    /** 是否成功 */
    private Boolean success;

    /** 失败原因（success=false 时返回） */
    private String failReason;
}
```

## 5. 最佳实践提示

- Form 字段名要和外部服务接口文档完全一致，Feign 默认 Jackson 序列化为驼峰 JSON，若外部用下划线需在 Feign 配置里改 `PropertyNamingStrategy`，不要在 Form 里加 `@JsonProperty` 单独改（保持类内纯净）。
- 外部私有协议若自带 `success`/`failReason` 字段，只能停留在 client 适配层；Service 层不直接感知第三方响应壳。网络/熔断异常由 FallbackFactory 统一转 `RemoteCallException`，禁止返回伪造成功对象。
- 调用外部服务后，Service 内必须判 `if (result == null || !Boolean.TRUE.equals(result.getSuccess()))`，按失败抛 `BusinessException(ErrorCodeEnum.REMOTE_CALL_FAIL, "xxx-service")`。
