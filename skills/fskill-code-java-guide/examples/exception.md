# 异常（Exception / ErrorCode / GlobalExceptionHandler）写法

> 第二层原子事例 · 后端 Java 规范
> 适用场景：业务校验失败、外部调用失败、全局兜底异常处理
> 速查索引：见 [SKILL.md](../SKILL.md) §3 索引目录

---

## 1. 放置位置

| 类型 | 位置 | 说明 |
|------|------|------|
| 通用业务异常 | `common.exception.BusinessException` | 业务可预期异常 |
| 通用错误码枚举 | `common.exception.ErrorCodeEnum` | 统一错误码定义 |
| 全局异常处理器 | `web.handler.GlobalExceptionHandler` | 兜底处理 |

## 2. 错误码分段规划

- `1xxxx` 参数校验类
- `2xxxx` 业务规则类
- `3xxxx` 外部调用类
- `5xxxx` 系统类

## 3. 使用规范

- **业务校验失败**：抛 `BusinessException(ErrorCodeEnum.XXX)` 或带占位参数。
- **外部调用失败**：抛 `BusinessException(ErrorCodeEnum.REMOTE_CALL_FAIL, serviceName)`。
- **禁止**在 Controller 里 try-catch 业务异常后自己返回，统一交给全局处理器。
- **禁止**用 `RuntimeException` 代替业务异常，必须有明确错误码。
- **日志打点（关键）**：抛 `BusinessException` **之前必须先打 `WARN` 日志**，记录中断原因 + 关键入参/当前状态。全局异常处理器只记录错误码和 message，**不会记录业务上下文**（如当时的库存值、入参），所以必须在抛异常前由业务代码打日志。代码示例见 [logging.md](logging.md)。
  ```java
  // ✅ 正确：抛异常前先打 WARN 日志（用 LogUtil + 方法名前缀）
  if (entity.getStock() < quantity) {
      LogUtil.warn(log, "doDeductStockInTx >> 中断，库存不足，activityId={}, currentStock={}, quantity={}",
              activityId, entity.getStock(), quantity);
      throw new BusinessException(ErrorCodeEnum.ACTIVITY_STOCK_NOT_ENOUGH);
  }
  ```
- **捕获系统异常**：`LogUtil.error(log, "方法名 >> xxx, param={}", e, param)` —— **必须传 Throwable 打印堆栈**，必须记录入参上下文，禁止只打 `e.getMessage()`。

## 4. 完整示例

### 4.1 ErrorCodeEnum

```java
package com.fshows.storemate.merchant.common.exception;

import lombok.Getter;

/**
 * 统一错误码枚举。
 * 分段：1xxxx 参数校验类 / 2xxxx 业务规则类 / 3xxxx 外部调用类 / 5xxxx 系统类
 */
@Getter
public enum ErrorCodeEnum {

    // ====== 参数校验类 ======
    PARAM_INVALID(10001, "参数校验失败：{}"),

    // ====== 业务规则类 ======
    ACTIVITY_NOT_FOUND(20001, "活动不存在"),
    ACTIVITY_STATUS_INVALID(20002, "活动状态不允许操作"),
    ACTIVITY_STOCK_NOT_ENOUGH(20003, "活动库存不足"),

    // ====== 外部调用类 ======
    REMOTE_CALL_FAIL(30001, "调用 {} 服务失败"),

    // ====== 系统类 ======
    SYSTEM_ERROR(50000, "系统繁忙，请稍后再试");

    private final Integer code;
    private final String desc;

    ErrorCodeEnum(Integer code, String desc) {
        this.code = code;
        this.desc = desc;
    }
}
```

### 4.2 BusinessException

```java
package com.fshows.storemate.merchant.common.exception;

import lombok.Getter;

/**
 * 业务异常。
 * 占位符使用 {}，构造时通过 MessageFormat 替换。
 */
@Getter
public class BusinessException extends RuntimeException {

    /** 错误码 */
    private final Integer code;

    /**
     * 构造业务异常（无占位参数）。
     *
     * @param errorCode 错误码枚举
     */
    public BusinessException(ErrorCodeEnum errorCode) {
        super(errorCode.getDesc());
        this.code = errorCode.getCode();
    }

    /**
     * 构造业务异常（带占位参数，desc 中的 {} 按顺序替换）。
     *
     * @param errorCode 错误码枚举
     * @param args      占位参数
     */
    public BusinessException(ErrorCodeEnum errorCode, Object... args) {
        super(formatMessage(errorCode.getDesc(), args));
        this.code = errorCode.getCode();
    }

    /**
     * 简易占位符替换：将 {} 按顺序替换为 args。
     *
     * @param template 模板
     * @param args     参数
     * @return 替换后的消息
     */
    private static String formatMessage(String template, Object... args) {
        if (args == null || args.length == 0) {
            return template;
        }
        String result = template;
        for (Object arg : args) {
            int idx = result.indexOf("{}");
            if (idx < 0) {
                break;
            }
            result = result.substring(0, idx) + arg + result.substring(idx + 2);
        }
        return result;
    }
}
```

### 4.3 GlobalExceptionHandler

```java
package com.fshows.storemate.merchant.web.handler;

import com.fshows.storemate.merchant.common.exception.BusinessException;
import com.fshows.storemate.merchant.common.response.Result;
import com.fshows.storemate.merchant.common.util.LogUtil;
import jakarta.validation.ConstraintViolationException;
import lombok.extern.slf4j.Slf4j;
import org.springframework.http.HttpStatus;
import org.springframework.validation.BindException;
import org.springframework.web.bind.MethodArgumentNotValidException;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.ResponseStatus;
import org.springframework.web.bind.annotation.RestControllerAdvice;

/**
 * 全局异常处理器。
 * Controller 抛出的异常在此统一兜底，禁止在 Controller 内 try-catch 业务异常后自行返回。
 */
@Slf4j
@RestControllerAdvice
public class GlobalExceptionHandler {

    /**
     * 业务异常：返回 200 + Result.fail。
     *
     * @param e 业务异常
     * @return 失败响应
     */
    @ExceptionHandler(BusinessException.class)
    public Result<Void> handleBusiness(BusinessException e) {
        LogUtil.warn(log, "GlobalExceptionHandler >> 业务异常，code={}, message={}", e.getCode(), e.getMessage());
        return Result.fail(e.getCode(), e.getMessage());
    }

    /**
     * @Valid 校验失败。
     *
     * @param e 校验异常
     * @return 失败响应
     */
    @ExceptionHandler(MethodArgumentNotValidException.class)
    public Result<Void> handleValid(MethodArgumentNotValidException e) {
        String message = e.getBindingResult().getFieldErrors().stream()
                .findFirst()
                .map(err -> err.getDefaultMessage())
                .orElse("参数校验失败");
        LogUtil.warn(log, "GlobalExceptionHandler >> 参数校验失败，message={}", message);
        return Result.fail(10001, message);
    }

    /**
     * Bind 校验失败。
     *
     * @param e 校验异常
     * @return 失败响应
     */
    @ExceptionHandler(BindException.class)
    public Result<Void> handleBind(BindException e) {
        String message = e.getBindingResult().getFieldErrors().stream()
                .findFirst()
                .map(err -> err.getDefaultMessage())
                .orElse("参数校验失败");
        LogUtil.warn(log, "GlobalExceptionHandler >> 参数绑定失败，message={}", message);
        return Result.fail(10001, message);
    }

    /**
     * ConstraintViolation 校验失败。
     *
     * @param e 校验异常
     * @return 失败响应
     */
    @ExceptionHandler(ConstraintViolationException.class)
    public Result<Void> handleConstraintViolation(ConstraintViolationException e) {
        String message = e.getConstraintViolations().stream()
                .findFirst()
                .map(v -> v.getMessage())
                .orElse("参数校验失败");
        LogUtil.warn(log, "GlobalExceptionHandler >> 约束校验失败，message={}", message);
        return Result.fail(10001, message);
    }

    /**
     * 兜底未知异常：返回 500 + 系统错误码。
     *
     * @param e 未知异常
     * @return 失败响应
     */
    @ExceptionHandler(Exception.class)
    @ResponseStatus(HttpStatus.INTERNAL_SERVER_ERROR)
    public Result<Void> handleUnknown(Exception e) {
        LogUtil.error(log, "GlobalExceptionHandler >> 系统异常", e);
        return Result.fail(50000, "系统繁忙，请稍后再试");
    }
}
```

### 4.4 Service 中抛业务异常

```java
ActivityEntity entity = activityRepository.findById(id);
if (entity == null) {
    // ✅ 抛异常前先打 WARN：方法名 >> 原因 + 入参（用 LogUtil）
    LogUtil.warn(log, "getActivity >> 中断，活动不存在，activityId={}", id);
    throw new BusinessException(ErrorCodeEnum.ACTIVITY_NOT_FOUND);
}
if (!ActivityStatusEnum.RUNNING.getCode().equals(entity.getStatus())) {
    // ✅ 抛异常前先打 WARN：方法名 >> 原因 + 当前状态 + 入参
    LogUtil.warn(log, "getActivity >> 中断，状态不允许操作，activityId={}, currentStatus={}",
            id, entity.getStatus());
    throw new BusinessException(ErrorCodeEnum.ACTIVITY_STATUS_INVALID);
}

// 带占位参数
// ✅ 抛异常前先打 WARN
LogUtil.warn(log, "getActivity >> 中断，调用外部服务失败，serviceName={}", "coupon-service");
throw new BusinessException(ErrorCodeEnum.REMOTE_CALL_FAIL, "coupon-service");
```

## 5. 最佳实践提示

- **抛异常前必须先打日志**（代码示例见 [logging.md](logging.md)）：每个抛 `BusinessException` 的地方，**前面必须有一行 `LogUtil.warn`**，记录中断原因 + 关键入参/当前状态。全局异常处理器只记错误码，不记业务上下文——排查时全靠这行 WARN 日志。
- 错误码新增时严格按分段规划分配，禁止跨段乱用。
- 占位符 `{}` 在 `ErrorCodeEnum.desc` 里声明，在 `BusinessException` 构造时传入 args 替换，避免日志里出现裸 `{}`。
- **禁止**用 `throw new RuntimeException("xxx")` 代替业务异常，必须有错误码便于前端/调用方识别。
- 全局处理器里业务异常用 `LogUtil.warn`（频繁但可预期），系统异常用 `LogUtil.error(log, "xxx", e)` 且**必须**传 Throwable，禁止只打印 `e.getMessage()`。
- Controller 不要 try-catch 业务异常后 `Result.fail(...)`，会让全局处理器失效、错误码日志丢失。
- **捕获系统异常必须传 Throwable 打堆栈**：`LogUtil.error(log, "方法名 >> xxx, param={}", e, param)` —— Throwable 在 format 后、args 前，LogUtil 内部用 `StrUtil.format` 替换占位符后传给 SLF4J 打堆栈。禁止 `LogUtil.error(log, "失败：" + e.getMessage())`（拼接+丢堆栈），禁止不传 `e`（丢堆栈）。
