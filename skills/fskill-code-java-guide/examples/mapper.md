# Mapper + XML 写法

> 第二层原子事例 · 后端 Java 规范
> 适用场景：MyBatis Mapper 接口 + XML SQL 文件
> 速查索引：见 [SKILL.md](../SKILL.md) §3 索引目录

---

## 1. 放置位置

| 类型 | 位置 |
|------|------|
| Mapper 接口 | `dal.{数据源}.{子域}.mapper` |
| XML 文件 | `dal/src/main/resources/mapper/{数据源}/{子域}/XxxMapper.xml` |

## 2. 编写规范

- Mapper 接口用 `@Mapper` 注解（或统一配置包扫描）。
- 方法名语义化：`insert`、`updateById`、`selectById`、`selectByCondition`、`countByCondition`。
- 参数超过一个时必须用 `@Param` 注解命名。
- 动态查询统一用 XML 的 `<where>`、`<if>`、`<choose>`、`<foreach>`，禁止在注解里写复杂 SQL。
- XML 使用 `resultMap` 显式映射字段，避免下划线自动映射隐患。
- 查询禁止 `SELECT *`，必须显式列名（通过 `<include refid="Base_Column_List"/>`）。
- `INSERT` 使用 `<trim prefix="(" suffix=")" suffixOverrides=",">` 兼容动态字段。
- `UPDATE` 使用动态 `<set>` 标签，避免空字段覆盖。
- 表名用 `t_` 前缀（如 `t_activity`），字段用下划线命名（如 `create_time`）。

### 2.1 入参规则

| 场景 | 入参类型 | 示例 | 说明 |
|------|---------|------|------|
| 插入/更新 | `Entity` | `insert(ActivityEntity entity)` | 单参数，无需 `@Param` |
| 主键查询/删除 | `Long` | `selectById(Long id)` | 单参数，无需 `@Param` |
| 多条件查询/更新 | 多个基本类型 + `@Param` | `updateStatus(@Param("id") Long id, @Param("status") Integer status)` | 每个参数必须加 `@Param` |
| 复杂条件查询 | `XxxCriteria` | `countByCondition(ActivityQueryCriteria criteria)` | 定义在 `dal.{数据源}.{子域}.criteria` |
| 分页查询 | `XxxCriteria` + `int offset` + `int limit` | `selectByCondition(@Param("criteria") ActivityQueryCriteria criteria, @Param("offset") int offset, @Param("limit") int limit)` | Repository 计算偏移量后传入 |
| 批量操作 | `List<Entity>` / `List<Long>` | `batchInsert(@Param("list") List<ActivityEntity> list)` | 批量 IN 查询单次不超过 1000 |

**禁止**：`Map<String, Object>` 作为入参、`Object` 作为入参、`JSONObject` 作为入参。

### 2.2 返参规则

| 场景 | 返参类型 | 示例 | 说明 |
|------|---------|------|------|
| 插入 | `int` | `int insert(ActivityEntity entity)` | 受影响行数，主键通过 `useGeneratedKeys` 回填到 Entity |
| 更新/删除 | `int` | `int updateStatus(...)` | 受影响行数，调用方可判断是否成功 |
| 单条查询 | `Entity` | `ActivityEntity selectById(Long id)` | 不存在返回 `null` |
| 列表查询 | `List<Entity>` | `List<ActivityEntity> selectByStatus(...)` | 无数据返回空列表，不返回 `null` |
| 自定义 SQL 查询 | `XxxResult` / `List<XxxResult>` | `List<ActivitySummaryResult> selectSummaryByCriteria(...)` | join / 聚合 / 投影等非表结构结果 |
| 统计数量 | `long` | `long countByCondition(...)` | 禁止用 `int` 防溢出 |

**禁止**：`void` 返回（无法判断操作是否成功）、`boolean` 返回、`Map` 返回、`Object` 返回。

## 3. 完整示例

### 3.1 Mapper 接口

```java
package com.fshows.storemate.merchant.dal.primary.activity.mapper;

import com.fshows.storemate.merchant.dal.primary.activity.criteria.ActivityQueryCriteria;
import com.fshows.storemate.merchant.dal.primary.activity.entity.ActivityEntity;
import com.fshows.storemate.merchant.dal.primary.activity.result.ActivitySummaryResult;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;

import java.util.List;

/**
 * 活动 Mapper 接口。
 */
@Mapper
public interface ActivityMapper {

    /**
     * 插入活动。
     *
     * @param entity 活动实体
     * @return 影响行数
     */
    int insert(ActivityEntity entity);

    /**
     * 根据主键查询。
     *
     * @param id 活动 ID
     * @return 活动实体
     */
    ActivityEntity selectById(Long id);

    /**
     * 按状态查询活动列表。
     *
     * @param status 状态
     * @return 活动列表
     */
    List<ActivityEntity> selectByStatus(@Param("status") Integer status);

    /**
     * 更新活动状态。
     *
     * @param id     活动 ID
     * @param status 目标状态
     * @return 影响行数
     */
    int updateStatus(@Param("id") Long id, @Param("status") Integer status);

    /**
     * 根据主键动态更新（仅更新非 null 字段）。
     *
     * @param entity 活动实体
     * @return 影响行数
     */
    int updateById(ActivityEntity entity);

    /**
     * 按条件统计数量。
     *
     * @param criteria 查询条件
     * @return 总数
     */
    long countByCondition(ActivityQueryCriteria criteria);

    /**
     * 按条件分页查询。
     *
     * @param criteria 查询条件
     * @param offset   偏移量
     * @param limit    每页大小
     * @return 活动列表
     */
    List<ActivityEntity> selectByCondition(@Param("criteria") ActivityQueryCriteria criteria,
                                           @Param("offset") int offset,
                                           @Param("limit") int limit);

    /**
     * 根据查询条件查询活动摘要列表。
     *
     * @param criteria 查询条件
     * @return 活动摘要列表
     */
    List<ActivitySummaryResult> selectSummaryByCriteria(ActivityQueryCriteria criteria);
}
```

### 3.2 XML 文件

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE mapper
        PUBLIC "-//mybatis.org//DTD Mapper 3.0//EN"
        "http://mybatis.org/dtd/mybatis-3-mapper.dtd">
<mapper namespace="com.fshows.storemate.merchant.dal.primary.activity.mapper.ActivityMapper">

    <resultMap id="BaseResultMap" type="com.fshows.storemate.merchant.dal.primary.activity.entity.ActivityEntity">
        <id column="id" property="id"/>
        <result column="name" property="name"/>
        <result column="type" property="type"/>
        <result column="stock" property="stock"/>
        <result column="status" property="status"/>
        <result column="start_time" property="startTime"/>
        <result column="end_time" property="endTime"/>
        <result column="create_time" property="createTime"/>
        <result column="update_time" property="updateTime"/>
    </resultMap>

    <resultMap id="SummaryResultMap" type="com.fshows.storemate.merchant.dal.primary.activity.result.ActivitySummaryResult">
        <id column="id" property="id"/>
        <result column="name" property="name"/>
        <result column="status" property="status"/>
        <result column="stock" property="stock"/>
        <result column="start_time" property="startTime"/>
        <result column="end_time" property="endTime"/>
        <result column="duration_minutes" property="durationMinutes"/>
    </resultMap>

    <sql id="Base_Column_List">
        id, name, type, stock, status, start_time, end_time, create_time, update_time
    </sql>

    <insert id="insert" useGeneratedKeys="true" keyProperty="id">
        INSERT INTO t_activity
        <trim prefix="(" suffix=")" suffixOverrides=",">
            <if test="name != null">name,</if>
            <if test="type != null">type,</if>
            <if test="stock != null">stock,</if>
            <if test="status != null">status,</if>
            <if test="startTime != null">start_time,</if>
            <if test="endTime != null">end_time,</if>
        </trim>
        <trim prefix="VALUES (" suffix=")" suffixOverrides=",">
            <if test="name != null">#{name},</if>
            <if test="type != null">#{type},</if>
            <if test="stock != null">#{stock},</if>
            <if test="status != null">#{status},</if>
            <if test="startTime != null">#{startTime},</if>
            <if test="endTime != null">#{endTime},</if>
        </trim>
    </insert>

    <select id="selectById" resultMap="BaseResultMap">
        SELECT <include refid="Base_Column_List"/>
        FROM t_activity
        WHERE id = #{id}
    </select>

    <select id="selectByStatus" resultMap="BaseResultMap">
        SELECT <include refid="Base_Column_List"/>
        FROM t_activity
        WHERE status = #{status}
        ORDER BY create_time DESC
    </select>

    <update id="updateStatus">
        UPDATE t_activity
        SET status = #{status}, update_time = now()
        WHERE id = #{id}
    </update>

    <update id="updateById">
        UPDATE t_activity
        <set>
            <if test="name != null">name = #{name},</if>
            <if test="type != null">type = #{type},</if>
            <if test="stock != null">stock = #{stock},</if>
            <if test="status != null">status = #{status},</if>
            <if test="startTime != null">start_time = #{startTime},</if>
            <if test="endTime != null">end_time = #{endTime},</if>
            update_time = now()
        </set>
        WHERE id = #{id}
    </update>

    <select id="countByCondition" resultType="long">
        SELECT COUNT(1)
        FROM t_activity
        <where>
            <if test="status != null">AND status = #{status}</if>
            <if test="nameLike != null and nameLike != ''">AND name LIKE CONCAT('%', #{nameLike}, '%')</if>
        </where>
    </select>

    <select id="selectByCondition" resultMap="BaseResultMap">
        SELECT <include refid="Base_Column_List"/>
        FROM t_activity
        <where>
            <if test="criteria.status != null">AND status = #{criteria.status}</if>
            <if test="criteria.nameLike != null and criteria.nameLike != ''">AND name LIKE CONCAT('%', #{criteria.nameLike}, '%')</if>
        </where>
        ORDER BY create_time DESC
        LIMIT #{offset}, #{limit}
    </select>

    <select id="selectSummaryByCriteria" resultMap="SummaryResultMap">
        SELECT
            id,
            name,
            status,
            stock,
            start_time,
            end_time,
            TIMESTAMPDIFF(MINUTE, start_time, end_time) AS duration_minutes
        FROM t_activity
        <where>
            <if test="status != null">AND status = #{status}</if>
            <if test="type != null">AND type = #{type}</if>
            <if test="nameLike != null and nameLike != ''">AND name LIKE CONCAT('%', #{nameLike}, '%')</if>
        </where>
        ORDER BY create_time DESC
    </select>

</mapper>
```

## 4. 最佳实践提示

- **禁止** `SELECT *`，统一用 `<include refid="Base_Column_List"/>`，避免表加字段后 XML 隐式返回脏数据。
- `INSERT` 用 `useGeneratedKeys="true" keyProperty="id"`，插入后回填主键到 Entity，Service 才能拿到 `entity.getId()`。
- `UPDATE` 必须用动态 `<set>` 标签，避免 null 字段覆盖数据库已有值。
- 分页 SQL 直接写 `LIMIT #{offset}, #{limit}`，由 Repository 计算偏移量，禁止用 MyBatis PageHelper 插件（与多数据源事务有兼容性问题）。
- 复杂 SQL 入参放 DAL 层 `criteria` 包，命名 `XxxCriteria`；禁止直接引用 Service 层 `XxxParam`。
- 自定义 SQL 返回结构放 DAL 层 `result` 包，命名 `XxxResult`；禁止用 `Map` 或往 Entity 塞非表字段。
- 新建表或改 SQL 时，**必须**检查 WHERE 条件是否命中索引，禁止对索引字段做函数计算。
- 大批量 `IN` 查询单次不超过 1000，超过需分批；批量 update 单批不超过 500 条。
