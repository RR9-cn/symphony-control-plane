# 建表规范（DDL Conventions）

通用建表规范，产出 DDL 时**必须遵循本规范**，不得发明规范之外的风格。
表名前缀视项目而定，从 DDL 输入材料中自动识别（如 tkhub 项目为 `tk_`，付呗项目为 `tp_`）。

## 命名

- 表名：小写下划线，统一业务前缀（从输入材料中识别，如 `tk_`、`tp_`、`t_` 等），如 `<前缀>_recharge_order`
- 字段名：小写下划线
- 唯一约束：`uk_<表名>_<语义>`
- 普通索引：`idx_<表名>_<字段>`，复合索引字段名串联
- 与 MySQL 关键字冲突的字段用反引号，如 `` `key` ``

## 主键与业务编码

- 物理主键：`id bigint unsigned auto_increment comment '主键' primary key`
- 业务编码：每个业务实体表必有 `<实体>_id varchar(32) not null comment 'xx业务编码'`，并加唯一约束
- **表间关联一律使用业务编码**，字段 comment 标明关联目标，如 `comment '用户业务编码（关联 user 表的 user_id）'`
- **禁止物理外键**（`foreign key`），关联关系只体现在 comment 中

## 公共字段（每张表必须）

```sql
create_time datetime default CURRENT_TIMESTAMP not null comment '创建时间',
update_time datetime default CURRENT_TIMESTAMP not null on update CURRENT_TIMESTAMP comment '修改时间'
```

- **每张表必须有 `create_time` 字段，并为该字段创建普通索引**，索引命名为 `idx_<表名>_create_time`。
- 多租户业务表必带：`tenant_code varchar(50) default '' not null comment '所属租户编码（关联的租户表视项目而定）'`
- 纯日志/追加型表（只插不改）可省略 `update_time`

## 字段风格

- 尽量 `not null` + 默认值（字符串 `default ''`、数值 `default 0`），少用 NULL
- NULL 仅用于语义明确的"未设置/可选"场景：可选时间（如 `expired_time`，NULL=永不过期）、可选关联（NULL=继承上级配置）、大文本/JSON
- 状态/类型字段：`tinyint` 或 `int` + comment 中**完整列出全部枚举值**，如 `comment '状态：0=待支付，1=已支付，2=已取消，3=已退款'`
- 多值字符串枚举：`varchar(16~32)` + comment 列出取值，如 `comment '通知方式：email / webhook'`
- 金额：`decimal(26, 8) default 0.00000000 not null`，comment 注明币种
- 折扣/比例：`decimal(10, 4) default 1.0000 not null`
- "未发生"的时间：`datetime default '1970-01-01 00:00:00' not null`，comment 注明"未 xx 为默认值"；或用 NULL，同一张表内保持一致
- JSON 数据：`text` 或 `json` 类型，comment 注明 JSON 及结构要点
- 冗余快照字段：允许为减少关联查询而冗余（如 `token_name`、`operator_name`），comment 注明"快照冗余"

## 软删除

- 默认风格：`is_del tinyint default 1 not null comment '1 未删除 2 已删除'`
- 当软删除字段需要参与唯一约束（同名可重建）时，用 `deleted_at datetime null comment '软删除时间'` 并将其纳入唯一约束，如 `unique (model_name, deleted_at)`
- 同一张新表只选一种，不混用

## 表属性

- 每张表必有表级 comment，每个字段必有 comment
- `collate = utf8mb4_general_ci`

## 索引（概设阶段的粒度）

技术分析阶段只给出以下必要索引：

1. 主键
2. `create_time` 的普通索引（每张表强制）
3. 全部唯一约束（业务编码 + 业务唯一键，如 `unique (user_id, bill_date)`）
4. 已明确为业务必需的关联字段、筛选字段、排序字段索引

除唯一索引、业务明确需要的索引和本规范强制要求的索引外，其他索引能不加就不加。确有必要新增时，必须在技术分析中明确其查询场景、涉及字段和必要性后再添加；更细的复合索引、覆盖索引优化也遵循该原则。

## 技术分析文档中的 DDL 呈现规范

在技术分析文档中，DDL 不需要给出完整的 `CREATE TABLE` 语句，而是用表格形式呈现：

### 表结构速览表格格式

| 表 | 用途 | 业务主键 | 核心字段 | 关联 |
| --- | --- | --- | --- | --- |
| `表名` | 一句话用途 | `业务主键字段名` | 核心字段列表 | → 关联表 |

### 字段说明规范

- **表名**：必须加中文注释，如 `t_order`（订单表）
- **业务主键**：标注业务主键字段名，如 `order_biz_no`
- **核心字段**：列出关键字段，状态字段加 ★ 标记，如 `order_status★`
- **关联**：用箭头标注表间关系，如 `→ user`

### 状态枚举标注规范

状态字段必须在 comment 中完整列出全部枚举值：

| 状态值 | 中文说明 | 触发条件 |
| --- | --- | --- |
| 0 | 待支付 | 创建订单 |
| 1 | 已支付 | 支付成功 |
| 2 | 已取消 | 用户取消 |

### 设计约定表格格式

<!-- 指引：根据项目实际约定填充，以下为常见示例 -->

| 约定 | 说明 |
| --- | --- |
| 业务主键 | 生成规则，唯一索引 |
| NOT NULL | 所有字段 NOT NULL + 默认值，无 NULL 字段 |
| 表间关联 | 用业务编码，不用自增 id |
