# Data Review Rules

用于 Data Reviewer sub-agent。只在 diff 命中 DDL/DML、`*Mapper.xml`、实体字段、索引、事务、数据迁移、数据回填脚本时读取。

## 审查目标

检查数据层改动是否满足数据规范、兼容性、性能和一致性要求。发现必须映射到 `Standards`、`Spec` 或 `Both`。

## 必查项

| 分类 | 检查点 | HIGH 信号 |
|------|--------|-----------|
| 表结构兼容 | 字段新增/删除/改类型、默认值、NULL 约束 | 新增 NOT NULL 无默认值；删除/改类型未同步读写方；历史数据无法兼容 |
| 索引 | 查询条件、排序、唯一约束、组合索引顺序 | 新增高频查询无索引；索引顺序不匹配；唯一约束与业务唯一性不一致 |
| SQL 正确性 | where 条件、join、分页、聚合、批量更新 | 更新/删除缺少业务唯一条件；join 放大数据；分页不稳定 |
| 软删除过滤 | `is_del`、逻辑删除、唯一性判断、关联查询 | 有 `is_del` 字段的表未显式过滤；JOIN 表漏加 `is_del`；唯一性/存在性校验包含已删除数据 |
| DAL Mapper 表归属 | Mapper/Repository 命名、主表边界、Entity 归属 | 业务流程名 Mapper 承载多个无归属关系表的单表 CRUD；同一表重复定义多套 Entity |
| 事务一致性 | 跨表写、状态更新、补偿、回滚 | 多表写无事务；事务内 RPC/MQ；部分成功无补偿 |
| 并发写 | 锁、CAS、唯一约束、幂等键 | 并发更新无前置状态；锁 key 不含唯一业务标识；幂等键不足 |
| 数据迁移 | 回填、灰度、双写、回滚 | DDL 与代码不兼容；回填不可重入；失败无恢复路径 |
| 敏感数据 | 个人信息、密钥、日志、导出 | SQL/日志暴露敏感字段；无脱敏；权限边界不清 |

## 降级条件

命中高风险区域但存在充分防御时可降级：

- 表结构改动有默认值、灰度兼容、读写双方同步。
- 高并发写有唯一索引、CAS 前置状态、业务唯一锁 key。
- 数据迁移可重入、可回滚、有批量限制和失败记录。
- 事务边界不包含 RPC/MQ/外部 IO，或有明确 outbox/补偿机制。

## 软删除 is_del 显式过滤规则

涉及带 `is_del` 字段的表时，SQL 必须显式表达软删除语义，避免查出、更新或校验到已删除数据。

具体约束：

- 单表查询默认必须包含 `is_del = 0` 或项目约定的未删除条件。
- 多表 `JOIN` 时，主表和被关联表如果都有 `is_del` 字段，都必须分别加上未删除条件。
- `count`、`exists`、唯一性校验、手机号/账号等查重逻辑，也必须排除已删除记录，除非业务明确要求包含删除数据。
- 更新、状态流转、逻辑删除前的定位 SQL，应显式限制 `is_del = 0`，避免修改已删除记录。
- 物理删除、后台审计、历史回收站等确实需要包含已删除数据的 SQL，必须在方法名或注释中说明语义，例如 `selectIncludingDeleted`。
- MyBatis XML、注解 SQL、动态 SQL 片段都要检查，不能只看主查询。

一句话版：

> 带 `is_del` 的表默认只操作未删除数据；查询、JOIN、查重、更新定位都要显式带 `is_del` 条件，除非方法语义明确说明包含已删除数据。

## DAL Mapper 表归属规则

Mapper / Repository 的命名应以“主表或稳定聚合根”为边界，不能用业务流程名承载多个无归属关系表的单表 CRUD。

具体约束：

- `XxxMapper` 默认只负责 `xxx` 主表的单表增删改查。
- 可在 `XxxMapper` 中使用其他表做 `JOIN`，但查询结果必须服务于 `xxx` 主表或明确的 DAL `Result` 投影。
- 禁止在 `ProviderMapper` 这类 Mapper 中新增 `insertAccount`、`selectUserByPhone`、`insertRole` 等其他主表的单表 CRUD。
- 多表组合查询如果服务于特定业务读模型，应命名为 `XxxQueryMapper` / `XxxAggregateMapper`，返回 `dal.result.XxxResult`，不要混入单表写操作。
- 单表写操作必须归属到对应表 Mapper，例如：
  - `chn_account` -> `AccountMapper`
  - `chn_user` -> `UserMapper`
  - `chn_role` -> `RoleMapper`
  - `chn_user_role` -> `UserRoleMapper`
  - `chn_provider_config` -> `ProviderConfigMapper`
- Repository 可以编排多个 Mapper 调用，但不能把多个表的 CRUD 都堆进一个 Mapper 里。
- Entity 也应按表唯一归属，避免同一张表在不同子域重复定义多套 Entity。

一句话版：

> DAL 层 Mapper 以表/聚合根为职责边界；允许为读模型做多表 JOIN，但禁止以业务流程为名聚合多个主表的单表 CRUD，避免 `ProviderMapper` 查询/写入 `account/user/role` 这类职责漂移。

## 输出要求

按统一 Finding Schema 输出。`reference` 写本文件规则分类，例如：

```yaml
- reviewer: Data
  file: backend/.../OrderMapper.xml
  line: 42
  axis: Standards
  severity: HIGH
  type: hard violation
  evidence: "update 缺少状态前置条件，并发提交会重复扣减"
  reference: "references/data-review-rules.md#并发写"
  recommendation: "增加 where status = ? 前置条件或 CAS 更新，并处理更新行数为 0 的分支"
  confidence: high
```

无发现时输出：

```text
NO_FINDINGS: 覆盖 <文件列表/范围>，未发现数据规范、兼容性、性能或一致性风险。
```
