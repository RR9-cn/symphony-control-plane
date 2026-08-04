# 场景：分页查询接口

本场景用于新增或修改分页查询。分页查询通常不需要事务；不要为了查询默认加 `@Transactional(readOnly = true)`。

## 初始必读

| 必读材料 | 用途 |
|---|---|
| `references/naming-placement.md` | 确认 Controller、Service、Repository、Mapper、DTO 放置 |
| `references/example-cards.md` | 快速确认涉及组件 |

## 条件读取

| 条件 | 再读取 |
|---|---|
| 新增分页 Request/Response | `examples/dto-web-api.md` |
| 新增查询 Param/Model | `examples/dto-service.md` |
| 新增或修改 Controller 分页入口 | `examples/controller.md`、`examples/result.md` |
| 新增或修改 Service 查询编排 | `examples/service.md` |
| 新增或修改 SQL | `examples/mapper.md` |
| Repository 封装 Mapper | `examples/repository.md` |
| 新增查询条件对象或投影结果 | `examples/mapper.md` 中 Criteria/Result 相关部分 |
| 需要 Entity 字段对照 | `examples/entity.md` |
| 需要对象转换 | `examples/bean-util.md` |
| 查询需要一致性快照 | `references/runtime-guardrails.md`、`examples/transaction-template.md` |

## 落地顺序

1. 定义分页 Request，使用 JSR-303 做格式校验。
2. 转换为 Service 查询 Param；跨字段和业务规则放 Service。
3. Service 调 Repository 查询总数和列表；Repository 内部封装 Mapper。
4. Mapper XML 显式列字段，禁止 `SELECT *`。
5. 将 Entity/Result 转为 Response/Model，返回 `PageResult.of(...)` 或项目既有分页封装。

## 最小完成标准

- 查询默认无事务。
- Controller 不手写 `Result.success(...)`。
- Mapper 入参/返参强类型，无 `Map`/`Object`。
- XML 使用 `resultMap` 和明确列清单。
