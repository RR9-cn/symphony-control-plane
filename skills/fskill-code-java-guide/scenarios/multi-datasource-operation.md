# 场景：多数据源读写

本场景用于新增或修改多数据源配置、跨数据源 DAO、按数据源分包的 Entity/Mapper/Repository。

## 初始必读

| 必读材料 | 用途 |
|---|---|
| `references/naming-placement.md` | 确认 dal 按数据源分包 |
| `references/example-cards.md` | 快速确认多数据源、DAO、事务规则 |

## 条件读取

| 条件 | 再读取 |
|---|---|
| 新增或修改数据源配置 | `examples/multi-datasource-config.md` |
| 新增 Entity | `examples/entity.md` |
| 新增 Mapper/XML | `examples/mapper.md` |
| Repository 封装 Mapper | `examples/repository.md` |
| Service 编排多个数据源读写 | `examples/service.md` |
| 涉及事务 | `references/runtime-guardrails.md`、`examples/transaction-template.md` |
| 需要幂等写 | `examples/idempotent-write.md` |
| 跨服务调用或同步外部数据 | `examples/feign-client.md` |

## 落地顺序

1. 按数据源划分 dal 包，不混放 Entity/Mapper/Repository。
2. 每个数据源独立配置 `DataSource`、`SqlSessionFactory`、`TransactionManager`。
3. Mapper XML 明确 namespace、resultMap 和列清单。
4. Service 根据业务选择正确 Repository；Repository 内部封装对应数据源 Mapper，不让 web/client/api 依赖 dal。
5. 跨数据源事务要谨慎；若项目无分布式事务能力，不要假设原子性。

## 最小完成标准

- dal 分包清晰，模块依赖不反向。
- Mapper/Repository 强类型。
- 事务管理器选择明确。
- 不在 web 层直连 dal。
