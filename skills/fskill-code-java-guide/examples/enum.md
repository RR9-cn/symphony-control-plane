# 枚举（Enum）写法

> 第二层原子事例 · 后端 Java 规范
> 适用场景：定义状态码、类型码、是/否、通用开关等离散值集合
> 速查索引：见 [SKILL.md](../SKILL.md) §3 索引目录

---

## 1. 放置位置

| 类型 | 位置 | 适用场景 |
|------|------|---------|
| 通用枚举（多模块共享） | `common.enums` | 跨业务复用，如 `CommonStatusEnum`、`YesOrNoEnum` |
| service 模块内通用枚举 | `service.common.enums` | 仅 service 模块跨子域共享，不对外暴露 |
| 业务子域枚举 | `service.enums.{子域}` | 子域专属，如 `service.enums.activity.ActivityStatusEnum` |
| 对外枚举（API 暴露） | `api.enums` | 仅供外部调用方使用 |

## 2. 编写规范

- 字段名严格为 `code` 和 `desc`，类型不限（`code` 可为 `Integer`/`String`，`desc` 一般为 `String`）。
- 字段使用 `@Getter` 由 Lombok 生成，构造器手写以保证字段顺序可读。
- 必须提供 `getByCode(code)` 静态方法，便于反查。
- 禁止在枚举里写复杂业务逻辑，只放字段、构造、查询方法。
- lint 校验（L003）只检查 `code`/`desc` 字段声明，不校验类型。

## 3. 完整示例

### 3.1 通用枚举（`code` 为 Integer）

```java
package com.fshows.storemate.merchant.common.enums;

import lombok.Getter;

/**
 * 通用状态枚举。
 */
@Getter
public enum CommonStatusEnum {

    ENABLED(1, "启用"),
    DISABLED(0, "禁用");

    private final Integer code;
    private final String desc;

    CommonStatusEnum(Integer code, String desc) {
        this.code = code;
        this.desc = desc;
    }

    public static CommonStatusEnum getByCode(Integer code) {
        for (CommonStatusEnum e : values()) {
            if (e.code.equals(code)) {
                return e;
            }
        }
        return null;
    }
}
```

### 3.2 业务子域枚举（`code` 为 String）

```java
package com.fshows.storemate.merchant.service.enums.activity;

import lombok.Getter;

/**
 * 活动类型枚举（code 为 String）。
 */
@Getter
public enum ActivityTypeEnum {

    FULL_REDUCTION("FULL_REDUCTION", "满减"),
    DISCOUNT("DISCOUNT", "折扣"),
    GIFT("GIFT", "赠品");

    private final String code;
    private final String desc;

    ActivityTypeEnum(String code, String desc) {
        this.code = code;
        this.desc = desc;
    }

    public static ActivityTypeEnum getByCode(String code) {
        for (ActivityTypeEnum e : values()) {
            if (e.code.equals(code)) {
                return e;
            }
        }
        return null;
    }
}
```

### 3.3 业务子域状态枚举（与 Entity.status 对应）

```java
package com.fshows.storemate.merchant.service.enums.activity;

import lombok.Getter;

/**
 * 活动状态枚举，对应 t_activity.status 字段。
 */
@Getter
public enum ActivityStatusEnum {

    DRAFT(0, "草稿"),
    RUNNING(1, "进行中"),
    FINISHED(2, "已结束"),
    DISABLED(3, "已下架");

    private final Integer code;
    private final String desc;

    ActivityStatusEnum(Integer code, String desc) {
        this.code = code;
        this.desc = desc;
    }

    public static ActivityStatusEnum getByCode(Integer code) {
        for (ActivityStatusEnum e : values()) {
            if (e.code.equals(code)) {
                return e;
            }
        }
        return null;
    }
}
```

## 4. 最佳实践提示

- 落库的状态/类型字段统一用 `Integer`（对应数据库 `tinyint`），用枚举解释语义，禁止用 `Boolean` 表示多状态。
- Service 内校验状态时：`if (!ActivityStatusEnum.RUNNING.getCode().equals(entity.getStatus())) { throw new BusinessException(...); }`。
- 跨子域复用优先放 `common.enums`；只在 service 内复用放 `service.common.enums`；只在单子域用放 `service.enums.{子域}`。
- 对外 API 暴露的枚举放 `api.enums`，禁止把 service 层枚举直接通过 Feign 接口暴露给外部。
