# web/api 层 DTO（Request / Response）写法

> 第二层原子事例 · 后端 Java 规范
> 适用场景：定义 Controller 入参/出参、对外 Feign 接口入参/出参
> 速查索引：见 [SKILL.md](../SKILL.md) §3 索引目录

---

## 1. 放置位置

- `Request` / `Response` / `VO` 放 `api.{子域}.dto`。
- 对外 HTTP Controller（`web.controller.external.{子域}`）和对内 RPC Controller（`web.controller.internal.{子域}`，`implements api.{子域}.XxxApi`）共用同一套 DTO，避免双份维护。
- Feign 契约接口（`api.{子域}.XxxApi`）的方法入参/出参直接引用本包 DTO，消费方依赖 `api` 模块后强类型对齐。

## 2. 命名规则

| 类型 | 命名 | 示例 |
|------|------|------|
| 入参（创建） | `XxxCreateRequest` | `ActivityCreateRequest` |
| 入参（更新） | `XxxUpdateRequest` | `ActivityUpdateRequest` |
| 入参（查询） | `XxxQueryRequest` | `ActivityQueryRequest` |
| 出参（详情/列表元素） | `XxxResponse` / `XxxVO` | `ActivityResponse`、`ActivityVO` |

## 3. 编写规范

- 使用 `@Data`，统一由 Lombok 生成 getter/setter。
- 字段必须有 JavaDoc `/** xxx */`，每个字段补 JSR-303 校验注解（`@NotNull` / `@NotBlank` / `@Positive` / `@Size` 等）。
- 字段类型：时间用 `LocalDateTime`，金额用 `BigDecimal`，状态/类型用 `Integer`。
- 禁止在 Request/Response 里写业务方法，只承载数据。
- 禁止直接复用 Service 层 `Param`/`Model` 作为对外 DTO，必须独立定义，避免层间协议耦合。

## 4. 完整示例

### 4.1 创建活动 Request（含 JSR-303 校验）

```java
package com.fshows.storemate.merchant.api.activity.dto;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Positive;
import jakarta.validation.constraints.Size;
import lombok.Data;

import java.time.LocalDateTime;

/**
 * 创建活动请求 DTO。
 */
@Data
public class ActivityCreateRequest {

    /** 活动名称 */
    @NotBlank(message = "活动名称不能为空")
    @Size(max = 50, message = "活动名称不能超过 50 字符")
    private String name;

    /** 活动类型 */
    @NotNull(message = "活动类型不能为空")
    private Integer type;

    /** 活动库存 */
    @NotNull(message = "库存不能为空")
    @Positive(message = "库存必须大于 0")
    private Integer stock;

    /** 开始时间 */
    @NotNull(message = "开始时间不能为空")
    private LocalDateTime startTime;

    /** 结束时间 */
    @NotNull(message = "结束时间不能为空")
    private LocalDateTime endTime;
}
```

### 4.2 活动响应 Response

```java
package com.fshows.storemate.merchant.api.activity.dto;

import lombok.Data;

import java.time.LocalDateTime;

/**
 * 活动响应 DTO。
 */
@Data
public class ActivityResponse {

    /** 活动 ID */
    private Long id;

    /** 活动名称 */
    private String name;

    /** 活动状态（值含义见 ActivityStatusEnum） */
    private Integer status;

    /** 剩余库存 */
    private Integer stock;

    /** 创建时间 */
    private LocalDateTime createTime;
}
```

### 4.3 分页查询 Request

```java
package com.fshows.storemate.merchant.api.activity.dto;

import jakarta.validation.constraints.Max;
import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotNull;
import lombok.Data;

/**
 * 活动分页查询请求 DTO。
 */
@Data
public class ActivityQueryRequest {

    /** 当前页，从 1 开始 */
    @NotNull(message = "页码不能为空")
    @Min(value = 1, message = "页码最小为 1")
    private Integer pageNum = 1;

    /** 每页大小 */
    @NotNull(message = "每页大小不能为空")
    @Min(value = 1, message = "每页大小最小为 1")
    @Max(value = 100, message = "每页大小最大为 100")
    private Integer pageSize = 20;

    /** 活动状态（可选，传 null 查全部） */
    private Integer status;

    /** 活动名称模糊查询（可选） */
    private String nameLike;
}
```

## 5. 最佳实践提示

- JSR-303 注解的 `message` 必须用中文，便于前端直接展示。
- 跨字段校验（如 `startTime < endTime`）**不要**用 JSR-303，放到 Service / Manager 用 `BusinessException` 校验。
- `Response` 中状态/类型字段统一用 `Integer` 装原值，前端通过枚举解释，**不要**在 Response 里直接塞 `desc` 字符串，避免协议与枚举脱节。
- 查询 Request 字段都给默认值（如 `pageNum = 1`），避免前端漏传导致 NPE。
