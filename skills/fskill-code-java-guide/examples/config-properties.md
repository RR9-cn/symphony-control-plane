# 配置类（@ConfigurationProperties / @Value）写法

> 第二层原子事例 · 后端 Java 规范
> 适用场景：读取 Nacos 配置项，注入到配置类供业务调用
> 速查索引：见 [SKILL.md](../SKILL.md) §3 索引目录

---

## 1. 放置位置

| 类型 | 位置 | 说明 |
|------|------|------|
| 业务属性配置类 | `service.config` | 如 `ActivityProperties`，对应 Nacos 业务配置 |
| Web 层专属配置 | `web.config` | 如 `WebMvcConfig`、`SwaggerConfig` |

## 2. 编写规范

- 配置项较多时使用 `@ConfigurationProperties` 绑定到配置类；配置项极少（1-2 个）且不需刷新时用 `@Value` 在配置类中注入。
- 配置类用 `@Component` + `@ConfigurationProperties(prefix = "xxx")`，需要动态刷新加 `@RefreshScope`。
- **禁止**在 Service / Manager / Controller / Repository 中直接使用 `@Value`，必须通过配置类的 get 方法获取。
- `@Value` 必须提供默认值（`${xxx:default}`），避免配置缺失导致启动失败。
- 类、字段必须有 JavaDoc。

## 3. 完整示例

### 3.1 @ConfigurationProperties（多配置项）

```java
package com.fshows.storemate.merchant.service.config;

import lombok.Data;
import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.cloud.context.config.annotation.RefreshScope;
import org.springframework.stereotype.Component;

import java.util.List;

/**
 * 活动业务配置。
 * 对应 Nacos 中 storemate-merchant-service.yml 的 activity 节点。
 */
@Data
@RefreshScope
@Component
@ConfigurationProperties(prefix = "activity")
public class ActivityProperties {

    /** 默认活动有效期（天） */
    private Integer defaultDurationDays = 30;

    /** 单用户最大参与次数 */
    private Integer maxJoinPerUser = 1;

    /** 允许的活动类型列表 */
    private List<Integer> allowedTypes;
}
```

对应 Nacos 配置（`storemate-merchant-service.yml`）：

```yaml
activity:
  default-duration-days: 7
  max-join-per-user: 3
  allowed-types: [1, 2, 3]
```

### 3.2 @Value（单项配置）

```java
package com.fshows.storemate.merchant.service.config;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

/**
 * 活动批量配置。
 * 单项配置通过 @Value 注入，暴露 get 方法供业务调用。
 */
@Component
public class ActivityBatchProperties {

    /** 单次批量大小，默认 100 */
    @Value("${activity.batch.size:100}")
    private Integer batchSize;

    /**
     * 获取单次批量大小。
     *
     * @return 批量大小
     */
    public Integer getBatchSize() {
        return batchSize;
    }
}
```

### 3.3 Service 中通过配置类调用

```java
@Service
public class ActivityServiceImpl implements IActivityService {

    /** 活动业务配置 */
    @Autowired
    private ActivityProperties activityProperties;

    /** 活动批量配置 */
    @Autowired
    private ActivityBatchProperties activityBatchProperties;

    @Override
    public ActivityModel createActivity(ActivityCreateParam param) {
        // 正确：通过配置类的 get 方法获取
        Integer maxJoin = activityProperties.getMaxJoinPerUser();
        Integer defaultDays = activityProperties.getDefaultDurationDays();
        // ...
    }

    public void batchProcess() {
        Integer batchSize = activityBatchProperties.getBatchSize();
        // ...
    }
}
```

### 3.4 bootstrap.yml（web 模块 resources）

```yaml
spring:
  application:
    name: storemate-merchant-service
  profiles:
    active: ${SPRING_PROFILES_ACTIVE:dev}
  cloud:
    nacos:
      discovery:
        server-addr: ${NACOS_HOST:it-nacos}:${NACOS_PORT:8848}
        namespace: ${NACOS_NAMESPACE:storemate-dev}
      config:
        server-addr: ${NACOS_HOST:it-nacos}:${NACOS_PORT:8848}
        namespace: ${NACOS_NAMESPACE:storemate-dev}
        file-extension: yml
        shared-configs:
          - data-id: common.yml
            refresh: true
```

## 4. 最佳实践提示

- **禁止**在 Service / Manager / Controller / Repository 直接 `@Value`，违反"配置统一在配置类读取"原则，会让配置散落难维护。
  ```java
  // 禁止：在 Service 中直接 @Value
  @Service
  public class ActivityServiceImpl implements IActivityService {
      @Value("${activity.batch.size:100}")  // 禁止
      private Integer batchSize;
  }
  ```
- `@RefreshScope` 仅在配置需要动态刷新时加，配合 Nacos 配置变更实时生效；不需要刷新的配置（如启动期一次性加载）不加，避免无谓的代理开销。
- 敏感信息（密码、密钥）**禁止**明文写入 Nacos，使用加密配置或环境变量注入。
- 环境隔离：dev / test / prod 使用不同 Nacos 命名空间，**禁止**跨环境共用配置。
- Nacos 配置变更需保留历史版本，便于回滚。
