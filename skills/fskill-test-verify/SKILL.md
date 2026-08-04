---
name: fskill-test-verify
description: tkhub AI harness 测试方案执行流程。用于用户已经通过 fskill-test-explore 产出测试方案，或要求基于测试方案启动 harness Docker 环境、确认 application-harness.yml 配置、编写测试、执行 Node.js/Java/Smoke 测试、修复回归、运行工程校验并清理环境；覆盖 Windows PowerShell + WSL + Docker Desktop 混合环境，防止应用、测试和 Gradle 误入 WSL。
---

# FSkill Test Verify

使用本 skill 执行测试方案。测试探索和方案设计由 `fskill-test-explore` 完成；本 skill 不应在缺少测试方案时直接编写测试。

执行过程和最终回复均使用中文。

## 背景信息

- 当前仓库常见执行环境是 Windows + PowerShell。
- API 自动化测试默认使用 Node.js，不使用 Python/pytest；如果本机缺少 Node.js，作为环境阻塞汇报。
- 如果 `references.backendRoot` 下是 Gradle 项目，在 Windows 环境执行构建命令时优先使用 `gradlew.bat`。
- 只有确认当前项目是 Maven 项目时，才使用 `mvn.cmd`。
- 当环境是 Windows + WSL + Docker Desktop 时，必须先完整读取 [Windows + WSL Harness 执行规范](references/windows-wsl-harness.md)，并在启动前固定本次执行矩阵。

## 前置输入

测试方案路径（优先级从高到低）：

1. 用户指定的测试方案路径
2. 执行 `fshows --json workflow status` 取 `data.current.featureRoot`，在 `<featureRoot>/test/` 目录下查找 `*-test-plan.md` 测试方案文件
3. 若上述命令失败（无 feature 上下文），询问用户一次测试方案路径

如果没有测试方案：

- 不要直接启动环境。
- 不要直接编写测试。
- 提醒先使用 `fskill-test-explore` 生成测试方案。

## 硬性规则

- 不得为了更容易通过而降低测试类型、减少服务依赖、删除副作用断言或改弱断言。
- 不得执行低于 `最低可接受等级` 的测试后宣称验证成功。
- `## 不可替代断言` 中的每一条都必须被测试覆盖；否则不能宣称验证成功。
- 只有用户明确同意，才能使用 `## 降级路径` 中的降级方案。
- 如果无法按方案执行，必须停止并汇报阻塞原因，不要自行替换为更简单路径。
- 如果连续多次因为环境问题导致测试失败，必须立即停止并汇报环境阻塞原因，等待用户确认是否继续测试。
- 如果确实需要调整方案，先更新测试方案并重新执行 `validate-test-plan.mjs`，再继续执行。
- 测试实现必须基于 harness 环境和 `application-harness.yml`，不能默认使用 `dev`、远程测试环境或本机残留配置。
- Windows 混合环境中，WSL 只允许执行 Docker 依赖、数据库初始化和清理 Shell；Java 应用、Node.js 测试、Gradle/Maven 构建和工程校验必须在 Windows 本机执行。
- 不得因 WSL 缺 JDK、Gradle、Node.js、无法访问 Windows localhost 或挂载盘 I/O 异常，而把原定 Windows 应用/测试切换到 WSL。
- 工程校验前必须停止 harness 应用并确认端口释放，避免 `bootRun` 锁住 build/JAR 后再执行 `clean`。

## 工作流

1. 读取并校验测试方案。
   - 执行 `git status --short`。
   - 读取测试方案文件。
    - 执行：
     `node .agents/skills/fskill-test-explore/scripts/validate-test-plan.mjs <test-plan-path>`
   - 校验失败时，先修复测试方案格式并重新校验。
   - 方案校验通过后，立即勾选 `方案校验通过`。

2. 根据测试方案确认执行策略。
   - 执行 `fshows --json workflow status`，提取 `data.current.featureRoot` 作为迭代目录；若失败，优先从测试方案路径推断 `<featureRoot>/test/` 的父目录，仍无法确定时询问用户一次。
   - 从 `## 测试类型选择` 读取主测试类型。
   - 从 `## 覆盖等级` 读取 `目标覆盖等级`、`最低可接受等级`、`降级是否需用户批准`。
   - 从 `## 不可替代断言` 读取不可替代断言。
   - 从 `## 依赖服务` 读取需要启动的应用和 Docker 依赖。
   - 从 `## 测试用例` 读取需要实现的 `TC-xxx`。
   - 从 `## 执行进度` 读取执行进度。
   - 从 `## 断言设计` 读取 HTTP、DB、Redis、MQ、Mock、日志/报告断言。
   - 判定当前是纯 Windows、Windows + WSL，还是纯 Linux；记录应用、Shell、测试、构建各自的固定执行环境。
   - Windows + WSL 场景按 `references/windows-wsl-harness.md` 执行，不得在后续失败时临时更换执行面。

3. 预检、启动并确认 harness 环境。
   - 修改环境前检查 Docker engine、目标端口、应用遗留进程、构建工具和测试运行时；预检失败时不要进入重置或启动。
   - 先读取准备调用的 harness 脚本，确认它只执行预期职责；不要盲目运行同时包含依赖、DB、应用和测试的一键脚本。
   - 如果方案需要干净环境，执行与当前操作系统匹配的依赖重置脚本。
   - 纯 Linux 环境可以使用仓库提供的完整 `ai-env-up.sh`。
   - 纯 Windows 环境使用 PowerShell和实际 compose 文件完成依赖/DB操作，应用、Node.js测试和构建均使用 Windows本机工具；不要为运行 `.sh` 临时引入 WSL。
   - Windows + WSL 环境：
     - 在 WSL 中只执行纯 Docker 依赖启动和 DB 初始化脚本；不要执行会继续调用 `gradlew`、`mvn` 或启动应用的 `ai-env-up.sh`。
     - 在 Windows PowerShell 中使用 `gradlew.bat` 或 `mvn.cmd` 启动目标应用，设置 `SPRING_PROFILES_ACTIVE=harness`，并使用隐藏窗口、独立日志和 PID 文件。
     - 应用健康检查和 profile 确认从 Windows PowerShell执行；Node.js 测试也使用 Windows `node.exe`。
   - 确保本地 Docker 依赖、harness DB seed、mock-upstream 和目标应用 harness profile 可用。
   - 确认目标应用使用 `harness` profile，而不是 `dev` profile。
   - 测试相关配置必须指向 `backend/<app>/src/main/resources/application-harness.yml`。
   - 如果方案需要 `tkhub-console`、`tkhub-admin` 或 `tkhub-batch`，但当前 harness 缺少对应启动脚本，先说明缺口；只有在方案允许时，才补启动脚本或使用与当前操作系统匹配的明确构建命令，并指定 `spring.profiles.active=harness`。
   - 不要对非本地数据库执行破坏性初始化。
   - 环境确认通过后，立即勾选 `环境启动完成`。

4. 在 harness 环境基线上编写或更新测试。
   - 写测试前先阅读对应应用的 `application-harness.yml`，确认 DB、Redis、Nacos、RocketMQ、mock-upstream、端口和 profile 配置。
   - 测试数据必须基于 harness seed、明确的基线 SQL 变更或方案指定的临时 fixture，不要依赖远程测试环境数据。
   - 新增或更新的一次性测试资产必须写入迭代目录的 `<featureRoot>/test/` 下，不要写入仓库根目录的 `harness/` 下。
   - 新增辅助脚本放到 `<featureRoot>/test/scripts/`；新增说明文档、执行记录或报告放到 `<featureRoot>/test/docs/` 或 `<featureRoot>/test/reports/`。
   - 写 SQL 前先判断归属：
     - `harness/tests/fixtures/db/schema.sql` 只放项目 harness 测试的基线表结构；新增表、字段、索引、字典表等确实会被后续其他功能测试复用时，才写入这里。
     - `harness/tests/fixtures/db/init_data.sql` 只放项目 harness 测试的基线初始化数据；稳定、通用、跨功能可复用的数据才写入这里。
     - `<featureRoot>/test/fixtures/db/tmp/` 放特定 `TC-xxx` 的临时 SQL，包括一次性场景数据、边界数据、异常数据、幂等/并发专用数据和清理 SQL。
   - 临时 SQL 文件名必须按 `TC-{需求名称}-xxx.sql` 命名，例如 `TC-order-create-001.sql`、`TC-pay-callback-idempotent-003.sql`；`{需求名称}` 使用当前测试内容或场景的短横线标识，`xxx` 使用对应 TC 序号或用途后缀。
   - 如果要修改 `schema.sql` 或 `init_data.sql`，必须在测试方案的 `## 数据准备` 中补充“基线变更：是”和判断依据；不确定是否可复用时，使用迭代临时 SQL。
   - `node_api`：
     - 测试文件放在 `<featureRoot>/test/api/`，文件名使用 `*.test.mjs`。
     - HTTP fixture 放在 `<featureRoot>/test/fixtures/http/`。
     - 临时 DB fixture 放在 `<featureRoot>/test/fixtures/db/tmp/`。
     - 优先使用 Node.js 内置 `node:test`、`node:assert/strict` 和全局 `fetch`，不要默认引入需要额外安装的测试框架。
     - 可复用 harness 中已有的配置、端口、token 和 fixture 思路，但不要把新测试文件写回 `harness/tests/`；必要时在 `<featureRoot>/test/helpers/` 中封装 Node.js helper。
     - 使用黑盒 HTTP 断言，并按方案补充 DB、Redis、MQ、Mock 副作用断言。
   - `java_unit`：
     - 测试文件放在 `backend/<module>/src/test/java/`。
     - 仅当方案允许 `unit` 或最低覆盖等级不高于 `unit` 时，才能作为成功路径。
   - `java_integration`：
     - 测试文件放在 `backend/<module>/src/test/java/`。
     - 用于 Spring Bean、DAO、事务、MQ listener 等需要 Spring 上下文或基础设施的场景。
     - 需要 Spring 配置时，必须显式使用 `harness` profile 或等价 test 配置，不要误连 dev 环境。
   - `manual_smoke`：
     - 只执行方案要求的 smoke 步骤。
     - 不要把 smoke 结果描述成完整自动化覆盖。

5. 保持测试与方案映射。
   - 每个新增或修改的测试用例，都必须能对应测试方案中的一个 `TC-xxx`。
   - 每个不可替代断言 `NNA-xxx` 都必须能对应至少一个测试断言。
   - 如果执行中发现方案不合理，先更新测试方案并重新运行 `validate-test-plan.mjs`，再继续改测试。
   - 不要绕过测试方案直接改变测试类型或覆盖等级。

6. 按 TC 小闭环执行。
   - 不要一次性写完几十个测试再统一修复。
   - 按 `TC-xxx` 或强相关小组执行：实现一个/一组，运行 targeted test，修复到通过，再进入下一组。
   - 每完成一个阶段，立即更新 `## 执行进度` 中对应 checkbox。
   - 不得到最后才一次性勾选全部。
   - 同一个 TC 连续修复并重跑 3 次仍失败时，停止扩散，汇报根因或阻塞。

7. 执行测试。
   - 调试阶段优先运行 targeted test：
     - Node.js：`node --test <file>`
     - Java Gradle（Windows）：`cd <backendRoot>; .\gradlew.bat :<module>:test --tests <TestClassName>.<methodName>`
     - Java Maven（Windows）：`cd <backendRoot>; mvn.cmd -pl <module> -am -Dtest=<TestClassName>#<methodName> test`
   - `node_api` 全量回归：优先执行 `node --test <featureRoot>/test/api`；如仓库提供支持外部测试路径的 harness 包装命令，可使用该命令但测试文件仍必须位于 `<featureRoot>/test/api/`。
   - Windows 中 Node.js 测试必须从 PowerShell执行并使用 Windows 路径；禁止从 WSL调用 Windows `node.exe` 或向 Windows可执行文件传入 `/mnt/...` 路径。
   - `java_unit` / `java_integration` 全量回归：按项目构建系统使用 Windows `gradlew.bat` 或 `mvn.cmd`；纯 Linux 环境使用对应 Unix wrapper。
   - `manual_smoke`：执行方案列出的 smoke 操作。
   - 失败时查看相关报告和日志，定位根因。
   - 如果失败根因连续指向环境问题，例如 Docker 依赖不可用、端口冲突、profile 未生效、DB/Redis/MQ/Nacos/mock-upstream 无法连接，连续 2 次重跑或修复后仍因环境问题失败时，停止继续测试，汇报已确认的环境问题、影响的 TC/命令、已尝试的修复，并等待用户确认是否继续。
   - 所有目标 TC 通过后，再执行方案对应的全量回归。

8. 修复并回归。
   - 根据根因修复产品代码、harness 代码、测试代码或测试数据。
   - 修复后重跑失败测试。
   - 直到测试方案要求覆盖的用例和不可替代断言通过，或明确说明无法通过的阻塞原因。

9. 执行工程校验。
   - 先停止 harness 应用进程树并确认应用端口已释放，再执行 `clean`、构建、lint 和 Java 测试。
   - 纯 Linux 环境可以执行 `bash harness/scripts/ai-check.sh`。
   - Windows + WSL 环境不得执行会在 WSL 中调用 Unix Gradle/Maven 的 `ai-check.sh`；在 PowerShell中使用 `gradlew.bat` 或 `mvn.cmd` 执行等价完整校验。
   - 分别记录编译、lint 和 Java 测试结果；其中任一失败都不能勾选 `工程校验通过`。
   - 如果本次改动范围很小且用户允许缩小校验范围，可以只跑相关检查，但最终回复必须说明原因。
   - 工程校验通过后，勾选 `工程校验通过`。

10. 清理 Docker harness 环境。
   - 使用仓库实际的 `.env` 和 compose 文件，不要套用不存在的固定路径。
   - 纯 Linux 环境执行项目清理脚本或 `docker compose ... down`。
   - 纯 Windows 环境从 PowerShell执行实际 `docker compose ... down`。
   - Windows + WSL 环境使用 `wsl.exe --cd <repo-root> bash <deps-down-script>`；没有脚本时从 WSL执行实际 compose 命令。
   - 清理后从 Windows确认应用进程已停止、Compose 容器为空、目标端口已释放；测试报告必须保留。
   - 清理完成后，勾选 `清理完成`。

## 成功标准

只有满足以下条件时，才能汇报验证成功：

- 测试方案存在且通过 `validate-test-plan.mjs`。
- harness Docker 环境已启动，目标应用确认使用 `harness` profile。
- Windows 混合环境中，应用、测试和工程校验均在 Windows 本机执行，WSL 仅用于 Docker/DB Shell；没有发生未经说明的执行面切换。
- 测试配置基于 `application-harness.yml`，没有误连 `dev` 或远程测试环境。
- 实际覆盖等级不低于 `最低可接受等级`。
- 新增或修改的测试与 `TC-xxx` 对应。
- `## 执行进度` 已按实际执行状态更新。
- `## 不可替代断言` 已全部覆盖并通过。
- 测试方案要求的测试已执行并通过。
- 必要工程校验已执行并通过；若跳过，必须说明原因。
- 如果要求清理环境，已完成清理。

## 最终回复格式

最终回复保持简洁，包含：

- 测试方案路径和校验结果。
- 实际执行矩阵：Docker/DB Shell、应用、测试、构建分别在哪个环境执行，以及是否发生偏离。
- harness Docker 环境启动结果，以及目标应用 profile。
- `application-harness.yml` 配置确认结果。
- 目标覆盖等级、最低可接受覆盖等级、实际覆盖等级。
- 是否发生降级；如果发生，用户是否批准。
- 采用的主测试类型。
- 不可替代断言覆盖结果。
- 新增或修改的测试文件。
- 测试执行结果。
- 工程校验结果。
- 清理结果。
- 报告路径。
