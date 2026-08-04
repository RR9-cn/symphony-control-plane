# Repository 写法

> 第二层原子事例 · 后端 Java 规范
> 适用场景：封装 Mapper 调用，对 Service 层屏蔽 MyBatis 细节
> 速查索引：见 [SKILL.md](../SKILL.md) §3 索引目录

---

## 1. 放置位置

- Repository 放 `dal.{数据源}.{子域}.repository`，与 Mapper、Entity 同子域包。

## 2. 职责边界

| 应做 | 禁止 |
|------|------|
| 封装 Mapper 调用 | 调 Service / Manager / Client |
| 方法命名语义化（`findById` / `findByStatus`） | 暴露 `selectXxx` / `updateXxx` 等 MyBatis 风格命名 |
| 组合多个 Mapper 调用 | 写业务校验/计算逻辑 |
| 分页参数转换、排序条件封装 | 在 Service 拼 SQL 条件 |
| 接收 DAL Criteria、返回 DAL Result | 复用 Service Param / Model |
| 反向依赖上层 | 反向依赖 Service / Manager |

## 3. 编写规范

- 使用 `@Repository`，依赖 Mapper 用 `@Autowired` 字段注入。
- 方法命名语义化：`findById`、`findByStatus`、`updateStock`、`save`、`deleteById`，不暴露 MyBatis 风格命名。
- 复杂查询统一走 Repository 封装，禁止 Service 直接拼 SQL 条件。
- 类、方法必须有 JavaDoc。

### 3.1 入参规则

| 场景 | 入参类型 | 示例 | 说明 |
|------|---------|------|------|
| 保存/更新 | `Entity` | `save(ActivityEntity entity)` | 单参数 |
| 主键查询/删除 | `Long` | `findById(Long id)` | 单参数 |
| 条件查询/更新 | 基本类型/包装类 | `findByStatus(Integer status)` / `updateStatus(Long id, Integer status)` | 多个参数无需 `@Param`（Repository 是普通类） |
| 复杂/分页查询 | `XxxCriteria` | `findPage(ActivityQueryCriteria criteria)` | 定义在 `dal.{数据源}.{子域}.criteria` |
| FOR UPDATE 查询 | `String` / `Long` | `findByBizNoForUpdate(String bizNo)` | 按业务唯一键或主键锁定 |

**禁止**：`Map<String, Object>` 作为入参、`Object` 作为入参。

### 3.2 返参规则

| 场景 | 返参类型 | 示例 | 说明 |
|------|---------|------|------|
| 保存（插入） | `void` | `void save(ActivityEntity entity)` | 主键通过 `useGeneratedKeys` 回填到 Entity，调用方从 `entity.getId()` 获取 |
| 更新/删除 | `int` | `int updateStatus(Long id, Integer status)` | 受影响行数，调用方可判断是否成功 |
| 单条查询 | `Entity` | `ActivityEntity findById(Long id)` | 不存在返回 `null` |
| 列表查询 | `List<Entity>` | `List<ActivityEntity> findByStatus(Integer status)` | 无数据返回空列表，不返回 `null` |
| 自定义 SQL 查询 | `List<XxxResult>` | `List<ActivitySummaryResult> findSummaryByCriteria(ActivityQueryCriteria criteria)` | join / 聚合 / 投影等非表结构结果 |
| 分页查询 | `PageResult<Entity>` / `PageResult<XxxResult>` | `PageResult<ActivityEntity> findPage(ActivityQueryCriteria criteria)` | Repository 内完成 count + list 组合 |

**禁止**：`Map` 返回、`Object` 返回、`boolean` 返回。

## 4. 完整示例

### 4.1 基础 Repository

```java
package com.fshows.storemate.merchant.dal.primary.activity.repository;

import com.fshows.storemate.merchant.dal.primary.activity.criteria.ActivityQueryCriteria;
import com.fshows.storemate.merchant.dal.primary.activity.entity.ActivityEntity;
import com.fshows.storemate.merchant.dal.primary.activity.mapper.ActivityMapper;
import com.fshows.storemate.merchant.dal.primary.activity.result.ActivitySummaryResult;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Repository;

import java.util.List;

/**
 * 活动仓储。
 * 封装 Mapper 调用，对上层屏蔽 MyBatis 细节。
 */
@Repository
public class ActivityRepository {

    /** 活动 Mapper */
    @Autowired
    private ActivityMapper activityMapper;

    /**
     * 保存活动（插入）。
     *
     * @param entity 活动实体
     */
    public void save(ActivityEntity entity) {
        activityMapper.insert(entity);
    }

    /**
     * 根据主键查询活动。
     *
     * @param id 活动 ID
     * @return 活动实体，不存在返回 null
     */
    public ActivityEntity findById(Long id) {
        return activityMapper.selectById(id);
    }

    /**
     * 按状态查询活动列表。
     *
     * @param status 状态
     * @return 活动列表
     */
    public List<ActivityEntity> findByStatus(Integer status) {
        return activityMapper.selectByStatus(status);
    }

    /**
     * 根据查询条件查询活动摘要列表。
     *
     * @param criteria 查询条件
     * @return 活动摘要列表
     */
    public List<ActivitySummaryResult> findSummaryByCriteria(ActivityQueryCriteria criteria) {
        return activityMapper.selectSummaryByCriteria(criteria);
    }

    /**
     * 更新活动状态。
     *
     * @param id     活动 ID
     * @param status 目标状态
     * @return 影响行数
     */
    public int updateStatus(Long id, Integer status) {
        return activityMapper.updateStatus(id, status);
    }

    /**
     * 根据主键更新活动（动态字段）。
     *
     * @param entity 活动实体（不为 null 的字段参与更新）
     * @return 影响行数
     */
    public int updateById(ActivityEntity entity) {
        return activityMapper.updateById(entity);
    }
}
```

### 4.2 带分页的 Repository（基于 DAL Criteria）

```java
package com.fshows.storemate.merchant.dal.primary.activity.repository;

import com.fshows.storemate.merchant.common.response.PageResult;
import com.fshows.storemate.merchant.dal.primary.activity.criteria.ActivityQueryCriteria;
import com.fshows.storemate.merchant.dal.primary.activity.entity.ActivityEntity;
import com.fshows.storemate.merchant.dal.primary.activity.mapper.ActivityMapper;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Repository;

import java.util.List;

/**
 * 活动仓储（含分页）。
 */
@Repository
public class ActivityRepository {

    @Autowired
    private ActivityMapper activityMapper;

    /**
     * 分页查询活动。
     *
     * @param criteria 查询条件
     * @return 分页结果
     */
    public PageResult<ActivityEntity> findPage(ActivityQueryCriteria criteria) {
        long total = activityMapper.countByCondition(criteria);
        if (total == 0) {
            return PageResult.of(java.util.Collections.emptyList(), 0L, criteria.getPageNum(), criteria.getPageSize());
        }
        int offset = (criteria.getPageNum() - 1) * criteria.getPageSize();
        List<ActivityEntity> list = activityMapper.selectByCondition(criteria, offset, criteria.getPageSize());
        return PageResult.of(list, total, criteria.getPageNum(), criteria.getPageSize());
    }
}
```

## 5. 最佳实践提示

- Service **只依赖 Repository**，不直接依赖 Mapper，便于将来替换 ORM 或加缓存拦截。
- Repository 方法名禁止用 `selectXxx`/`updateXxx` 等 MyBatis 风格，统一用领域语义命名（`findById`/`findByStatus`/`updateStock`）。
- 复杂查询条件放 DAL 层 `criteria` 包，命名 `XxxCriteria`；禁止直接复用 Service 层 `XxxParam`。
- 自定义 SQL 返回结构放 DAL 层 `result` 包，命名 `XxxResult`；禁止往 Entity 塞非表字段。
- 分页查询在 Repository 内完成 count + list 的组合，返回 `PageResult<Entity>` 或 `PageResult<XxxResult>`，Service 再做 `Entity/Result → Model` 转换。
- 多数据源场景下，Repository 必须与同数据源的 Mapper 在同一包下（如 `dal.primary.activity`、`dal.report`），禁止跨数据源引用。
