# 对象转换（Assembler + FsBeanUtil / MapStruct）写法

> 第二层原子事例 · 后端 Java 规范
> 适用场景：Request/Response、Param/Model、Entity/Criteria/Result 之间的对象转换
> 速查索引：见 [SKILL.md](../SKILL.md) §3 索引目录

---

## 1. 放置位置与职责

- `FsBeanUtil` 放 `common.util`，跨模块共享，只做静态同名属性拷贝工具。
- `XxxAssembler` 放 `service.manager.{子域}`，是 Service 层可见的唯一转换门面。
- MapStruct 接口放 `service.manager.{子域}.converter`，命名 `XxxConverter` / `XxxStructConverter`，只允许被 `XxxAssembler` 调用，禁止命名为 `XxxMapper`。
- MapStruct 依赖只加在需要 `XxxConverter` 的业务模块（通常 service 模块），版本由根工程统一管理；不要为了简单同名拷贝引入 Converter。
- Controller 边界可直接用 `FsBeanUtil` 做简单协议转换：`Request -> Param`、`Model -> Response`。
- Service 禁止直接散落 `FsBeanUtil` / MapStruct 调用；统一调用 `XxxAssembler`。

## 2. 工具选择优先级

| 场景 | 推荐方式 | 说明 |
|------|---------|------|
| Controller 简单协议转换 | `FsBeanUtil.map()` / `mapList()` | 字段同名、类型一致、无业务语义 |
| Service 层业务/DAL 转换 | `XxxAssembler` | Service 只看 Assembler，不关心底层工具 |
| Assembler 内同名字段拷贝 | `FsBeanUtil` | 简单、低风险、差异字段少 |
| Assembler 内字段名不一致、嵌套对象、枚举转换、集合稳定映射 | MapStruct `XxxConverter` | 编译期暴露字段变更，适合长期维护的复杂映射 |
| 少量差异字段 | Assembler 内单独 setter | 差异字段要有注释说明来源或语义 |

## 3. FsBeanUtil 方法说明

| 方法 | 说明 |
|------|------|
| `FsBeanUtil.map(source, TargetClass)` | 源对象属性拷贝到目标类型新实例 |
| `FsBeanUtil.mapList(sourceList, TargetClass)` | 集合中每个元素属性拷贝到目标类型 |
| `FsBeanUtil.copyProperties(source, target, ignoreProperties)` | 拷贝到已有目标对象，可忽略指定属性 |
| `FsBeanUtil.map(sourceMap, TargetClass)` | Map 中每个 value 属性拷贝到目标类型 |

## 4. 使用规范

- 转换入口收口：Controller 做协议 DTO 简单转换；Service 只调用 `XxxAssembler`；Assembler 内部决定用 `FsBeanUtil` 还是 MapStruct。
- DTO 分层隔离：禁止 Request/Response 直接传入 Service；禁止 Param/Model 直接传入 Mapper；禁止 Entity 直接返回 Controller。
- `FsBeanUtil` 只适合同名同类型字段拷贝，差异字段由 Assembler 单独补充。
- MapStruct 接口禁止叫 `XxxMapper`，避免与 MyBatis Mapper 混淆。
- 禁止在 Service / Manager 里手写大量 setter 做转换；少量业务补字段可在 Assembler 内完成。

## 5. 完整示例

### 5.1 Controller 简单协议转换

```java
// Controller 内 Request → Param
ActivityCreateParam param = FsBeanUtil.map(request, ActivityCreateParam.class);

// Controller 内 Model → Response
return FsBeanUtil.map(model, ActivityResponse.class);

// Controller 内列表转换 Model → Response
List<ActivityResponse> respList = FsBeanUtil.mapList(modelPage.getList(), ActivityResponse.class);
```

> Controller 只处理 web/api DTO 与 Service DTO 的边界转换，不做 Entity / DAL Result 转换。

### 5.2 Assembler 内使用 FsBeanUtil + 补充差异字段

```java
// Assembler 内 Param → Entity，差异字段单独 setter
public ActivityEntity toEntity(ActivityCreateParam param, Integer initStatus) {
    ActivityEntity entity = FsBeanUtil.map(param, ActivityEntity.class);
    entity.setStatus(initStatus);  // Param 里没有 status，单独补
    return entity;
}

// Assembler 内 Entity → Model
public ActivityModel toModel(ActivityEntity entity) {
    return FsBeanUtil.map(entity, ActivityModel.class);
}
```

### 5.3 Assembler 内使用 MapStruct Converter

```java
package com.fshows.storemate.merchant.service.manager.activity.converter;

import com.fshows.storemate.merchant.dal.primary.activity.entity.ActivityEntity;
import com.fshows.storemate.merchant.service.service.activity.model.ActivityModel;
import org.mapstruct.Mapper;
import org.mapstruct.Mapping;
import org.mapstruct.ReportingPolicy;

/**
 * 活动对象 MapStruct 转换器。
 * 仅供 ActivityAssembler 注入使用，禁止命名为 ActivityMapper。
 */
@Mapper(componentModel = "spring", unmappedTargetPolicy = ReportingPolicy.ERROR)
public interface ActivityConverter {

    /**
     * 实体转业务模型。
     *
     * @param entity 活动实体
     * @return 活动模型
     */
    @Mapping(source = "status", target = "statusCode")
    ActivityModel toModel(ActivityEntity entity);
}
```

```java
package com.fshows.storemate.merchant.service.manager.activity;

import com.fshows.storemate.merchant.dal.primary.activity.entity.ActivityEntity;
import com.fshows.storemate.merchant.service.manager.activity.converter.ActivityConverter;
import com.fshows.storemate.merchant.service.service.activity.model.ActivityModel;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Component;

/**
 * 活动对象转换门面。
 */
@Component
public class ActivityAssembler {

    /** 活动 MapStruct 转换器 */
    @Autowired
    private ActivityConverter activityConverter;

    /**
     * 实体转业务模型。
     *
     * @param entity 活动实体
     * @return 活动模型
     */
    public ActivityModel toModel(ActivityEntity entity) {
        return activityConverter.toModel(entity);
    }
}
```

### 5.4 禁止：Service 中手写大量 setter

```java
// 禁止：手写大量 setter
ActivityEntity entity = new ActivityEntity();
entity.setName(request.getName());
entity.setType(request.getType());
entity.setStock(request.getStock());
entity.setStartTime(request.getStartTime());
entity.setEndTime(request.getEndTime());
// ... 大量重复 setter
```

### 5.5 拷贝到已有对象 + 忽略字段

```java
// Assembler 内更新场景：把 Param 字段拷贝到已查出的 Entity，忽略 id/createTime
public void copyUpdateParam(ActivityUpdateParam param, ActivityEntity existEntity) {
    FsBeanUtil.copyProperties(param, existEntity, "id", "createTime");
}
```

## 6. 最佳实践提示

- `map()` 返回新实例，原对象不变；`copyProperties()` 拷贝到已有对象，常用于更新场景（避免新建对象丢主键）。
- 拷贝是基于**同名属性**的，所以 Request/Param/Model/Response/Entity 字段名必须严格对齐，差异字段（如落库前要补的 `status`、查询时要剔除的 `updateTime`）单独 setter。
- 更新场景务必用 `copyProperties` + `ignoreProperties` 忽略 `id`/`createTime` 等不可变字段，避免被 Param 里的 null 覆盖。
- `mapList` 内部循环调用 `map`，大列表（> 1000）注意性能，必要时分批或换 Stream + 手写转换。
- MapStruct 适合字段名不一致、嵌套对象、枚举转换、集合转换等稳定复杂映射；简单同名字段不要为了 MapStruct 过度建接口。
- MapStruct Converter 是 Assembler 的内部实现细节，Service / Controller 禁止直接注入 Converter。
- 转换逻辑统一放 `XxxAssembler`，禁止散落在 Service 各处，便于复用和维护。
