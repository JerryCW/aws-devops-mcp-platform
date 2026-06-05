# PG 巡检 MCP — 工具候选与决策记录

> 这不是"待办清单",是**决策日志**:记录已经评估过、但有意暂不做的工具候选。
> 目的:将来真遇到触发条件时不用重新分析;避免重复讨论"要不要加 X"。
> 新增工具的判断原则(沿用项目 steering):**真实 case 驱动,不为想象需求提前做**。

当前已实现:**28 个工具**(见 README.md / main.py 顶部清单)。

---

## 已评估 · 有意暂缓(等触发条件出现再做)

### 1. `inspect_query_plan` — 自动抓执行计划
- **价值**:`inspect_top_queries` / `inspect_current_queries` 给的是 SQL 文本,但没有执行计划。
  排查慢查询时,EXPLAIN 才能看出"走没走索引、join 方式、行数估算偏差"。
- **触发条件**:Agent 频繁需要"为什么这条 SQL 慢"的根因,光看 SQL 文本不够时。
- **⚠ 必须想清的坑**:
  - 只能用纯 `EXPLAIN`(看计划),**禁止 `EXPLAIN ANALYZE`** —— ANALYZE 会**真执行 SQL**,
    在生产库上对一条慢查询跑 ANALYZE 可能加重负担、甚至触发副作用(若是 DML)。
  - 需要 SQL 文本作为**输入参数** —— 这部分打破了"SQL 全固化、无外部 SQL 入口"的安全模型(A3 / SHALL NOT #3)。
    要做必须:① 只接 SELECT(解析校验)② 强制只 EXPLAIN 不 ANALYZE ③ 审计日志。
  - 结论:有价值但安全面敏感,**做之前要单独设计 + 评审**,不能随手加。

### 2. `inspect_query_stats_delta` — pg_stat_statements 近 N 分钟增量
- **价值**:`inspect_top_queries` 是**累计值**(自上次 reset),看不出"最近 N 分钟在恶化的 SQL"。
  增量视图能定位"刚刚开始变慢的查询"。
- **触发条件**:累计值不够用、需要"最近时间窗"的趋势时。
- **⚠ 坑**:要在容器内存两次快照做差值 —— 引入**有状态**,跟当前"无状态、每次 tool call 独立"
  的设计冲突。要么容器内缓存上次快照(进程级状态,Runtime 重启丢失),要么调用方传基线。需想清。

### 3. `inspect_deadlock_history` — 死锁历史
- **价值**:`inspect_blocking_chains` 看的是**实时**锁等待;死锁发生后想查历史只能看日志。
- **触发条件**:客户频繁死锁、且需要历史复盘时。
- **⚠ 边界**:死锁记录在 PG 错误日志(`log_lock_waits` / deadlock 日志),要解析 RDS 日志文件。
  这跨进了**控制面/日志领域** —— `download-db-log-file-portion` 是 DevOps Agent **自带能力**,
  不该由本 SQL 层 MCP 重复造。优先让 Agent 用自带的日志能力,除非有强需求。

### 4. 连接池诊断(PgBouncer / RDS Proxy)
- **价值**:连接池层的指标(pool 使用率、等待、复用)现有工具看不到。
- **触发条件**:**客户确实部署了连接池**(RDS Proxy 或自建 PgBouncer)。条件性需求 —— 没用就 0 价值。
- **⚠ 边界**:RDS Proxy 指标在 CloudWatch(Agent 自带能力);PgBouncer 要连它的 admin 库
  (`SHOW POOLS` 等,不是普通 PG 协议)。需确认客户用的是哪种再设计。

### 5. FDW / 外部表健康
- **价值**:用了 postgres_fdw / 外部表时,外部连接健康、外部表可达性现有工具不覆盖。
- **触发条件**:**客户确实用了 FDW / 外部表**。条件性需求,大多数客户不用。

---

## 已评估 · 决定不做

### A. pgstattuple 精确碎片度量
- **为什么不做**:`inspect_table_bloat` / `inspect_index_bloat` 已用**统计估算**(±20% 误差)识别明显膨胀。
  pgstattuple 给精确值,但要**全扫表/索引**,违反"不卡业务库"原则(P1)。
- **结论**:估算版足够"发现问题";真要精确值,REINDEX 前让 DBA 手工用 pgstattuple 确认即可。不进 MCP。

---

## 容量提醒(做新工具前必看)

- PG 当前 **28 个工具**。Gateway tools/list 单页上限 **30**(SHALL NOT #25)。
- 再加 2 个就到 30;**接 Valkey(11 个)后总数必破 30**,届时 Valkey 几乎肯定要走**第二个 Gateway**。
- 给 PG 加新工具前先想:是否值得占用所剩不多的单页配额?还是该规划多 Gateway。

## 维护规则

- 某项**触发条件满足**(如客户上了 RDS Proxy)→ 从"暂缓"提到实现,实现后从本文件移除并更新 README 计数。
- 新识别的候选 → 加到"已评估·暂缓",写明触发条件 + 风险,不要只写名字。
- 决定永久不做的 → 移到"决定不做"并写原因。
