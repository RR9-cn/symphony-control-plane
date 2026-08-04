# 统一响应体（Result / PageResult）写法

> 第二层原子事例 · 后端 Java 规范
> 适用场景：HTTP 出口统一响应结构、分页查询返回结构
> 速查索引：见 [SKILL.md](../SKILL.md) §3 索引目录

---

## 1. 放置位置

- `Result<T>` / `PageResult<T>` 放在 `common.response`。
- 业务对外响应 DTO 放 `api.{子域}.dto`，不污染通用响应体。
- Controller 方法签名返回业务 DTO / `PageResult<T>`，由 `web.advice.UnifiedResponseBodyAdvice` 自动包装为 `Result<T>`。

## 2. 字段说明

### 2.1 `Result<T>`

| 字段 | 类型 | 说明 |
|------|------|------|
| `success` | `Boolean` | true 表示成功 |
| `code` | `Integer` | 业务码，成功为 0，失败为具体错误码 |
| `message` | `String` | 提示信息 |
| `data` | `T` | 业务数据 |

### 2.2 `PageResult<T>`

| 字段 | 类型 | 说明 |
|------|------|------|
| `pageNum` | `Integer` | 当前页 |
| `pageSize` | `Integer` | 每页大小 |
| `total` | `Long` | 总条数 |
| `totalPages` | `Integer` | 总页数 |
| `list` | `List<T>` | 当前页数据 |

## 3. 静态方法

| 方法 | 说明 |
|------|------|
| `Result.success()` | 成功响应（无数据） |
| `Result.success(T data)` | 成功响应（带数据） |
| `Result.fail(Integer code, String message)` | 失败响应 |
| `PageResult.of(List<T>, Long total, Integer pageNum, Integer pageSize)` | 构造分页结果 |

## 4. 完整示例

### 4.1 Result 定义

```java
package com.fshows.storemate.merchant.common.response;

import lombok.Data;

/**
 * 统一响应体。
 */
@Data
public class Result<T> {

    /** 是否成功 */
    private Boolean success;

    /** 业务码，成功为 0，失败为具体错误码 */
    private Integer code;

    /** 提示信息 */
    private String message;

    /** 业务数据 */
    private T data;

    /**
     * 成功响应（无数据）。
     *
     * @param <T> 数据类型
     * @return 成功响应
     */
    public static <T> Result<T> success() {
        return success(null);
    }

    /**
     * 成功响应（带数据）。
     *
     * @param data 业务数据
     * @param <T>  数据类型
     * @return 成功响应
     */
    public static <T> Result<T> success(T data) {
        Result<T> result = new Result<>();
        result.setSuccess(true);
        result.setCode(0);
        result.setData(data);
        return result;
    }

    /**
     * 失败响应。
     *
     * @param code    错误码
     * @param message 提示信息
     * @param <T>     数据类型
     * @return 失败响应
     */
    public static <T> Result<T> fail(Integer code, String message) {
        Result<T> result = new Result<>();
        result.setSuccess(false);
        result.setCode(code);
        result.setMessage(message);
        return result;
    }
}
```

### 4.2 PageResult 定义

```java
package com.fshows.storemate.merchant.common.response;

import lombok.Data;

import java.util.List;

/**
 * 分页响应体。
 */
@Data
public class PageResult<T> {

    /** 当前页 */
    private Integer pageNum;

    /** 每页大小 */
    private Integer pageSize;

    /** 总条数 */
    private Long total;

    /** 总页数 */
    private Integer totalPages;

    /** 当前页数据 */
    private List<T> list;

    /**
     * 构造分页结果。
     *
     * @param list     当前页数据
     * @param total    总条数
     * @param pageNum  当前页
     * @param pageSize 每页大小
     * @param <T>      元素类型
     * @return 分页结果
     */
    public static <T> PageResult<T> of(List<T> list, Long total, Integer pageNum, Integer pageSize) {
        PageResult<T> result = new PageResult<>();
        result.setList(list);
        result.setTotal(total);
        result.setPageNum(pageNum);
        result.setPageSize(pageSize);
        result.setTotalPages(pageSize == 0 ? 0 : (int) ((total + pageSize - 1) / pageSize));
        return result;
    }
}
```

### 4.3 Controller 使用

```java
@PostMapping("/create")
public ActivityResponse create(@Valid @RequestBody ActivityCreateRequest request) {
    ActivityCreateParam param = FsBeanUtil.map(request, ActivityCreateParam.class);
    ActivityModel model = activityService.createActivity(param);
    return FsBeanUtil.map(model, ActivityResponse.class);
}
```

## 5. 最佳实践提示

- Controller 层统一返回业务 DTO / `PageResult<T>`，HTTP 出口由 `UnifiedResponseBodyAdvice` 自动包装为 `Result<T>`。
- `Result<T>` 是线上协议壳，不是 Java API 方法签名；`api.{子域}.XxxApi` 禁止返回 `Result<T>`。
- **禁止**在 Controller 中手写 `Result.success(...)` / `Result.fail(...)`，禁止返回裸 Map / 裸 Entity / 裸 `XxxModel`。
- 业务异常**不要**在 Controller 里 try-catch 后自己 `Result.fail(...)`，统一抛 `BusinessException` 由 `GlobalExceptionHandler` 兜底。
- 分页接口返回 `PageResult<XxxResponse>`，HTTP 出口自动包装成 `Result<PageResult<XxxResponse>>`。
- `PageResult.of(...)` 由 Service 组装好 `Model` 列表后返回，Controller 仅做 `Model → Response` 转换。
