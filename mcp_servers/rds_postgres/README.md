# RDS PostgreSQL 巡检 MCP Server

一个通用的 PostgreSQL / Aurora PostgreSQL **只读巡检** MCP server,通过 28 个语义化工具,
让 DevOps Agent 能安全地排查线上 PG 问题(慢查询、锁、膨胀、复制延迟、容量、wraparound 等)。

> 它是「PG 巡检能力」,不是「某个实例的巡检器」—— 不绑定任何数据库实例。
> 每个工具的 `cluster_endpoint` 是**调用时必传**的,由 Agent 按用户上下文给出。
> 一套 server 天然巡检 N 个同类 PG 实例。

## 安全模型(先看这个)

| 设计 | 说明 |
|---|---|
| **只读账号** | 容器用专门的只读用户 `mcp_devops_ro` 连库,**绝不用 admin / master** |
| **SQL 全固化** | 28 个工具的 SQL 都写死在代码里,只查 `pg_*` / `information_schema` 等**元数据/统计视图**,**没有任何"传 SQL 进来执行"的口子** |
| **看不到业务数据** | 工具能看到"表多大、膨胀多少、被扫描多少次",但**永远看不到表里的行数据**(没有 `SELECT * FROM 业务表`) |
| **不卡业务库** | 每个连接强制 `statement_timeout=15s` + `lock_timeout=5s` + `idle_in_transaction_session_timeout=5min`,巡检 SQL 超时自动 abort |
| **endpoint 不注入** | 容器不预设任何实例地址,连哪个库由 Agent 调用时传入 |
| **凭据走 Secrets Manager** | 密码不进代码、不进镜像、不进日志,容器运行时按需拉取(5 分钟 TTL 缓存) |

---

## ⭐ 只读用户 `mcp_devops_ro` 权限配置(最重要)

巡检容器用这个只读用户连库。**框架不会自动创建它**——需要 DBA 用 admin 凭据手工创建一次。
所有同类型 PG 实例**共用同一套** `mcp_devops_ro` 凭据(单 secret 多库策略)。

### 第 1 步:创建只读用户(DBA 用 admin / master 连 cluster writer)

```sql
-- 1) 建用户(密码用强随机,32+ 字符;先用占位,下面第 3 步会重设并灌进 Secret)
CREATE USER mcp_devops_ro WITH LOGIN PASSWORD 'CHANGE_ME_临时占位'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;

-- 2) 授予 pg_monitor —— 这是巡检的核心权限(覆盖绝大多数工具)
--    pg_monitor 是 PG 10+ 内置 role,自带:
--      pg_read_all_settings  → pg_settings / SHOW
--      pg_read_all_stats     → pg_stat_*(activity / statements / user_tables / statio ...)
--      pg_stat_scan_tables   → 统计扫描
GRANT pg_monitor TO mcp_devops_ro;

-- 3) 对每个要巡检的库授予 CONNECT(per-database 工具需要连进去看表)
GRANT CONNECT ON DATABASE postgres TO mcp_devops_ro;
GRANT CONNECT ON DATABASE <你的业务库, 如 jupiter> TO mcp_devops_ro;
-- 有几个业务库就授几次
```

### 第 2 步(仅标准 RDS PostgreSQL,非 Aurora):流复制权限

只有 `inspect_pg_stat_replication`(看标准流复制延迟)需要。RDS PG 上读 `pg_stat_replication`
完整字段需要 `rds_replication` role:

```sql
-- 仅 RDS PostgreSQL 执行;Aurora 跳过(Aurora 用 inspect_replica_lag,走 aurora_replica_status())
GRANT rds_replication TO mcp_devops_ro;
```

不加也不影响其它 21 个工具,该工具会优雅降级返回 `warning`。

### 第 3 步:设密码 + 灌进 Secrets Manager

```sql
-- 在 PG 上设最终密码(与下面灌进 Secret 的必须一致)
ALTER USER mcp_devops_ro WITH PASSWORD '<32+ 字符强随机密码>';
```

```bash
# 把同一个密码灌进框架建的空壳 Secret(部署时 CDK 已建好空壳,密码=PLACEHOLDER_REPLACE_ME)
aws secretsmanager put-secret-value \
  --secret-id aws-devops-mcp/rds-postgres/devops-readonly \
  --region us-east-1 \
  --secret-string '{"username":"mcp_devops_ro","password":"<同上那个密码>"}'
```

> Secret 名固定 `aws-devops-mcp/rds-postgres/devops-readonly`,在 **Runtime 所在 region(us-east-1)**,
> 不是数据库所在 region。容器跨 region 拉这个 Secret。

### 权限够不够?—— 各工具权限要求速查

| 权限 | 覆盖的工具 |
|---|---|
| **`pg_monitor`**(必备) | 绝大多数:long_transactions / top_queries / index_usage / table_bloat / connections / blocking_chains / table_sizes / active_clients / sequence_capacity / xid_wraparound / vacuum_progress / settings / replication_slots / extensions / wait_events / cache_hit_ratio / database_sizes / index_bloat / autovacuum_tuning / schema_objects / current_queries / table_io_stats / checkpoint_stats / table_stats_freshness / temp_file_usage |
| **`CONNECT` on 业务库** | 所有 database 级工具(看具体库的表) |
| **`rds_replication`**(仅 RDS 非 Aurora) | inspect_pg_stat_replication |
| Aurora writer 连接 | inspect_replica_lag(`aurora_replica_status()` 只能在 writer 上调) |
| `pg_stat_statements` 扩展已装 | inspect_top_queries(没装会返回 warning 提示去装) |

`mcp_devops_ro` 有 `pg_monitor` 就能跑通绝大多数巡检。其余是特定场景的可选增强。

---

## 工具清单(28 个)

工具按 **scope** 分三类,这决定调用时 `database` 参数怎么传:

### 实例级(看到所有库,`database` 参数不影响结果)
| 工具 | 用途 |
|---|---|
| `inspect_long_transactions` | 长事务 / 卡住的事务(排查锁堆积、膨胀根源) |
| `inspect_top_queries` | top N 最慢 / 最耗资源 SQL(需 pg_stat_statements) |
| `inspect_connections` | 连接数与分布(排查连接耗尽 / 池泄漏) |
| `inspect_active_clients` | 活跃客户端来源(谁在连库) |
| `inspect_blocking_chains` | 锁阻塞链(谁等谁、等多久) |
| `inspect_wait_events` | wait event 分布(性能排查第一步) |
| `inspect_settings` | 关键参数 + pending_restart(参数为何不生效) |
| `inspect_replication_slots` | 复制槽泄漏检测(撑爆磁盘的经典原因) |
| `inspect_pg_stat_replication` | 标准 PG 流复制延迟(非 Aurora) |
| `inspect_replica_lag` | Aurora 集群复制延迟(Aurora 专用,连 writer) |
| `inspect_database_sizes` | 全 cluster 各库大小 + 总容量 |
| `inspect_current_queries` | **实时查询快照(不依赖 pg_stat_statements,现在数据库在跑什么)** |
| `inspect_checkpoint_stats` | **checkpoint/bgwriter 压力(周期性性能抖动根因)** |
| `inspect_temp_file_usage` | **临时文件落盘(work_mem 不足,排序/hash 慢)** |

### database 级(⚠ 必须传 `database=业务库名`,默认 postgres 只看 postgres 库)
| 工具 | 用途 |
|---|---|
| `inspect_table_bloat` | 表膨胀(autovacuum 跟不上) |
| `inspect_index_usage` | 索引使用率(冗余索引 / 缺索引) |
| `inspect_index_bloat` | 索引膨胀(table_bloat 的盲区) |
| `inspect_table_sizes` | 表 / 索引大小 top N |
| `inspect_cache_hit_ratio` | shared_buffer 命中率 |
| `inspect_autovacuum_tuning` | autovacuum 配置健康度(膨胀根因) |
| `inspect_extensions` | 已装 / 可装扩展 |
| `inspect_schema_objects` | **列出库里有哪些表(表名/类型/行数/大小)— 排障发现入口,不知道库里有什么时先用它** |
| `inspect_table_io_stats` | **表/索引级磁盘 I/O 热点(谁在猛读盘)** |
| `inspect_table_stats_freshness` | **统计信息过期检测(查询计划走偏的根因)** |

### 自动多库(自动扫全 cluster,无需传 `database`)
| 工具 | 用途 |
|---|---|
| `inspect_sequence_capacity` | sequence 耗尽风险(int4 列爆 21 亿业务全停) |
| `inspect_xid_wraparound` | 事务 ID wraparound 风险(进只读保护) |
| `inspect_vacuum_progress` | 正在跑的 VACUUM / ANALYZE 进度 |
| `inspect_logical_replication` | publication / subscription 状态 |

---

## 注意事项

### 1. `database` 参数怎么传(最常见的坑)
- **database 级工具**默认 `database="postgres"`,但**业务表通常不在 postgres 库**。
  排查业务库的表膨胀/索引,**必须传 `database=业务库名`**(如 `jupiter`),否则查的是空的 postgres 库,会误以为"健康"。
- 实例级 / 自动多库工具不用管这个。

### 2. RDS 内部库 `rdsadmin` 无法访问 —— 正常现象
RDS 托管的 `rdsadmin` 库任何用户(含 master)都无权连接,这是 AWS 的硬限制。
`inspect_database_sizes` 和多库扫描工具会**自动跳过**它(标注 `skipped_no_connect`),不是错误。

### 3. Aurora vs 标准 PG 的复制延迟
- **Aurora** 用 `inspect_replica_lag`(走 `aurora_replica_status()`,**只能在 writer endpoint 上调**,reader 上会失败)
- **标准 RDS PG / 自建** 用 `inspect_pg_stat_replication`(需 `rds_replication` 权限)

### 4. `pg_stat_statements` 没装
`inspect_top_queries` 依赖这个扩展。没装时工具不会报错,会返回 warning 提示:
在 RDS parameter group 的 `shared_preload_libraries` 加 `pg_stat_statements` → 重启 → `CREATE EXTENSION pg_stat_statements;`

### 5. 多库扫描的数量上限
`inspect_sequence_capacity` / `inspect_vacuum_progress` / `inspect_logical_replication`
默认最多扫 **20 个库**(`max_databases` 参数可调)。cluster 库数超 20 时会截断 + 提示,
避免一次扫几十个库串行建连接导致超时。

### 6. 密码轮换
轮换 `mcp_devops_ro` 密码:先 `ALTER USER ... WITH PASSWORD` 改 PG,再 `put-secret-value` 同步 Secret。
容器不用重启,最长 5 分钟(Secret TTL 缓存)后自动用新密码。

### 7. 跨 region
容器跑在 us-east-1,数据库可在任意 region(经 TGW)。要确保:
- 数据库安全组放行 Runtime VPC CIDR 的 5432
- Runtime VPC ↔ 数据库 VPC 的 TGW 路由双向通
- Secret 在 us-east-1(Runtime 所在 region)

---

## 容器环境变量(由 target stack 注入,不用手动配)

| 变量 | 说明 |
|---|---|
| `DB_SECRET_NAME` | Secret 名,如 `aws-devops-mcp/rds-postgres/devops-readonly` |
| `DB_PORT` | 默认 5432 |
| `AWS_REGION` | Secret 所在 region(us-east-1) |
| `LOG_LEVEL` | 可选,默认 INFO |
| ~~endpoint~~ | **不注入** —— endpoint 是 tool 调用必传参数 |

## 返回格式

所有工具返回统一结构(conventions A8):
```json
{
  "status": "ok | warning | critical",
  "findings": [ {"severity": "...", "metric": "...", ...} ],
  "raw_data": { "...原始数据供 LLM 进一步推理..." },
  "recommendation": "机器可读 + 人类可读的建议"
}
```
