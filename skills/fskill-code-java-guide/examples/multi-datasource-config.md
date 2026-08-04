# 多数据源配置写法

> 第二层原子事例 · 后端 Java 规范
> 适用场景：dal 模块需要同时连接多个数据库（主库 + 报表库等）
> 速查索引：见 [SKILL.md](../SKILL.md) §3 索引目录

---

## 1. 放置位置

| 类型 | 位置 |
|------|------|
| 数据源配置类 | `dal.config` |
| 主库 Entity/Mapper/Repository | `dal.primary.{子域}` |
| 报表库 Entity/Mapper/Repository | `dal.report.{子域}` |

## 2. DAL 模块文件结构

```
storemate-merchant-service-dal/
└── src/main/java/com/fshows/storemate/merchant/dal/
    ├── config/                                 # 多数据源配置
    │   ├── PrimaryDataSourceConfig.java        # 主数据源配置
    │   └── ReportDataSourceConfig.java         # 报表数据源配置（可选）
    ├── primary/                                # 主数据源
    │   └── activity/
    │       ├── entity/
    │       ├── mapper/
    │       └── repository/
    └── report/                                 # 报表数据源
        └── xxx/
            ├── entity/
            ├── mapper/
            └── repository/
└── src/main/resources/
    └── mapper/
        ├── primary/                            # 主库 XML
        │   └── activity/
        │       └── ActivityMapper.xml
        └── report/                             # 报表库 XML
            └── ReportMapper.xml
```

## 3. 编写规范

- 按数据源在 dal 模块根包下分包（`dal.primary`、`dal.report`），不同数据源的 Entity/Mapper/Repository **严禁跨包引用**。
- 每个数据源在 `dal.config` 下独立配置 `DataSource` / `SqlSessionFactory` / `TransactionManager`。
- 通过 `@MapperScan(basePackages = "...", sqlSessionFactoryRef = "...")` 指定该数据源扫描的 Mapper 包。
- 主数据源加 `@Primary`，作为默认数据源和默认事务管理器。
- 多数据源事务：默认用主库 `TransactionManager`；跨库操作需显式指定 `@Transactional(transactionManager = "xxxTransactionManager")`。
- **禁止**在同一个 Service 方法中混用多个数据源的事务（除非使用 JTA），跨库操作改为通过 Client 调用对应应用接口。

## 4. 完整示例

### 4.1 主数据源配置

```java
package com.fshows.storemate.merchant.dal.config;

import com.zaxxer.hikari.HikariDataSource;
import org.apache.ibatis.session.SqlSessionFactory;
import org.mybatis.spring.SqlSessionFactoryBean;
import org.mybatis.spring.annotation.MapperScan;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.boot.autoconfigure.jdbc.DataSourceProperties;
import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.context.annotation.Primary;
import org.springframework.core.io.support.PathMatchingResourcePatternResolver;
import org.springframework.jdbc.datasource.DataSourceTransactionManager;
import org.springframework.transaction.PlatformTransactionManager;

import javax.sql.DataSource;

/**
 * 主数据源配置。
 * 扫描除 report 外的所有 Mapper 包，作为默认数据源。
 */
@Configuration
@MapperScan(
        basePackages = "com.fshows.storemate.merchant.dal.primary",
        sqlSessionFactoryRef = "primarySqlSessionFactory"
)
public class PrimaryDataSourceConfig {

    /**
     * 主数据源属性配置。
     *
     * @return 数据源属性
     */
    @Bean
    @Primary
    @ConfigurationProperties("spring.datasource.primary")
    public DataSourceProperties primaryDataSourceProperties() {
        return new DataSourceProperties();
    }

    /**
     * 主数据源。
     *
     * @return 主数据源
     */
    @Bean
    @Primary
    public DataSource primaryDataSource() {
        return primaryDataSourceProperties()
                .initializeDataSourceBuilder()
                .type(HikariDataSource.class)
                .build();
    }

    /**
     * 主数据源 SqlSessionFactory。
     *
     * @param dataSource 主数据源
     * @return SqlSessionFactory
     * @throws Exception 创建异常
     */
    @Bean
    @Primary
    public SqlSessionFactory primarySqlSessionFactory(@Qualifier("primaryDataSource") DataSource dataSource) throws Exception {
        SqlSessionFactoryBean bean = new SqlSessionFactoryBean();
        bean.setDataSource(dataSource);
        bean.setMapperLocations(new PathMatchingResourcePatternResolver()
                .getResources("classpath:mapper/primary/**/*.xml"));
        return bean.getObject();
    }

    /**
     * 主数据源事务管理器。
     *
     * @param dataSource 主数据源
     * @return 事务管理器
     */
    @Bean
    @Primary
    public PlatformTransactionManager primaryTransactionManager(@Qualifier("primaryDataSource") DataSource dataSource) {
        return new DataSourceTransactionManager(dataSource);
    }
}
```

### 4.2 报表数据源配置

```java
package com.fshows.storemate.merchant.dal.config;

import com.zaxxer.hikari.HikariDataSource;
import org.apache.ibatis.session.SqlSessionFactory;
import org.mybatis.spring.SqlSessionFactoryBean;
import org.mybatis.spring.annotation.MapperScan;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.boot.autoconfigure.jdbc.DataSourceProperties;
import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.core.io.support.PathMatchingResourcePatternResolver;
import org.springframework.jdbc.datasource.DataSourceTransactionManager;
import org.springframework.transaction.PlatformTransactionManager;

import javax.sql.DataSource;

/**
 * 报表数据源配置。
 * 仅扫描 report 子域的 Mapper 包。
 */
@Configuration
@MapperScan(
        basePackages = "com.fshows.storemate.merchant.dal.report",
        sqlSessionFactoryRef = "reportSqlSessionFactory"
)
public class ReportDataSourceConfig {

    @Bean
    @ConfigurationProperties("spring.datasource.report")
    public DataSourceProperties reportDataSourceProperties() {
        return new DataSourceProperties();
    }

    @Bean
    public DataSource reportDataSource() {
        return reportDataSourceProperties()
                .initializeDataSourceBuilder()
                .type(HikariDataSource.class)
                .build();
    }

    @Bean
    public SqlSessionFactory reportSqlSessionFactory(@Qualifier("reportDataSource") DataSource dataSource) throws Exception {
        SqlSessionFactoryBean bean = new SqlSessionFactoryBean();
        bean.setDataSource(dataSource);
        bean.setMapperLocations(new PathMatchingResourcePatternResolver()
                .getResources("classpath:mapper/report/*.xml"));
        return bean.getObject();
    }

    @Bean
    public PlatformTransactionManager reportTransactionManager(@Qualifier("reportDataSource") DataSource dataSource) {
        return new DataSourceTransactionManager(dataSource);
    }
}
```

### 4.3 application.yml（多数据源配置）

```yaml
spring:
  datasource:
    primary:                              # 主数据源
      url: jdbc:mysql://${MYSQL_HOST:it-mysql}:${MYSQL_PORT:3306}/storemate_merchant_service
      username: ${MYSQL_USER:root}
      password: ${MYSQL_PASSWORD:root}
      driver-class-name: com.mysql.cj.jdbc.Driver
      hikari:
        maximum-pool-size: 20
    report:                               # 报表数据源（可选）
      url: jdbc:mysql://${MYSQL_REPORT_HOST:it-mysql}:${MYSQL_REPORT_PORT:3306}/storemate_report
      username: ${MYSQL_REPORT_USER:root}
      password: ${MYSQL_REPORT_PASSWORD:root}
      driver-class-name: com.mysql.cj.jdbc.Driver
      hikari:
        maximum-pool-size: 10
```

### 4.4 多数据源事务使用

```java
// 默认使用主数据源事务
@Override
@Transactional(rollbackFor = Exception.class)
public ActivityModel createActivity(ActivityCreateParam param) {
    // 操作主库
    ...
}

// 报表库操作需显式指定事务管理器
@Override
@Transactional(transactionManager = "reportTransactionManager", rollbackFor = Exception.class)
public ReportModel getReport(Long id) {
    // 操作报表库
    ...
}
```

## 5. 最佳实践提示

- `@MapperScan` 的 `basePackages` 必须严格限定到对应数据源的子包，禁止扫到其他数据源的 Mapper。
- XML 路径与 Mapper 包对应：主库 XML 放 `resources/mapper/primary/**`，报表库放 `resources/mapper/report/*.xml`，避免 `SqlSessionFactory` 加载到错误数据源的 XML。
- 跨数据源的写操作**禁止**放在同一事务方法里（除非用 JTA），应通过 Client 调用对应应用的接口实现最终一致。
- 报表库通常是只读，对应 Repository 方法建议加 `@Transactional(readOnly = true, transactionManager = "reportTransactionManager")`。
- **事务内禁止网络阻塞点**：每个数据源的 `@Transactional` 方法内同样**严禁**调 FeignClient/Redis/同步 MQ，跨库联动应通过 Client 调用对应应用接口（在 `afterCommit` 阶段），不能在事务内直接调外部服务。
- **幂等写用 FOR UPDATE 行锁**：跨数据源场景下，每个数据源各自的幂等写仍在各自事务内用 `SELECT ... FOR UPDATE` 锁行 + 状态判断，详见 [idempotent-write.md](idempotent-write.md)。
