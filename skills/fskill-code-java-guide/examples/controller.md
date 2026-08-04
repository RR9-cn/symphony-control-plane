# Controller 写法

> 第二层原子事例 · 后端 Java 规范
> 适用场景：HTTP 接口入口，参数接收、转换、调用 Service、返回业务 DTO
> 速查索引：见 [SKILL.md](../SKILL.md) §3 索引目录

---

## 1. 放置位置

Controller 按「对外 HTTP」与「对内 RPC」物理分包：

| 类型 | 放置位置 | 路径前缀 | 命名 |
|------|---------|---------|------|
| 对外 HTTP Controller | `web.controller.external.{子域}` | `/api/**`（验证/临时接口可用业务前缀如 `/verifier/**`） | `XxxController` |
| 对内 RPC Controller | `web.controller.internal.{子域}` | `/rpc/**` | `XxxRpcController` |

> 对内 RPC Controller 通过 `implements api.{子域}.XxxApi` 实现 Feign 契约，DTO 类型天然一致，详见本章 §4.2。

## 2. 职责边界

| 应做 | 禁止 |
|------|------|
| 参数接收 | 写业务逻辑 |
| `@Valid` 触发 JSR-303 校验 | 直接调用 Manager / Mapper / Repository / Client |
| `Request → Param` 简单协议转换（可用 `FsBeanUtil`） | 手写大量 setter 转换 |
| 调用 Service 接口（`IXxxService`） | try-catch 业务异常后自己 `Result.fail` |
| `Model → Response` 简单协议转换（可用 `FsBeanUtil`） | 返回裸 Map / 裸 Entity / 裸 Model |
| 返回业务 DTO / `PageResult<T>`，由 `UnifiedResponseBodyAdvice` 自动包装 | 直接返回 Service 的 Model / 手写 `Result.success(...)` |
| 关键节点日志（入参、出参） | 在循环内打日志 |

## 3. 编写规范

- 使用 `@RestController` + `@RequestMapping("/xxx")`。
- 依赖统一 `@Autowired` 字段注入，注入 **Service 接口**，不注入实现类。
- 入参对象用 `@Valid @RequestBody`，路径变量用 `@PathVariable`，查询参数用 `@RequestParam`。
- Controller 只允许做 web/api DTO 与 Service DTO 之间的简单协议转换；复杂业务/DAL 转换必须在 Service 层委托 `XxxAssembler` 完成。
- 类、方法、注入字段必须有 JavaDoc。
- 类内方法按业务分组排序：创建 → 更新 → 查询 → 删除。

## 4. 完整示例

### 4.1 对外 HTTP Controller

```java
package com.fshows.storemate.merchant.web.controller.external.activity;

import com.fshows.storemate.merchant.api.activity.dto.ActivityCreateRequest;
import com.fshows.storemate.merchant.api.activity.dto.ActivityQueryRequest;
import com.fshows.storemate.merchant.api.activity.dto.ActivityResponse;
import com.fshows.storemate.merchant.common.response.PageResult;
import com.fshows.storemate.merchant.common.util.FsBeanUtil;
import com.fshows.storemate.merchant.common.util.LogUtil;
import com.fshows.storemate.merchant.service.service.activity.IActivityService;
import com.fshows.storemate.merchant.service.service.activity.model.ActivityModel;
import com.fshows.storemate.merchant.service.service.activity.param.ActivityCreateParam;
import com.fshows.storemate.merchant.service.service.activity.param.ActivityQueryParam;
import jakarta.validation.Valid;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

/**
 * 活动对外 HTTP 接口（面向商户/前端/第三方）。
 * 路径前缀 /api/activity，与内部 RPC 接口 /rpc/activity 物理隔离。
 */
@Slf4j
@RestController
@RequestMapping("/api/activity")
public class ActivityController {

    /** 活动服务 */
    @Autowired
    private IActivityService activityService;

    /**
     * 创建活动。
     *
     * @param request 创建请求
     * @return 活动信息
     */
    @PostMapping("/create")
    public ActivityResponse create(@Valid @RequestBody ActivityCreateRequest request) {
        LogUtil.info(log, "create >> 创建活动入参，name={}, type={}", request.getName(), request.getType());
        // 1. Request → Param
        ActivityCreateParam param = FsBeanUtil.map(request, ActivityCreateParam.class);
        // 2. 调用 Service
        ActivityModel model = activityService.createActivity(param);
        // 3. Model → Response
        return FsBeanUtil.map(model, ActivityResponse.class);
    }

    /**
     * 分页查询活动。
     *
     * @param request 查询请求
     * @return 分页结果
     */
    @PostMapping("/page")
    public PageResult<ActivityResponse> page(@Valid @RequestBody ActivityQueryRequest request) {
        ActivityQueryParam param = FsBeanUtil.map(request, ActivityQueryParam.class);
        PageResult<ActivityModel> modelPage = activityService.pageActivity(param);
        // 列表元素 Model → Response
        PageResult<ActivityResponse> respPage = PageResult.of(
                FsBeanUtil.mapList(modelPage.getList(), ActivityResponse.class),
                modelPage.getTotal(),
                modelPage.getPageNum(),
                modelPage.getPageSize()
        );
        return respPage;
    }
}
```

### 4.2 对内 RPC Controller（implements Feign 契约）

> 对内 RPC 接口采用「Feign 契约即接口」模式：契约定义在 `api.{子域}.XxxApi`，Controller 通过 `implements XxxApi` 实现契约。
> 路径前缀 `/rpc/**`，受 `InternalApiInterceptor` 鉴权保护（校验 `X-Internal-Token` 请求头）。

#### 4.2.1 Feign 契约接口（api 模块，供消费方依赖）

```java
package com.fshows.storemate.merchant.api.activity;

import com.fshows.storemate.merchant.api.activity.dto.ActivityCreateRequest;
import com.fshows.storemate.merchant.api.activity.dto.ActivityResponse;
import jakarta.validation.Valid;
import org.springframework.cloud.openfeign.FeignClient;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;

/**
 * 活动内部 RPC 接口契约（供公司内其他微服务通过 OpenFeign 调用）。
 * path 含 context-path（/storemate-merchant-service）+ /rpc/activity，Feign 调用时直接用此路径。
 */
@FeignClient(name = "storemate-merchant-service", path = "/storemate-merchant-service/rpc/activity")
public interface ActivityApi {

    /**
     * 创建活动。
     *
     * @param request 创建请求
     * @return 活动信息
     */
    @PostMapping("/create")
    ActivityResponse create(@Valid @RequestBody ActivityCreateRequest request);

    /**
     * 查询活动详情。
     *
     * @param id 活动 ID
     * @return 活动信息
     */
    @GetMapping("/{id}")
    ActivityResponse get(@PathVariable("id") Long id);
}
```

#### 4.2.2 RPC Controller 实现（web 模块）

```java
package com.fshows.storemate.merchant.web.controller.internal.activity;

import com.fshows.storemate.merchant.api.activity.ActivityApi;
import com.fshows.storemate.merchant.api.activity.dto.ActivityCreateRequest;
import com.fshows.storemate.merchant.api.activity.dto.ActivityResponse;
import com.fshows.storemate.merchant.common.util.FsBeanUtil;
import com.fshows.storemate.merchant.service.service.activity.IActivityService;
import com.fshows.storemate.merchant.service.service.activity.model.ActivityModel;
import com.fshows.storemate.merchant.service.service.activity.param.ActivityCreateParam;
import jakarta.validation.Valid;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

/**
 * 活动内部 RPC 接口实现（面向公司内其他微服务，通过 OpenFeign 调用）。
 * 实现 ActivityApi 契约，路径前缀 /rpc/activity，受 InternalApiInterceptor 鉴权保护。
 * 类级 @RequestMapping 不含 context-path（由服务端自动拼接），与 Feign 契约的 path（含 context-path）对应。
 */
@RestController
@RequestMapping("/rpc/activity")
public class ActivityRpcController implements ActivityApi {

    /** 活动服务 */
    @Autowired
    private IActivityService activityService;

    /**
     * 创建活动。
     *
     * @param request 创建请求
     * @return 活动信息
     */
    @Override
    public ActivityResponse create(@Valid @RequestBody ActivityCreateRequest request) {
        // 1. Request → Param
        ActivityCreateParam param = FsBeanUtil.map(request, ActivityCreateParam.class);
        // 2. 调用 Service
        ActivityModel model = activityService.createActivity(param);
        // 3. Model → Response
        return FsBeanUtil.map(model, ActivityResponse.class);
    }

    /**
     * 查询活动详情。
     *
     * @param id 活动 ID
     * @return 活动信息
     */
    @Override
    public ActivityResponse get(@PathVariable("id") Long id) {
        ActivityModel model = activityService.getActivity(id);
        return FsBeanUtil.map(model, ActivityResponse.class);
    }
}
```

> **context-path 处理要点**：Feign 契约 `@FeignClient` 的 `path` 写 `/storemate-merchant-service/rpc/activity`（含 context-path），Controller 的 `@RequestMapping` 写 `/rpc/activity`（不含 context-path，由服务端自动拼接）。这与 `client` 模块调外部服务时 FeignClient 路径带对方 context-path 前缀的模式一致。

## 5. 最佳实践提示

- Controller 可用 `@Slf4j` 提供 Logger，但日志输出统一走 `LogUtil.info/warn/error`，不要直接调 `log.info`。
- `Request → Param` / `Model → Response` 简单协议转换是 Controller 的边界职责，禁止把 Request 直接传给 Service，也禁止让 Controller 接触 Entity / DAL Result。
- 分页查询的列表协议转换可用 `FsBeanUtil.mapList()`，不要循环 `new Response()` + setter；复杂字段映射下沉到业务层 Assembler。
- 业务异常**不要**在 Controller try-catch 后 `Result.fail`，让 `GlobalExceptionHandler` 兜底统一处理；成功响应也不要手写 `Result.success`。
- **内外接口隔离**：对外 HTTP Controller 放 `web.controller.external.{子域}`（路径 `/api/**`），对内 RPC Controller 放 `web.controller.internal.{子域}`（路径 `/rpc/**`，`implements api.{子域}.XxxApi`）。禁止两类 Controller 混放同一包。新增内部 RPC 接口时，先在 `api.{子域}` 定义 `XxxApi` Feign 契约，再在 `web.controller.internal.{子域}` 写 `XxxRpcController implements XxxApi`，无需额外配置拦截器（`/rpc/**` 已由 `InternalApiInterceptor` 全局拦截）。
- **RPC Controller 的 `@RequestMapping` 不含 context-path**：Feign 契约 `path` 写 `/storemate-merchant-service/rpc/activity`（含 context-path），Controller 类级 `@RequestMapping` 写 `/rpc/activity`（不含 context-path，由服务端自动拼接），两者对应但不重复。
