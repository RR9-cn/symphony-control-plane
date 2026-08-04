# Entity 写法

> 第二层原子事例 · 后端 Java 规范
> 适用场景：数据库表对应实体类
> 速查索引：见 [SKILL.md](../SKILL.md) §3 索引目录

---

## 1. 放置位置

- Entity 放 `dal.{数据源}.{子域}.entity`，与 Mapper、Repository 同子域包。

## 2. 编写规范

- 使用 `@Data` 注解，统一由 Lombok 生成 getter/setter。
- 主键使用 `Long` 类型自增，字段名 `id`。
- 时间字段使用 `LocalDateTime`，禁止用 `Date`。
- 字段与数据库列名一一对应（下划线→驼峰），不映射冗余字段。
- SQL join / 聚合 / 投影返回的非表结构字段，放 `dal.{数据源}.{子域}.result.XxxResult`，禁止塞进 Entity。
- 布尔/状态字段使用 `Integer`（对应数据库 `tinyint`），用枚举解释语义，禁止用 `Boolean`。
- 金额字段使用 `BigDecimal`，与数据库 `decimal` 类型一致。
- 字段必须有 JavaDoc `/** xxx */`。
- 禁止在 Entity 里写业务方法，只承载数据。

## 3. 完整示例

### 3.1 基础 Entity

```java
package com.fshows.storemate.merchant.dal.primary.activity.entity;

import lombok.Data;

import java.time.LocalDateTime;

/**
 * 活动数据库实体。
 * 对应表 t_activity。
 */
@Data
public class ActivityEntity {

    /** 主键 ID */
    private Long id;

    /** 活动名称 */
    private String name;

    /** 活动类型（值含义见 ActivityTypeEnum） */
    private Integer type;

    /** 剩余库存 */
    private Integer stock;

    /** 状态（值含义见 ActivityStatusEnum） */
    private Integer status;

    /** 开始时间 */
    private LocalDateTime startTime;

    /** 结束时间 */
    private LocalDateTime endTime;

    /** 创建时间 */
    private LocalDateTime createTime;

    /** 更新时间 */
    private LocalDateTime updateTime;
}
```

### 3.2 带金额字段的 Entity

```java
package com.fshows.storemate.merchant.dal.primary.order.entity;

import lombok.Data;

import java.math.BigDecimal;
import java.time.LocalDateTime;

/**
 * 订单数据库实体。
 * 对应表 t_order。
 */
@Data
public class OrderEntity {

    /** 主键 ID */
    private Long id;

    /** 订单号 */
    private String orderNo;

    /** 用户 ID */
    private Long userId;

    /** 订单金额（元） */
    private BigDecimal amount;

    /** 订单状态（值含义见 OrderStatusEnum） */
    private Integer status;

    /** 创建时间 */
    private LocalDateTime createTime;

    /** 更新时间 */
    private LocalDateTime updateTime;
}
```

## 4. 最佳实践提示

- `createTime`/`updateTime` 字段统一在 Entity 里声明，由 XML 的 `now()` 或 MyBatis 拦截器自动填充，禁止在 Service 里手动 `setCreateTime(LocalDateTime.now())`。
- 状态/类型字段统一用 `Integer`，落库值用枚举的 `code`，禁止直接落枚举 `name()` 字符串。
- 多数据源场景下，Entity 必须放在对应数据源的子包下（如 `dal.primary.activity.entity`、`dal.report.xxx.entity`），禁止跨数据源引用 Entity。
- Entity 字段不要带业务计算字段（如 `剩余可参与次数 = 总次数 - 已参与次数`），这类字段属于 Model 层，由 Assembler 计算。
- 如果某个字段是 SQL 层直接计算出来的查询结果（如 `COUNT`、`SUM`、`duration_minutes`、join 表名称），应放在 DAL `result` JavaBean 中，由 Service 再转换为 Model。
