# Service 层 DTO（Param / Model）写法

> 第二层原子事例 · 后端 Java 规范
> 适用场景：Service 接口入参/出参，承上启下隔离 web 协议与 dal 实体
> 速查索引：见 [SKILL.md](../SKILL.md) §3 索引目录

---

## 1. 放置位置

| 类型 | 位置 | 命名 |
|------|------|------|
| Service 入参 | `service.service.{子域}.param` | `XxxParam` / `XxxCreateParam` / `XxxUpdateParam` / `XxxQueryParam` |
| Service 出参 | `service.service.{子域}.model` | `XxxModel` |

## 2. 编写规范

- 使用 `@Data`。
- 字段必须有 JavaDoc `/** xxx */`。
- Param/Model 可与 web 层 `Request`/`Response` 字段名保持一致，便于 Controller 边界用 `FsBeanUtil.map()` 做协议转换；Service 不直接依赖 web DTO。
- 禁止在 Param/Model 里写业务方法，只承载数据。
- 禁止把 Entity 直接当 Service 出参，必须经过 `Assembler` 转换为 Model，屏蔽 dal 字段细节。

## 3. 完整示例

### 3.1 创建活动入参 Param

```java
package com.fshows.storemate.merchant.service.service.activity.param;

import lombok.Data;

import java.time.LocalDateTime;

/**
 * 创建活动入参。
 */
@Data
public class ActivityCreateParam {

    /** 活动名称 */
    private String name;

    /** 活动类型 */
    private Integer type;

    /** 活动库存 */
    private Integer stock;

    /** 开始时间 */
    private LocalDateTime startTime;

    /** 结束时间 */
    private LocalDateTime endTime;
}
```

### 3.2 活动业务模型 Model

```java
package com.fshows.storemate.merchant.service.service.activity.model;

import lombok.Data;

import java.time.LocalDateTime;

/**
 * 活动业务模型。
 */
@Data
public class ActivityModel {

    /** 活动 ID */
    private Long id;

    /** 活动名称 */
    private String name;

    /** 活动状态 */
    private Integer status;

    /** 剩余库存 */
    private Integer stock;

    /** 创建时间 */
    private LocalDateTime createTime;
}
```

### 3.3 分页查询入参 Param

```java
package com.fshows.storemate.merchant.service.service.activity.param;

import lombok.Data;

/**
 * 活动分页查询入参。
 */
@Data
public class ActivityQueryParam {

    /** 当前页，从 1 开始 */
    private Integer pageNum = 1;

    /** 每页大小 */
    private Integer pageSize = 20;

    /** 活动状态（可选） */
    private Integer status;

    /** 活动名称模糊查询（可选） */
    private String nameLike;
}
```

## 4. 最佳实践提示

- Param/Model 与 Request/Response 字段名一致仅用于 Controller 边界直接拷贝；Param/Entity、Entity/Model 等业务/DAL 转换统一由 Assembler 处理，差异字段（如落库前要补的 `status`）在各自边界单独 setter。
- Service 入参**不要**带 JSR-303 校验注解，Controller 层校验过的数据进入 Service 后，业务规则校验由 Manager/Service 用 `BusinessException` 完成。
- 查询 Param 默认值与 Request 默认值保持一致，避免 Controller `Request → Param` 拷贝时把 null 覆盖掉默认值。
- Model 字段可少于 Entity（屏蔽 `updateTime` 等内部字段），但**禁止**多于 Entity（避免伪造字段）。
