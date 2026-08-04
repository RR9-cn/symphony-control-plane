---
name: fskill-test-explore
description: tkhub AI harness 测试探索与测试方案生成流程。用于用户要求分析某个功能怎么测、选择 Node.js 黑盒测试/Java 单测/Java 集成测试/手工 smoke、梳理触发入口、依赖服务、数据准备、Mock/Stub、断言点、覆盖等级、不可替代断言，并按本 skill 自带的 template/test-plan-template.md 产出可校验 Markdown 测试方案时。
---

# FSkill Test Explore

使用本 skill 时，只做测试探索与方案产出。不要直接启动环境、编写测试代码、运行测试或修复业务代码；这些动作交给 `fskill-test-verify`。

最终回复使用中文。

API 自动化测试默认使用 Node.js；不要生成 Python/pytest 测试方案，除非用户明确要求保留旧 Python 测试。

## 工作流

1. 读取测试方案模板。
   - 打开本 skill 目录下的 `template/test-plan-template.md`。
   - 读取路径按当前 `SKILL.md` 所在目录解析，不要从仓库根目录的 `harness/` 下查找模板。
   - 后续输出必须遵循该模板的标题结构和字段。

2. 探索功能边界。
   - 阅读用户指定功能相关代码、配置、DDL、seed、已有测试和文档。
   - 使用 `rg` 搜索功能关键词、Controller、Service、DAO、MQ listener、Job、配置项和前端入口。
   - 明确本次要验证和不验证的范围。

3. 识别触发入口。
   - 判断是否存在 HTTP API、MQ 消息、定时任务、内部 Service 调用、前端操作或数据库状态触发。
   - 说明推荐触发入口，以及不推荐入口的原因。

4. 识别依赖服务和环境。
   - 判断需要启动哪些应用和模块，优先参考 `fshows --json workflow instructions test` 返回的 `context.references.backendRoot`。
   - 判断需要哪些 Docker 依赖：MySQL、Redis、Nacos、RocketMQ、mock-upstream。
   - 如果当前 harness 脚本尚不支持所需应用，必须在方案里说明缺口。

5. 设计数据准备和 Mock。
   - 写清楚需要的用户、Token/API Key、业务配置、数据库数据、Mock 数据。
   - 先获取迭代目录：优先使用 `fshows --json workflow status` 返回的 `data.current.featureRoot`；失败时询问用户一次。
   - 新增测试脚本、fixture、临时 SQL、补充说明文档和报告等一次性测试资产，统一放到 `<featureRoot>/test/` 下，不要放到仓库根目录的 `harness/` 下。
   - 区分基线 SQL 和临时 SQL：
     - `harness/tests/fixtures/db/schema.sql` 是项目 harness 测试的基线表结构；只有当前功能引入的新表、字段、索引、枚举字典表等会被后续其他功能测试复用时，才建议扩展。
     - `harness/tests/fixtures/db/init_data.sql` 是项目 harness 测试的基线初始化数据；只有稳定、通用、跨功能可复用的数据，如租户、应用、用户、权限、基础配置、字典、通用商品/门店等，才建议扩展。
     - `<featureRoot>/test/fixtures/db/tmp/` 存放特定测试用例的临时 SQL；一次性场景数据、边界数据、异常数据、幂等/并发专用数据、只服务某个 `TC-xxx` 的数据都写到这里，避免污染基线。
   - 临时 SQL 文件名必须按 `TC-{需求名称}-xxx.sql` 命名，例如 `TC-order-create-001.sql`、`TC-pay-callback-idempotent-003.sql`；`{需求名称}` 使用当前测试内容或场景的短横线标识，`xxx` 使用对应 TC 序号或用途后缀。
   - 在测试方案中必须说明每个 SQL 文件为什么属于基线或临时数据；不确定是否可复用时，默认放入 `<featureRoot>/test/fixtures/db/tmp/`。
   - 如需新增 HTTP fixture，放到 `<featureRoot>/test/fixtures/http/`；如需新增 Node.js API 测试脚本，放到 `<featureRoot>/test/api/`，文件名使用 `*.test.mjs`；如需新增辅助脚本或说明文档，分别放到 `<featureRoot>/test/scripts/`、`<featureRoot>/test/docs/`。
   - 外部服务必须优先使用 mock/stub，不要依赖真实第三方服务。

6. 选择测试类型。
   - 在方案中明确主测试类型：`node_api`、`java_unit`、`java_integration` 或 `manual_smoke`。
   - 选择依据：
     - HTTP API、响应、DB/Redis 副作用优先 `node_api`。
     - 纯 Service/Sender/模板/工具类逻辑优先 `java_unit`。
     - Spring Bean、DAO、事务、MQ listener 优先 `java_integration`。
     - 尚无自动化入口或只需临时确认时才使用 `manual_smoke`。
   - `node_api` 测试优先使用 Node.js 内置 `node:test`、`node:assert/strict` 和全局 `fetch`，不要默认引入需要额外安装的测试框架。
   - 如果用户期望 Node.js API 测试但功能没有可用 HTTP 入口，必须说明原因并给出补齐路径。

7. 设定覆盖等级和不可替代断言。
   - 在 `## 覆盖等级` 中明确目标覆盖等级和最低可接受覆盖等级。
   - 覆盖等级从低到高为：`unit`、`service`、`integration`、`e2e`。
   - 默认不要把最低可接受覆盖等级设置得低于真实风险需要的等级。
   - 降级是否需用户批准必须为 **是**。
   - 在 `## 不可替代断言` 中写出不能被更弱测试替代的断言，例如真实 Webhook 接收、DB 状态、MQ 消费、Redis key、副作用次数、鉴权头、幂等次数等。
   - 在 `## 降级路径` 中只描述阻塞时可选的降级路径；降级路径不能作为默认执行路径。

8. 产出测试方案文件。
   - 文件名：按本次测试覆盖的功能内容命名，格式为 `<测试内容简述>-test-plan.md`（如 `订单创建-test-plan.md`、`商户积分扣减-test-plan.md`）。一个迭代可能拆分给多个人开发，每个人会写各自的测试方案，因此文件名需具有辨识度。
   - 保存位置（优先级从高到低）：
     1. 用户指定的输出路径
     2. 执行 `fshows --json workflow status` 取 `data.current.featureRoot`，写入 `<featureRoot>/test/<测试内容简述>-test-plan.md`
     3. 若上述命令失败（无 feature 上下文），询问用户一次保存路径；用户不指定则存当前工作目录
   - 内容必须从 `template/test-plan-template.md` 复制结构后填写，不要输出自由格式散文。

9. 校验测试方案格式。
   - 执行：
     `node <skill_dir>/scripts/validate-test-plan.mjs <测试方案文件路径>`
   - `<skill_dir>` 是当前 `SKILL.md` 所在目录，例如 `.agents/skills/fskill-test-explore`。
   - 校验失败时，修复方案格式并重新校验。

## 输出要求

最终回复只简洁说明：

- 测试方案文件路径。
- 推荐测试类型。
- 目标覆盖等级和最低可接受覆盖等级。
- 不可替代断言摘要。
- 推荐触发入口。
- 需要启动的应用和 Docker 依赖。
- 格式校验结果。

不要在最终回复里粘贴完整测试方案，除非用户明确要求。
