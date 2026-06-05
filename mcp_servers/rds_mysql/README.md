# RDS / Aurora MySQL 巡检 MCP Server

一个通用的 RDS MySQL / Aurora MySQL **只读巡检** MCP server,通过 18 个语义化工具,
让 DevOps Agent 能安全地排查线上 MySQL 问题(慢查询、锁等待、元数据锁、InnoDB buffer
pool、连接耗尽、临时表落盘、auto_increment 耗尽、复制延迟等)。

> 它是「MySQL 巡检能力」,不是「某个实例的巡检器」—— 不绑定任何数据库实例。
> 每个工具的 `cluster_endpoint` 是**调用时必传**的,由 Agent 按用户上下文给出。
> 一套 server 天然巡检 N 个同类 MySQL 实例。

**适用引擎**:RDS MySQL 8.0 / Aurora MySQL 3(8.0 兼容)。主要面向 **MySQL 8.0**
(锁等待用 8.0 的 `performance_schema.data_lock_waits`);5.7 上锁相关工具会优雅降级。

## 安全模型(先看这个)

| 设计 | 说明 |
|---|---|
| **只读账号** | 容器用专门的只读用户 `mcp_devops_ro` 连库,**绝不用 admin / master** |
| **SQL 全固化** | 18 个工具的 SQL 都写死在代码里,只查 `information_schema` / `performance_schema` / `sys` 等**元数据/统计视图**,**没有任何"传 SQL 进来执行"的口子** |
| **看不到业务数据** | 工具能看到"表多大、慢查询模板、锁等待、I/O 热点",但**永远看不到表里的行数据**(没有 `SELECT * FROM 业务表`)。慢查询用归一化 digest 模板,不含具体参数值 |
| **不卡业务库** | 每个连接强制 `max_execution_time=15s`,巡检 SQL 超时自动 abort |
| **endpoint 不注入** | 容器不预设任何实例地址,连哪个库由 Agent 调用时传入 |
| **query 文本防注入** | DB 返回的 SQL 文本截断 200 字符 + 标注 `[FROM_DB | UNTRUSTED]`,防 prompt injection |
| **凭据走 Secrets Manager** | 密码不进代码、不进镜像、不进日志,容器运行时按需拉取(5 分钟 TTL 缓存) |

---

## ⭐ 只读用户 `mcp_devops_ro` 权限配置(最重要)

巡检容器用这个只读用户连库。**框架不会自动创建它**——需要 DBA 用 admin 凭据手工创建一次。
所有同类型 MySQL 实例**共用同一套** `mcp_devops_ro` 凭据(单 secret 多库策略)。

### 第 1 步:创建只读用户(DBA 用 admin/master 连 writer 实例)

```sql
-- 1) 建用户(密码用强随机,32+ 字符;先用占位,第 3 步会重设并灌进 Secret)
--    RDS 强制 TLS 时加 REQUIRE SSL
CREATE USER 'mcp_devops_ro'@'%' IDENTIFIED BY 'CHANGE_ME_临时占位' REQUIRE SSL;

-- 2) 授予最小巡检权限(server 级,因为巡检视图是全局的)
--    SELECT            → information_schema / performance_schema / sys / 表元数据
--    PROCESS           → information_schema.PROCESSLIST/INNODB_TRX 看到所有线程(不止自己的)
--    REPLICATION CLIENT → SHOW REPLICA STATUS(复制延迟)
GRANT SELECT, PROCESS, REPLICATION CLIENT ON *.* TO 'mcp_devops_ro'@'%';

FLUSH PRIVILEGES;
```

> **为什么是 server 级 `*.*`**:`PROCESS` 是 MySQL server-level 权限(无法限定到某个库),
> 是 `information_schema.PROCESSLIST` / `INNODB_TRX` 能看到**所有**会话的硬要求(否则只能看到自己的)。
> `SELECT ON *.*` 让巡检能读 `performance_schema` / `sys` / `information_schema` 的统计视图。
> 这套权限**只能读,不能写任何业务数据**(无 INSERT/UPDATE/DELETE/DDL)。

### 第 2 步:确认 performance_schema 已开启

大部分工具依赖 `performance_schema`(慢查询 digest、锁等待、元数据锁、表 I/O、未用索引)。
RDS MySQL 8.0 默认开启。确认:

```sql
SHOW VARIABLES LIKE 'performance_schema';   -- 应为 ON
```

若为 OFF:在 RDS parameter group 设 `performance_schema=1` → 重启实例。
关闭时,依赖它的工具会返回 `warning` + 提示,不会报错崩溃。

### 第 3 步:设密码 + 灌进 Secrets Manager

```sql
ALTER USER 'mcp_devops_ro'@'%' IDENTIFIED BY '<32+ 字符强随机密码>';
```

```bash
# 灌进框架建的空壳 Secret(部署时 CDK 已建好空壳,密码=PLACEHOLDER_REPLACE_ME)
aws secretsmanager put-secret-value \
  --secret-id aws-devops-mcp/rds-mysql/devops-readonly \
  --region us-east-1 \
  --secret-string '{"username":"mcp_devops_ro","password":"<同上那个密码>"}'
```

> Secret 名固定 `aws-devops-mcp/rds-mysql/devops-readonly`,在 **Runtime 所在 region(us-east-1)**,
> 不是数据库所在 region。容器跨 region 拉这个 Secret。

### 各工具权限要求速查

| 权限 / 前提 | 覆盖的工具 |
|---|---|
| `SELECT` + `PROCESS`(必备) | processlist / long_transactions / connections / active_clients / current_queries |
| `SELECT` on performance_schema | lock_waits / metadata_locks / slow_queries / table_io / index_usage / buffer_pool / temp_tables / global_status |
| `SELECT` on information_schema | table_sizes / schema_objects / auto_increment / variables |
| `REPLICATION CLIENT` | replica_status(SHOW REPLICA STATUS) |
| performance_schema=ON | 上述所有 performance_schema 类工具(关闭则优雅降级 warning) |

---

## 工具清单(18 个)

### 事务 / 锁 / 连接
| 工具 | 用途 |
|---|---|
| `inspect_processlist` | 活跃线程快照(不依赖扩展,排障第一步) |
| `inspect_long_transactions` | 长事务(INNODB_TRX,未提交事务导致锁堆积/undo 膨胀) |
| `inspect_lock_waits` | 行锁等待阻塞链(8.0 data_lock_waits,谁等谁) |
| `inspect_metadata_locks` | 元数据锁(DDL 被 DML 阻塞,"加字段卡住") |
| `inspect_connections` | 连接数 / max_connections 占比 / 中止连接 |
| `inspect_active_clients` | 按 user+host 聚合连接(连接池泄漏定位) |

### 性能
| 工具 | 用途 |
|---|---|
| `inspect_buffer_pool` | InnoDB buffer pool 命中率 / 脏页 / 等待 free page |
| `inspect_slow_queries` | 慢查询 top N(digest 归一化模板) |
| `inspect_current_queries` | 实时慢查询(PROCESSLIST,不依赖 performance_schema) |
| `inspect_table_io` | 表级 I/O 热点(谁读写等待最多) |
| `inspect_temp_tables` | 内部临时表落盘(sort/join 超 tmp_table_size) |
| `inspect_global_status` | 关键 GLOBAL STATUS(QPS / 慢查询 / 全表扫描 / 锁) |

### 容量 / 配置 / 对象
| 工具 | 用途 |
|---|---|
| `inspect_table_sizes` | 表/索引大小 top N(可选 database 过滤) |
| `inspect_schema_objects` | 列出库里有哪些表(发现入口) |
| `inspect_index_usage` | 未使用索引(冗余索引候选) |
| `inspect_auto_increment` | auto_increment 容量耗尽风险(int 爆 21 亿) |
| `inspect_variables` | 关键 GLOBAL VARIABLES(参数配置审阅) |

### 复制
| 工具 | 用途 |
|---|---|
| `inspect_replica_status` | 复制延迟 + 链路(RDS replica / Aurora reader 自动适配) |

---

## 注意事项

### 1. MySQL 8.0 vs 5.7
- `inspect_lock_waits` 用 8.0 的 `performance_schema.data_lock_waits`,5.7 上会返回 warning
  提示用 `information_schema.INNODB_LOCK_WAITS`。
- `inspect_replica_status` 优先 `SHOW REPLICA STATUS`(8.0.22+),回退 `SHOW SLAVE STATUS`(5.7)。

### 2. Aurora vs RDS MySQL 复制
- **Aurora** 用 `information_schema.REPLICA_HOST_STATUS`(各 reader 的 `REPLICA_LAG_IN_MILLISECONDS`)
- **RDS MySQL read replica** 用 `SHOW REPLICA STATUS`(`Seconds_Behind_Source`)
- 工具自动探测引擎类型适配,连 reader endpoint 看延迟更准。

### 3. performance_schema 关闭时
慢查询 / 锁等待 / 元数据锁 / 表 I/O / 未用索引这几个工具依赖 performance_schema。
关闭时它们返回 `warning` + 开启指引,不会崩溃。`inspect_processlist` /
`inspect_current_queries` / `inspect_long_transactions` 走 information_schema,不受影响。

### 4. 未使用索引的误报
`inspect_index_usage` 基于"自上次实例重启以来"的累计统计。实例**刚重启**时统计未积累,
会把正常索引误报为未使用。确认实例稳定运行一段时间(覆盖完整业务周期,如月度报表)后再据此删索引。

### 5. 密码轮换
先 `ALTER USER ... IDENTIFIED BY` 改 MySQL,再 `put-secret-value` 同步 Secret。
容器不用重启,最长 5 分钟(Secret TTL 缓存)后自动用新密码。

### 6. 跨 region
容器跑在 us-east-1,数据库可在任意 region(经 TGW)。要确保:
- 数据库安全组放行 Runtime VPC CIDR 的 3306
- Runtime VPC ↔ 数据库 VPC 的 TGW 路由双向通
- Secret 在 us-east-1(Runtime 所在 region)

---

## 容器环境变量(由 target stack 注入,不用手动配)

| 变量 | 说明 |
|---|---|
| `DB_SECRET_NAME` | Secret 名,如 `aws-devops-mcp/rds-mysql/devops-readonly` |
| `DB_PORT` | 默认 3306 |
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
