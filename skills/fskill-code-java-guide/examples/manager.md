# Manager + Assembler 写法

> 第二层原子事例 · 后端 Java 规范
> 适用场景：可复用纯领域逻辑、对象转换逻辑
> 速查索引：见 [SKILL.md](../SKILL.md) §3 索引目录

---

## 1. 放置位置

- Manager 和 Assembler 都放 `service.manager.{子域}`，同包便于查找。

## 2. 职责边界

### 2.1 Manager

| 应做 | 禁止 |
|------|------|
| 纯领域逻辑（校验、计算、状态判断） | 加 `@Transactional` |
| 可被多个 Service 复用 | 直接调 Mapper / Client / Repository |
| 仅依赖其他 Manager 或参数传入 | 依赖 api 模块 Request/Response DTO |
| 抛 `BusinessException` | 写转换逻辑（放 Assembler） |

### 2.2 Assembler

| 应做 | 禁止 |
|------|------|
| 作为 Service 层可见的转换门面 | 写业务校验/计算逻辑 |
| `Param ↔ Entity`、`Param → Criteria` 转换 | 让 Service 直接调用 `FsBeanUtil` / MapStruct |
| `Entity ↔ Model` 转换 | 调 Mapper / Client |
| 简单转换用 `FsBeanUtil`，复杂转换委托 MapStruct `XxxConverter` | 手写大量 setter |
| 补充差异字段（名称不同、需额外赋值） | 依赖 api 模块 DTO |

## 3. 编写规范

- 都用 `@Component`，依赖统一 `@Autowired` 字段注入。
- Manager 方法接收**基础类型**或**领域对象**（如 `LocalDateTime`、`Integer`、`Param`），不直接依赖 api 模块 Request/Response。
- Assembler 接收 Service 层 `Param`/`Model` 和 dal 层 `Entity`/`Criteria`/`Result`，不依赖 api 模块 DTO。
- MapStruct Converter 放 `service.manager.{子域}.converter`，命名 `XxxConverter` / `XxxStructConverter`，只允许 Assembler 注入调用，禁止命名为 `XxxMapper`。
- 类、方法必须有 JavaDoc。
- 一个子域可有多个 Manager（按职责拆，如 `ActivityCreateManager` / `ActivityStatusManager`），但 Assembler 通常一个子域一个。

## 4. 完整示例

### 4.1 ActivityCreateManager（校验类）

```java
package com.fshows.storemate.merchant.service.manager.activity;

import com.fshows.storemate.merchant.common.exception.BusinessException;
import com.fshows.storemate.merchant.common.exception.ErrorCodeEnum;
import org.springframework.stereotype.Component;

import java.time.LocalDateTime;

/**
 * 活动创建领域逻辑。
 */
@Component
public class ActivityCreateManager {

    /**
     * 创建活动参数校验。
     *
     * @param startTime 开始时间
     * @param endTime   结束时间
     */
    public void validate(LocalDateTime startTime, LocalDateTime endTime) {
        LocalDateTime now = LocalDateTime.now();
        if (startTime.isBefore(now)) {
            throw new BusinessException(ErrorCodeEnum.PARAM_INVALID, "开始时间不能早于当前时间");
        }
        if (!endTime.isAfter(startTime)) {
            throw new BusinessException(ErrorCodeEnum.PARAM_INVALID, "结束时间必须晚于开始时间");
        }
    }
}
```

### 4.2 ActivityStatusManager（状态计算类）

```java
package com.fshows.storemate.merchant.service.manager.activity;

import com.fshows.storemate.merchant.service.enums.activity.ActivityStatusEnum;
import org.springframework.stereotype.Component;

/**
 * 活动状态领域逻辑。
 */
@Component
public class ActivityStatusManager {

    /**
     * 初始化活动状态。
     *
     * @return 初始状态码
     */
    public Integer initStatus() {
        // 简单场景：默认草稿；可按业务规则扩展
        return ActivityStatusEnum.DRAFT.getCode();
    }

    /**
     * 判断是否允许变更到目标状态。
     *
     * @param current 当前状态
     * @param target  目标状态
     * @return 是否允许
     */
    public boolean canTransit(Integer current, Integer target) {
        // 简单状态机示例
        ActivityStatusEnum cur = ActivityStatusEnum.getByCode(current);
        ActivityStatusEnum tgt = ActivityStatusEnum.getByCode(target);
        if (cur == null || tgt == null) {
            return false;
        }
        // 草稿 → 进行中 / 已下架
        if (cur == ActivityStatusEnum.DRAFT) {
            return tgt == ActivityStatusEnum.RUNNING || tgt == ActivityStatusEnum.DISABLED;
        }
        // 进行中 → 已结束 / 已下架
        if (cur == ActivityStatusEnum.RUNNING) {
            return tgt == ActivityStatusEnum.FINISHED || tgt == ActivityStatusEnum.DISABLED;
        }
        return false;
    }
}
```

### 4.3 ActivityAssembler（转换类）

```java
package com.fshows.storemate.merchant.service.manager.activity;

import com.fshows.storemate.merchant.common.util.FsBeanUtil;
import com.fshows.storemate.merchant.dal.primary.activity.criteria.ActivityQueryCriteria;
import com.fshows.storemate.merchant.dal.primary.activity.entity.ActivityEntity;
import com.fshows.storemate.merchant.service.service.activity.model.ActivityModel;
import com.fshows.storemate.merchant.service.service.activity.param.ActivityCreateParam;
import com.fshows.storemate.merchant.service.service.activity.param.ActivityQueryParam;
import org.springframework.stereotype.Component;

import java.util.List;

/**
 * 活动对象转换门面。
 * 负责 Param ↔ Entity、Entity ↔ Model 转换，简单同名字段使用 FsBeanUtil，
 * 复杂稳定映射可委托 MapStruct Converter。
 */
@Component
public class ActivityAssembler {

    /**
     * 创建入参转实体。
     *
     * @param param      创建入参
     * @param initStatus 初始状态码（Param 里没有，单独传入）
     * @return 活动实体
     */
    public ActivityEntity toEntity(ActivityCreateParam param, Integer initStatus) {
        ActivityEntity entity = FsBeanUtil.map(param, ActivityEntity.class);
        entity.setStatus(initStatus);
        return entity;
    }

    /**
     * Service 查询入参转 DAL 查询条件。
     *
     * @param param 查询入参
     * @return DAL 查询条件
     */
    public ActivityQueryCriteria toCriteria(ActivityQueryParam param) {
        return FsBeanUtil.map(param, ActivityQueryCriteria.class);
    }

    /**
     * 实体转业务模型。
     *
     * @param entity 活动实体
     * @return 活动业务模型
     */
    public ActivityModel toModel(ActivityEntity entity) {
        return FsBeanUtil.map(entity, ActivityModel.class);
    }

    /**
     * 实体列表转业务模型列表。
     *
     * @param entityList 实体列表
     * @return 业务模型列表
     */
    public List<ActivityModel> toModelList(List<ActivityEntity> entityList) {
        return FsBeanUtil.mapList(entityList, ActivityModel.class);
    }
}
```

### 4.4 ActivityConverter（MapStruct，可选）

> 仅当字段名不一致、嵌套对象、枚举转换或集合稳定映射较多时新增 MapStruct Converter。简单同名字段继续用 `FsBeanUtil`，不要过度抽象。

```java
package com.fshows.storemate.merchant.service.manager.activity.converter;

import com.fshows.storemate.merchant.dal.primary.activity.entity.ActivityEntity;
import com.fshows.storemate.merchant.service.service.activity.model.ActivityModel;
import org.mapstruct.Mapper;
import org.mapstruct.Mapping;
import org.mapstruct.ReportingPolicy;

/**
 * 活动 MapStruct 转换器。
 * 仅供 ActivityAssembler 注入使用，禁止命名为 ActivityMapper。
 */
@Mapper(componentModel = "spring", unmappedTargetPolicy = ReportingPolicy.ERROR)
public interface ActivityConverter {

    /**
     * 实体转业务模型。
     *
     * @param entity 活动实体
     * @return 活动业务模型
     */
    @Mapping(source = "status", target = "statusCode")
    ActivityModel toModel(ActivityEntity entity);
}
```

> 如果新增了 `ActivityConverter`，`ActivityAssembler#toModel` 内注入并调用 converter；Service 仍然只调用 `ActivityAssembler`，不直接依赖 Converter。

## 5. 最佳实践提示

- Manager 的核心价值是**可复用**，写之前问自己"这段逻辑会被另一个 Service 调用吗？"，如果会→放 Manager；如果只在本 Service 一个方法内用一次→放 Service 私有方法即可，避免过度抽象。
- Assembler 是业务层唯一转换门面：Service 只调用 Assembler；`FsBeanUtil` / MapStruct Converter 是 Assembler 内部实现细节。
- Assembler 内简单同名属性用 `FsBeanUtil.map()` / `FsBeanUtil.mapList()`，差异字段单独 setter；复杂稳定映射再引入 MapStruct Converter。
- MapStruct 接口禁止叫 `XxxMapper`，避免和 MyBatis Mapper 混淆。
- Assembler 方法签名不要传 `Request`/`Response`，只接受 Service 层 `Param`/`Model` 和 dal 层 `Entity`/`Criteria`/`Result`，避免与 web 协议耦合。
- 状态机/复杂校验逻辑放 Manager 而非 Service，便于单测覆盖（Manager 无 Spring 依赖时可直接 `new` 出来测）。
