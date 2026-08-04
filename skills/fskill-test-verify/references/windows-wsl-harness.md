# Windows + WSL Harness 执行规范

当仓库位于 Windows 磁盘、Docker 由 Docker Desktop 提供、Shell 脚本通过 WSL 执行时，使用本规范。目标是固定执行面，禁止在失败后临时切换 Java、Node.js 或 Gradle 的运行环境。

## 固定执行矩阵

| 操作 | 执行环境 | 约束 |
|---|---|---|
| Docker Desktop 检查 | Windows PowerShell | 先执行 `docker info` |
| `.sh` 依赖/DB/清理脚本 | WSL | 使用 `wsl.exe --cd <repo-root> bash <script>` |
| Docker Compose 状态核对 | Windows PowerShell 或 WSL | 同一次任务固定一种入口 |
| Java 应用编译、启动、停止 | Windows PowerShell | Gradle 使用 `gradlew.bat`，Maven 使用 `mvn.cmd` |
| Node.js API 测试 | Windows PowerShell | 使用 Windows `node.exe` 和 Windows 路径 |
| Java 测试和工程校验 | Windows PowerShell | 应用停止后执行 |
| 健康检查、日志和报告 | Windows PowerShell | 访问 `localhost` 暴露端口 |

WSL 只负责 Docker 依赖和数据库 Shell。不要要求 WSL 安装 JDK、Gradle、Maven、Node.js 测试依赖或项目构建缓存。

## 预检

在修改环境前从 PowerShell 检查：

```powershell
git status --short
docker info
wsl.exe --status
Get-Command node, java -ErrorAction Stop
Test-Path <backend-root>\gradlew.bat
Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
  Where-Object LocalPort -In 8083,3307,6380,8848,9001,9877,10911
```

同时读取将要调用的 `.sh`。如果脚本包含 `gradlew`、`mvn`、`bootRun`、`spring-boot:run`、Node.js 测试或应用健康等待，不要在 WSL 执行整段脚本；只调用其中纯 Docker/DB 子脚本。

Docker Desktop 未运行时，可以从 PowerShell启动并等待 `docker info` 成功。连续两次仍不可用时按环境阻塞停止。

## 启动依赖和初始化数据库

优先调用仓库已有的纯依赖脚本：

```powershell
wsl.exe --cd <repo-root> bash harness/scripts/init/ai-deps-up.sh
wsl.exe --cd <repo-root> bash harness/scripts/init/ai-init-db.sh
```

脚本路径以仓库实际结构为准。不要调用会继续启动 Java 应用的 `ai-env-up.sh`。

必须确认初始化目标是本地 harness 数据库；拒绝非 localhost/127.0.0.1 或非项目允许名称的数据库。

## 启动 Windows 应用

使用原生 PowerShell设置 profile，并隐藏启动窗口：

```powershell
$env:SPRING_PROFILES_ACTIVE = 'harness'
$env:SERVER_PORT = '<harness-port>'
$process = Start-Process `
  -FilePath '<backend-root>\gradlew.bat' `
  -ArgumentList ':<web-module>:bootRun' `
  -WorkingDirectory '<backend-root>' `
  -RedirectStandardOutput '<report-dir>\app.out.log' `
  -RedirectStandardError '<report-dir>\app.err.log' `
  -WindowStyle Hidden `
  -PassThru
```

Maven 项目使用 `mvn.cmd` 的等价启动命令。记录根 PID，并在后续停止整个子进程树。

确认条件：

- Windows `localhost:<port>` 健康检查成功；
- 日志明确显示 `harness` 是激活 profile；
- 不以 actuator/info 缺失为理由跳过日志中的 profile 证明；
- 8083 等目标端口没有被另一个应用占用。

## 执行测试

从 PowerShell使用 Windows Node.js 和 Windows 路径：

```powershell
node --test '<featureRoot>\test\api'
```

禁止：

- 从 WSL 调用 `node --test`；
- 从 WSL 调用 `.venv\Scripts\python.exe`；
- 向 Windows 可执行文件传递 `/mnt/<drive>/...` 路径；
- 由于 WSL 无法访问 Windows `localhost` 而把应用重新启动到 WSL。

如果仓库遗留 Python API 测试，同样从 PowerShell使用 Windows Python和 Windows路径执行。

## 停止应用和工程校验

API 测试结束后，工程校验前必须停止应用进程树并确认端口释放。不能在 `bootRun` 持有 JAR/build 目录时执行 `clean`。

Gradle：

```powershell
Set-Location '<backend-root>'
.\gradlew.bat clean
.\gradlew.bat build -x test
.\gradlew.bat test
```

Maven：

```powershell
Set-Location '<backend-root>'
mvn.cmd test
```

不要从 WSL 在 `/mnt/<drive>` 上执行 Gradle；这会引入文件哈希 I/O、权限、锁和缓存差异。

分别记录：编译结果、lint 结果、Java 测试结果。lint 失败但 Java 测试通过时，两者不能合并成“工程校验通过”。

## 清理

使用 WSL 执行仓库实际的 Compose 清理脚本或命令：

```powershell
wsl.exe --cd <repo-root> bash harness/scripts/ai-deps-down.sh
```

没有脚本时，使用实际 `.env` 和 compose 文件：

```powershell
wsl.exe --cd <repo-root> docker compose --env-file <env-file> -f <compose-file> down
```

最后从 PowerShell确认应用已停止、Compose 无容器、目标端口已释放。保留测试报告。

## 失败处理

不要在失败后切换执行面：

- WSL Gradle失败 → 不在 WSL继续修复 Java环境；回到既定 Windows Gradle路径。
- WSL无法访问 Windows localhost → 不把应用移到 WSL；测试必须在 Windows执行。
- Windows `clean` 被锁 → 停止应用进程树后重试一次。
- Windows工具缺失 → 汇报环境阻塞，不使用 WSL工具冒充替代。

只有执行矩阵本身与仓库不兼容时才调整矩阵，并先向用户说明。最终回复列出实际执行面和任何偏离。
