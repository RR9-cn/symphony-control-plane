# 日志打点代码示例

> 第二层原子事例 · 后端 Java 规范
> 日常打日志看 [SKILL.md](../SKILL.md) 的最终红线和 [runtime-guardrails.md](../references/runtime-guardrails.md) 的日志规则即可；本文件只在不确定具体写法时读取。
> 本文件仅提供三种关键模式的完整代码示例，**按需读**——只有不确定怎么写时才来查。

---

## 0. 日志工具与格式

- **用 `LogUtil` 不直调 SLF4J**：`LogUtil.info(log, "xxx")` 而非 `log.info("xxx")`。
  > Agent 首次打日志前先在项目 `common/util/` 下找 `LogUtil.java`，确认其 API（info/warn/error/debug 各 4 个重载，需传入 Logger）。
- **日志以主入口方法名开头**：格式 `方法名 >> 描述`，如 `createOrder >> 开始，userId={}`。
- 类用 `@Slf4j` 获取 `log`（Logger 对象），打印走 `LogUtil.xxx(log, ...)`。
- `LogUtil` 带堆栈重载签名：`LogUtil.error(log, format, Throwable, args...)`，Throwable 在 format 后、args 前。
- **traceId 由基础设施自动放入 MDC，业务代码禁止手动处理**：`logback-spring.xml` 的 pattern 已含 `[%X{traceId:-}]`，traceId 由 `TraceIdFilter`（HTTP 入口）/ `TraceIdFeignInterceptor`（Feign 出站）/ `TraceIdRocketMqConsumerHook`（MQ 消费）/ `SchedulerxJobTraceIdAspect`（SchedulerX 入口）/ `MdcTaskDecorator`（线程池）自动放入 MDC，`LogUtil` 打日志自动带 traceId。业务代码**禁止**在日志 message 里手动拼 traceId，也**禁止**自建 `ThreadLocal` 传 traceId（虚拟线程下不安全）。
  ```java
  // ❌ 禁止：手动拼 traceId 到日志内容
  LogUtil.info(log, "createOrder >> traceId={}, userId={}", TraceIdContext.get(), userId);
  // ✅ 正确：traceId 由 pattern 的 [%X{traceId:-}] 自动输出，message 只写业务内容
  LogUtil.info(log, "createOrder >> 开始，userId={}", userId);
  ```

---

## 1. Service 主流程入口/出口 + 中断点 + 系统异常（核心模式）

```java
@Slf4j
@Service
public class ActivityServiceImpl implements IActivityService {

    @Override
    public ActivityModel createActivity(ActivityCreateParam param) {
        // ✅ 入口日志：方法名 >> 关键入参
        LogUtil.info(log, "createActivity >> 开始，name={}, type={}, stock={}",
                param.getName(), param.getType(), param.getStock());

        activityCreateManager.validate(param.getStartTime(), param.getEndTime());
        ActivityEntity entity = transactionTemplate.execute(status -> {
            ActivityEntity e = activityAssembler.toEntity(param, ActivityStatusEnum.DRAFT.getCode());
            activityRepository.save(e);
            return e;
        });

        // 事务外调外部服务
        try {
            LogUtil.info(log, "createActivity >> 调发券开始，activityId={}", entity.getId());
            CouponGrantResult result = couponClient.grantCoupon(buildForm(entity.getId()));
            if (result == null || !Boolean.TRUE.equals(result.getSuccess())) {
                LogUtil.warn(log, "createActivity >> 调发券失败，activityId={}, failReason={}",
                        entity.getId(), result == null ? "null" : result.getFailReason());
            }
        } catch (Exception e) {
            // ✅ 系统异常：LogUtil.error + 方法名前缀 + 入参 + Throwable
            LogUtil.error(log, "createActivity >> 调发券异常，activityId={}", e, entity.getId());
        }

        ActivityModel result = activityAssembler.toModel(entity);
        // ✅ 出口日志：与入口成对
        LogUtil.info(log, "createActivity >> 完成，activityId={}, status={}",
                result.getId(), result.getStatus());
        return result;
    }

    @Transactional(rollbackFor = Exception.class)
    public boolean doDeductStockInTx(Long activityId, Integer quantity) {
        LogUtil.info(log, "doDeductStockInTx >> 开始，activityId={}, quantity={}", activityId, quantity);

        ActivityEntity entity = activityRepository.findByIdForUpdate(activityId);
        if (entity == null) {
            // ✅ 中断点：抛异常前先打 WARN + 方法名前缀 + 原因 + 入参
            LogUtil.warn(log, "doDeductStockInTx >> 中断，活动不存在，activityId={}", activityId);
            throw new BusinessException(ErrorCodeEnum.ACTIVITY_NOT_FOUND);
        }
        if (entity.getStock() < quantity) {
            // ✅ 中断点：抛异常前先打 WARN + 原因 + 当前状态 + 入参
            LogUtil.warn(log, "doDeductStockInTx >> 中断，库存不足，activityId={}, currentStock={}, quantity={}",
                    activityId, entity.getStock(), quantity);
            throw new BusinessException(ErrorCodeEnum.ACTIVITY_STOCK_NOT_ENOUGH);
        }

        entity.setStock(entity.getStock() - quantity);
        activityRepository.updateById(entity);
        LogUtil.info(log, "doDeductStockInTx >> 完成，activityId={}, remainingStock={}",
                activityId, entity.getStock());
        return true;
    }
}
```

---

## 2. 反面示例（禁止写法）

```java
// ❌ 直接调 SLF4J，未用 LogUtil
log.info("创建活动开始，name={}", param.getName());

// ❌ 日志没有方法名前缀，无法按主入口检索
LogUtil.info(log, "开始，name={}", param.getName());

// ❌ e.printStackTrace()
} catch (Exception e) { e.printStackTrace(); }

// ❌ 字符串拼接 + 丢失堆栈
LogUtil.error(log, "调发券失败：" + e.getMessage());

// ❌ 不传 Throwable，堆栈丢失
LogUtil.error(log, "调发券失败，activityId={}", activityId);

// ❌ 吞异常
} catch (Exception e) { return false; }
```
