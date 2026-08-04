# 场景：读取 Nacos 配置

本场景用于新增或修改配置类、配置项读取、bootstrap/application yml。

## 初始必读

| 必读材料 | 用途 |
|---|---|
| `references/naming-placement.md` | 确认配置类放 `service.config` 或 `web.config` |
| `references/example-cards.md` | 快速确认配置规则 |

## 条件读取

| 条件 | 再读取 |
|---|---|
| 新增或修改配置类和 yml 写法 | `examples/config-properties.md` |
| 配置被 Service 使用 | `examples/service.md` |
| 配置影响线程池、traceId 或 Web 拦截器 | `references/runtime-guardrails.md` |
| 配置影响多数据源 | `examples/multi-datasource-config.md` |

## 落地顺序

1. 多配置项优先使用 `@ConfigurationProperties`。
2. 单项配置可在配置类中用 `@Value`，但业务类不直接 `@Value`。
3. Service/Manager/Controller 注入配置类，通过 getter 使用配置。
4. yml key 保持命名空间清晰，和配置类字段一致。
5. 必要时给配置字段补默认值或校验，避免空值导致运行时异常。

## 最小完成标准

- Service/Manager/Controller 无直接 `@Value`。
- 配置类放置位置正确。
- yml 与配置字段一致。
- 配置读取不散落在业务逻辑中。
