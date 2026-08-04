---
name: fskill-tools-db
description: "数据库查询工具使用指南。当 Agent 需要查询当前项目的数据库表结构、搜索表，或者修改当前项目的测试环境数据库的数据时，执行 SQL 时使用，指导如何使用fshows cli工具进行查询"
metadata:
  author: tkhub
  version: "1.0"
---

# fskill-tools-db — 数据库查询工具使用指南

## 定位

本 skill 是 **数据库查询工具的使用说明书**，指导 Agent 如何正确调用 `fshows --json db` 命令组查询数据库。

> Agent 想查数据库 → 触发本 skill → 引导 Agent 使用 `fshows --json db` 命令组

**它不做**：

- DDL 设计（归 `fskill-analysis-ddl`）
- 技术分析（归 `fskill-analysis-tech`）
- 任务拆分（归 `fskill-analysis-task-split`）
- 编码实现

**它只做**：指导 Agent 正确使用 `fshows db` 命令组查询数据库表结构和数据。

## 触发场景

- "查一下数据库里有哪些表"
- "看看 t_user 表的结构"
- "查一下测试环境的数据"
- "搜索包含 user 的表"
- "执行一条 SQL 查询"
- "这个表有哪些字段"
- 任何 Agent 需要读取数据库表结构或数据的场景

## 前置条件

1. **项目已初始化 fshows**：存在 `.fshows/config.yaml`（通过 `fskill-init-project` 完成）
2. **项目已配置数据库连接**：存在 `.fshows/db-config.yaml` 且已填写有效连接信息

### 配置未完成时的处理

当 Agent 触发本 skill 需要执行 `fshows --json db` 命令时，若发现 `.fshows/db-config.yaml` 不存在或配置为空（`database`、`user`、`host` 等关键字段为空字符串），**必须先引导用户完成配置，再继续执行查询**：

1. **检查配置文件状态**：
   - 文件不存在 → 提示用户：当前项目尚未配置数据库连接，需要先创建 `.fshows/db-config.yaml`
   - 文件存在但关键字段为空 → 提示用户：配置文件存在但连接信息不完整，需要补充数据库地址、端口、库名、用户名等信息

2. **引导用户补充配置**：
   - 询问用户测试环境数据库的连接信息（类型、host、port、库名、用户名）
   - 密码通过环境变量引用（如 `password: ${DB_PASSWORD}`），不要求用户直接告知密码
   - 告知用户设置对应环境变量后重新打开终端
   - 配置模板见下方"配置文件"章节

3. **配置完成后继续执行**：用户确认配置完成后，重新执行 `fshows --json db` 命令

> **不要在配置未完成时静默跳过数据库查询**。若用户暂时无法提供配置，明确告知用户：当前无法查询数据库，后续准备好配置后可随时触发本 skill。

## 配置文件

文件路径：`.fshows/db-config.yaml`（项目根目录下）

```yaml
# 默认数据源（db 命令不指定 --name 时使用）
default: primary

# 数据源列表
datasources:
  primary:
    type: mysql              # mysql | postgresql
    host: 127.0.0.1
    port: 3306
    database: storemate
    user: root
    password: ${DB_PASSWORD}  # 支持环境变量引用，避免明文
    charset: utf8mb4          # 可选，MySQL 默认 utf8mb4

  # 可配置多个数据源
  # secondary:
  #   type: postgresql
  #   host: 127.0.0.1
  #   port: 5432
  #   database: analytics
  #   user: postgres
  #   password: ${PG_PASSWORD}
```

**安全提示**：

- 密码字段支持 `${ENV_VAR}` 语法引用环境变量，推荐使用
- 若密码为明文，CLI 会输出警告
- 建议将 `.fshows/db-config.yaml` 加入 `.gitignore`

## 命令速查

### 1. 查询/搜索表列表

```powershell
# 列出所有表
fshows --json db tables

# 按表名或表备注模糊搜索
fshows --json db tables --search "user"

# 使用指定数据源
fshows --json db tables --name secondary
```

**返回结构**：

```json
{
  "success": true,
  "data": {
    "database": "storemate",
    "tables": [
      { "tableName": "t_user", "tableComment": "用户表" },
      { "tableName": "t_user_device", "tableComment": "用户设备表" }
    ]
  }
}
```

**使用场景**：

- 盘点现有表，了解数据库中有哪些表
- 按关键词搜索相关表（如搜索 "user" 找到所有用户相关表）
- 搜索时同时匹配表名和表备注

### 2. 查询表 DDL 结构

```powershell
# 查看表的完整 DDL（含建表语句、列信息、索引信息）
fshows --json db schema t_user

# 使用指定数据源
fshows --json db schema t_user --name secondary
```

**返回结构**：

```json
{
  "success": true,
  "data": {
    "database": "storemate",
    "table": "t_user",
    "ddl": "CREATE TABLE `t_user` (...)",
    "columns": [
      { "name": "id", "type": "bigint(20)", "nullable": false, "key": "PRI", "default": null, "comment": "主键ID" }
    ],
    "indexes": [
      { "name": "uk_user_id", "columns": ["user_id"], "unique": true }
    ]
  }
}
```

**使用场景**：

- 查看表的完整建表语句
- 了解表的字段类型、是否可空、默认值、注释
- 查看表的索引信息（主键、唯一索引、普通索引）
- DDL 设计时复用已有表结构

### 3. 执行 SQL 查询

支持 **三种 SQL 输入方式**，按优先级依次尝试：

#### 方式 1：`--file` 从文件读取（推荐长 SQL）

```powershell
fshows --json db query --file ./query.sql
```

#### 方式 2：stdin 管道传入（推荐 Agent 使用）

```powershell
# PowerShell here-string，无需转义
@'
SELECT * FROM t_user
WHERE user_name LIKE '%测试%'
ORDER BY id DESC
LIMIT 10
'@ | fshows --json db query

# 也可以从文件管道
Get-Content ./query.sql -Raw | fshows --json db query
```

#### 方式 3：命令行参数直接传入（仅适合短 SQL）

```powershell
fshows --json db query "SELECT * FROM t_user LIMIT 10"
```

**输入优先级**：`--file` > stdin（有管道输入时） > 命令行参数

**返回结构**：

```json
{
  "success": true,
  "data": {
    "database": "storemate",
    "columns": ["id", "user_id", "user_name"],
    "rows": [
      { "id": 1, "user_id": "U001", "user_name": "张三" }
    ],
    "rowCount": 1
  }
}
```

**使用场景**：

- 查询表数据样本，了解数据分布
- 验证表结构设计是否满足业务需求
- 查询关联数据，辅助技术分析

**安全限制**：

- 允许 `SELECT`、`WITH`（CTE）、`UPDATE`、`DELETE` 语句
- 不允许执行 DDL 语句（`CREATE`/`DROP`/`ALTER`/`TRUNCATE`/`INSERT`）

## 使用建议

### SQL 输入方式选择

| 场景 | 推荐方式 | 原因 |
|------|----------|------|
| 短 SQL（单行简单查询） | 命令行参数 | 简单直接 |
| 长 SQL（多行、含特殊字符） | `--file` 或 stdin | 避免 shell 转义问题 |
| Agent 自动生成复杂 SQL | `--file` | 先写入临时 .sql 文件再执行，最可靠 |
| 需要管道传递 | stdin | 灵活，支持 here-string |

### 多数据源切换

- 默认使用 `db-config.yaml` 中 `default` 指定的数据源
- 通过 `--name <datasource>` 切换到其他数据源
- 例如：`fshows --json db tables --name secondary`

### 与其他 skill 的协作

| 场景 | 推荐流程 |
|------|----------|
| DDL 设计时盘点现有表 | 先 `db tables` 列出所有表，再 `db schema <table>` 查看关键表结构 |
| 技术分析时查存量表 | `db schema <table>` 获取表结构，`db query` 验证数据 |
| 验证表字段是否存在 | `db schema <table>` 查看列信息 |
| 搜索相关表 | `db tables --search "keyword"` 模糊搜索表名和备注 |

## Guardrails

- 本 skill 只负责指导 Agent 使用 `fshows db` 命令组，不做分析或设计。
- `db query` 仅支持 SELECT/WITH 查询，不允许执行 DDL/DML。
- 数据库密码应使用环境变量引用，避免明文写入配置文件。
- 查询结果可能包含敏感数据，Agent 应注意不要在对话中暴露敏感信息。
- 连接失败时检查 `.fshows/db-config.yaml` 配置是否正确、数据库是否可访问。
