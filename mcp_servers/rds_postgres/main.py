"""RDS PostgreSQL 巡检 MCP server(28 个语义化巡检 tool,IAM 形态)。

关键设计:**endpoint 不注入容器**。本 server 是通用 PG 巡检工具,不绑定任何
实例。每个 tool 的 `cluster_endpoint` 是**必传参数**,由 DevOps Agent 在调用时按
用户上下文给出(如"巡检 cluster-A 的长事务" → cluster_endpoint=cluster-A...)。
这样一套 server 天然巡检 N 个同类 PG 实例。

暴露的 tool 全集(每个对应一个 DBA 真实场景):

【核心 10 个】
  inspect_long_transactions    长事务(原 demo)
  inspect_top_queries          慢查询 top N(pg_stat_statements)
  inspect_index_usage          索引使用率(冗余 / 缺失候选)
  inspect_table_bloat          表膨胀(autovacuum 滞后)
  inspect_replica_lag          Aurora replica 复制延迟(aurora_replica_status())
  inspect_connections          连接分布 / 连接 leak 候选
  inspect_blocking_chains      锁等待阻塞链
  inspect_table_sizes          表 / 索引大小 top N
  inspect_active_clients       活跃 client 分布(usename / IP / app)
  inspect_sequence_capacity    Sequence 容量(int4 列耗尽风险,Alan case 同款)

【扩展 10 个,覆盖 rds-postgresql-troubleshooting skill 的数据面缺口】
  inspect_xid_wraparound       Transaction ID wraparound 风险(B1 关键)
  inspect_vacuum_progress      正在跑的 VACUUM/ANALYZE 进度(B1)
  inspect_settings             pg_settings 配置 + pending_restart(D1 / B2 / B3 / G1 通用)
  inspect_replication_slots    replication slot 状态 + slot leak 检测(E2 关键)
  inspect_logical_replication  publication / subscription 状态(E2)
  inspect_pg_stat_replication  PG 标准流复制状态(E1,通用 PG 非 Aurora-only)
  inspect_extensions           已装 / 可装 extensions + shared_preload_libraries(G1)
  inspect_wait_events          pg_stat_activity wait_event 分布 top N(Z1 / B3)
  inspect_cache_hit_ratio      shared_buffer 命中率 + per-table 命中率最低(B3)
  inspect_database_sizes       全 cluster 各 db 大小(F1 容量规划)

【bloat 排查闭环补充 2 个】
  inspect_index_bloat          索引膨胀估算(table_bloat 的盲区:索引页稀疏)
  inspect_autovacuum_tuning    autovacuum 配置健康度(为什么表会膨胀的根因层)

【发现入口 1 个】
  inspect_schema_objects       列出库里有哪些表(表名/类型/行数/大小),排障的起点

【性能深挖补充 5 个(基于 AWS 内部 skill 实战)】
  inspect_current_queries      实时查询快照(不依赖 pg_stat_statements)
  inspect_table_io_stats       表/索引级磁盘 I/O 热点
  inspect_checkpoint_stats     checkpoint/bgwriter 压力(性能抖动根因,PG17 兼容)
  inspect_table_stats_freshness 统计信息过期(查询计划走偏)
  inspect_temp_file_usage      临时文件落盘(work_mem 不足)

PG=28。多 db 扫描类工具(sequence_capacity / vacuum_progress /
logical_replication)有 max_databases 护栏,默认上限 20 个 db。

DevOps Agent 端 MCP client 暂未实现 nextCursor 分页,客户在 tool 选择
界面**不要勾选 valkey___inspect_stats**(它仍可在 Agent 注册期通过 raw call 访问,
但 UI 列表不显示)。详见 docs/development-trace.md。

容器外部注入(由 target stack 在 Runtime 上设环境变量):
  DB_PORT                   默认 5432
  DB_SECRET_NAME            Secrets Manager 路径(mcp_devops_ro 凭据)
  AWS_REGION                Secret 所在 region
  LOG_LEVEL                 可选,默认 INFO
  注意:**不注入 endpoint** — endpoint 是 tool 调用必传参数(见上)

设计契约(全集):
  - FastMCP host=0.0.0.0 port=8000 stateless_http=True(SHALL NOT #6 / #17)
  - **Secret 5 分钟 TTL 缓存**(P7,密码轮换最长 5 分钟同步)
  - **PG 连接强制 statement_timeout=15s + lock_timeout=5s**(P1,巡检不卡业务 + LLM 不超时)
  - 所有 tool SQL 固化,`%s` 参数化绑定 / `pgsql.Identifier()` 包列名(SHALL NOT #3)
  - **DB 返回的 query 文本走 `_redact_query()`**:截断 200 字符 + 标注 [FROM_DB | UNTRUSTED]
    防止 prompt injection 攻击面 + PII 外漏(S1 / S2)
  - 返回结构严格 conventions A8(status / findings / raw_data / recommendation)
  - extension / aurora-only 函数缺失时优雅降级(返回 status='warning' + 提示)
  - 严禁打印 password 内容 / 完整 query 字符串到日志(SHALL NOT #12)
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from threading import Lock
from typing import Any

import boto3
import psycopg
from mcp.server.fastmcp import FastMCP
from psycopg import sql as pgsql
from psycopg.rows import dict_row
from starlette.requests import Request
from starlette.responses import PlainTextResponse

# ---------------------------------------------------------------------------
# 模块级常量
# ---------------------------------------------------------------------------
_DEFAULT_THRESHOLD_SECONDS: int = 60
_CRITICAL_MULTIPLIER: int = 5
_QUERY_LIMIT: int = 100
_TOP_N_DEFAULT: int = 20
_CONNECT_TIMEOUT_SECONDS: int = 15
_PLACEHOLDER_PASSWORD: str = "PLACEHOLDER_REPLACE_ME"

# 巡检 SQL session 级超时(P1):任何巡检 SQL ≥ 15s 自动 abort,不卡业务
_STATEMENT_TIMEOUT_MS: int = 15000
_LOCK_TIMEOUT_MS: int = 5000

# Secret 缓存 TTL(P7)— 5 分钟,balance 轮换响应延迟 vs SecretsManager API 调用频率
_SECRET_TTL_SECONDS: int = 300

# query 文本防注入截断(S1 / S2)
_QUERY_TRUNCATE_LEN: int = 200
_QUERY_REDACT_PREFIX: str = "[FROM_DB | UNTRUSTED]: "

# 多 db 扫描类工具的 db 数量上限(P2 性能护栏):cluster 的 db 数超过此值时,
# 拒绝"扫全部"(每 db 一条连接,几十个 db 会串行建几十次连接 → client 超时),
# 要求调用方传具体 database。客户可在调用时传 max_databases 覆盖。
_MAX_SCAN_DATABASES_DEFAULT: int = 20

# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
_LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=_LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("rds_postgres_inspect")

# ---------------------------------------------------------------------------
# 容器启动时一次性读取的环境变量
# ---------------------------------------------------------------------------
_AWS_REGION: str = os.environ.get("AWS_REGION", "us-east-1")
# Secret 名由 Runtime 注入 DB_SECRET_NAME;缺失则 fail-fast(不兜底到某个固定名,
# 避免本地调试 / env 丢失时去找一个不存在的 secret 报误导性错误)。
_DB_SECRET_NAME: str = os.environ.get("DB_SECRET_NAME", "")
_DEFAULT_DB_PORT: int = int(os.environ.get("DB_PORT", "5432"))

# ---------------------------------------------------------------------------
# FastMCP 实例(MCP 协议契约 port=8000 / mount=/mcp)
# ---------------------------------------------------------------------------
mcp = FastMCP(
    host="0.0.0.0",  # noqa: S104 — 容器内监听全 0,由 Runtime 边界控制访问
    port=8000,
    stateless_http=True,
)


@mcp.custom_route("/ping", methods=["GET"])
async def ping(_request: Request) -> PlainTextResponse:
    """AgentCore Runtime health check endpoint."""
    return PlainTextResponse("ok")


# ---------------------------------------------------------------------------
# 共用 helpers
# ---------------------------------------------------------------------------
# Secret TTL 缓存(P7):线程安全,避免同一进程多次 boto3 跨 region 调用。
# Secret 失窃 / 密码轮换后,**最长 _SECRET_TTL_SECONDS 秒**自动同步;客户也可
# 在轮换后立即在 Secrets Manager 控制台等 5 分钟,或重启 Runtime 强制刷新。
_secret_cache: dict[str, tuple[float, tuple[str, str]]] = {}
_secret_cache_lock = Lock()


def _fetch_db_credentials() -> tuple[str, str]:
    """从 Secrets Manager 拉 mcp_devops_ro 凭据,带 5 分钟 TTL 缓存。

    线程安全:多个 MCP 工具并发调用时只触发一次 SecretsManager API。
    """
    now = time.monotonic()
    with _secret_cache_lock:
        if not _DB_SECRET_NAME:
            raise RuntimeError(
                "环境变量 DB_SECRET_NAME 未设置 —— Runtime 应注入该值;"
                "本地调试时请显式 export DB_SECRET_NAME=<secret 名>"
            )
        cached = _secret_cache.get(_DB_SECRET_NAME)
        if cached is not None:
            cached_at, value = cached
            if now - cached_at < _SECRET_TTL_SECONDS:
                return value
        # 缓存过期 / 不存在 — 拉取(锁内拉防止 thundering herd)
        sm = boto3.client("secretsmanager", region_name=_AWS_REGION)
        raw = sm.get_secret_value(SecretId=_DB_SECRET_NAME)["SecretString"]
        payload = json.loads(raw)
        user = payload["username"]
        pwd = payload["password"]
        if pwd == _PLACEHOLDER_PASSWORD:
            raise RuntimeError(
                f"Secret {_DB_SECRET_NAME!r} 中 password 仍是 PLACEHOLDER,"
                "请按 docs/customer-setup/01-postgres-user.md 灌真密码"
            )
        _secret_cache[_DB_SECRET_NAME] = (now, (user, pwd))
        return user, pwd


def _resolve_endpoint(cluster_endpoint: str) -> str:
    """校验 cluster_endpoint 非空。

    MCP server 是通用 PG 巡检工具,不绑定任何实例。endpoint 必须由
    调用方(DevOps Agent)在 tool 调用时传入,容器内不注入任何默认 endpoint。
    """
    endpoint = (cluster_endpoint or "").strip()
    if not endpoint:
        raise ValueError(
            "cluster_endpoint 必传:请传入要巡检的 PG cluster endpoint"
            "(如 my-cluster.cluster-xxxx.us-east-1.rds.amazonaws.com)。"
            "本工具是通用巡检工具,不绑定特定实例。"
        )
    return endpoint


def _connect(endpoint: str, database: str) -> psycopg.Connection:
    """建 PG 连接(P1 强制 statement_timeout / lock_timeout)。

    错误信息只带 host/db,绝不带 user/pwd(SHALL NOT #15)。
    用 psycopg3 关键字参数(不拼连接串)— libpq 自己处理转义,密码 / endpoint
    含空格 / 单引号 / 反斜线等特殊字符也安全;options 用 -c 传 session 级 GUC,
    即使全局 parameter group 没设也生效,巡检 SQL 超时自动 abort 不卡业务。
    """
    user, pwd = _fetch_db_credentials()
    # libpq options 用 -c 传 session-level GUC;空格分隔
    pg_options = (
        f"-c statement_timeout={_STATEMENT_TIMEOUT_MS} "
        f"-c lock_timeout={_LOCK_TIMEOUT_MS} "
        # idle_in_transaction_session_timeout:本 session 自身 idle 时被 PG 杀
        # (5 分钟,防止巡检 session 异常 idle 卡死表锁)
        f"-c idle_in_transaction_session_timeout=300000"
    )
    return psycopg.connect(
        host=endpoint,
        port=_DEFAULT_DB_PORT,
        dbname=database,
        user=user,
        password=pwd,
        connect_timeout=_CONNECT_TIMEOUT_SECONDS,
        sslmode="require",
        options=pg_options,
        # Application name 给 DBA 在 pg_stat_activity 一眼能看到是谁
        application_name="mcp_devops_inspect",
        row_factory=dict_row,
    )


def _redact_query(query: str | None, max_len: int = _QUERY_TRUNCATE_LEN) -> str | None:
    """截断 + 标注 SQL 文本,防止 prompt injection / PII 外漏(S1 / S2)。

    规则:
      - None / 空 → None
      - 截到 max_len 字符
      - 加 `[FROM_DB | UNTRUSTED]: ` 前缀,告诉 LLM 这是数据不是指令
      - 替换 prompt 注入常见 token(<|im_start|> / ignore previous instructions / etc.)
        — 简单做法:把控制字符 + 反引号 + 反斜线 + < > 替成空格

    注:不做正则参数值脱敏(密码 / 邮箱 / 卡号),那会改变 SQL 语义影响 DBA 排错;
    只截断 + 标注,让 LLM 知道"这是来自不可信数据源的字符串"。
    """
    if not query:
        return None
    truncated = query[:max_len].rstrip()
    if len(query) > max_len:
        truncated += "...[truncated]"
    # 简单 sanitize:把控制字符 / 反引号 / `<|`、`|>` 这种 prompt 标记替成空格,
    # 不让 LLM 把 SQL 内容当成 system 指令解析
    sanitized = truncated.replace("\x00", " ").replace("\r", " ").replace("\n", " ")
    sanitized = sanitized.replace("<|", "<<").replace("|>", ">>")
    return _QUERY_REDACT_PREFIX + sanitized


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _wrap(
    *,
    status: str,
    findings: list[dict[str, Any]],
    raw_data: dict[str, Any],
    recommendation: str,
) -> dict[str, Any]:
    """conventions A8 包装(强制 4 key + status 枚举校验)。"""
    assert status in ("ok", "warning", "critical"), f"invalid status: {status!r}"
    return {
        "status": status,
        "findings": list(findings),
        "raw_data": dict(raw_data),
        "recommendation": str(recommendation),
    }


def _check_extension(cur, ext_name: str) -> bool:
    """检查 pg extension 是否安装(用于 graceful 降级)。"""
    cur.execute(
        "SELECT 1 FROM pg_extension WHERE extname = %s",
        (ext_name,),
    )
    return cur.fetchone() is not None


def _resolve_scan_db_list(
    endpoint: str,
    *,
    database: str,
    scan_all_databases: bool,
    max_databases: int,
) -> tuple[list[str], str | None]:
    """决定多 db 扫描类工具要扫哪些 db,带 P2 性能护栏。

    返回 (db_list, cap_note):
      - 传了 database 或 scan_all_databases=False → 只扫这一个,cap_note=None
      - scan_all_databases=True → 连 postgres 拿全 cluster 非模板 db 列表;
        若数量 > max_databases,**只取前 max_databases 个**(按 datname 排序),
        cap_note 返回提示语(让调用方知道被截断、该传具体 database)。
    """
    if not (scan_all_databases and not database):
        return [database or "postgres"], None

    with _connect(endpoint, "postgres") as conn, conn.cursor() as cur:
        # 只列"当前用户有 CONNECT 权限"的库:RDS 内部库(rdsadmin)普通用户连不上,
        # 排除掉避免每次扫描都产生一条无意义的连接失败(rdsadmin 的 datallowconn=true
        # 但 has_database_privilege CONNECT=false)。
        cur.execute(
            "SELECT datname FROM pg_database "
            "WHERE datistemplate = false AND datallowconn = true "
            "  AND has_database_privilege(current_user, datname, 'CONNECT') "
            "ORDER BY datname"
        )
        all_dbs = [r["datname"] for r in cur.fetchall()]

    if len(all_dbs) > max_databases:
        capped = all_dbs[:max_databases]
        note = (
            f"⚠ cluster 有 {len(all_dbs)} 个 db,超过 max_databases={max_databases} 上限,"
            f"本次只扫了前 {max_databases} 个(按字母序):{capped}。"
            "若要扫其它 db,请传具体 database= 参数,或调高 max_databases。"
        )
        return capped, note
    return all_dbs, None


# ===========================================================================
# Tool 1 — 长事务(原 demo,保留)
# ===========================================================================
@mcp.tool()
def inspect_long_transactions(
    cluster_endpoint: str,
    threshold_seconds: int = _DEFAULT_THRESHOLD_SECONDS,
    database: str = "postgres",
) -> dict[str, Any]:
    """巡检长事务 / 卡住的事务(active 或 idle in transaction 超阈值)— 排查"事务一直不提交"导致的锁堆积、膨胀。实例级:看到所有库的事务,database 参数不影响结果。"""
    endpoint = _resolve_endpoint(cluster_endpoint)
    if threshold_seconds < 1:
        raise ValueError(f"threshold_seconds 必须 >= 1,收到 {threshold_seconds}")

    log.info("[long_transactions] %s db=%s threshold=%ds", endpoint, database, threshold_seconds)
    with _connect(endpoint, database) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT pid, usename, datname, application_name,
                   client_addr::text AS client_addr, state,
                   xact_start, backend_start, wait_event_type, wait_event, query,
                   EXTRACT(EPOCH FROM (NOW() - xact_start))::int AS duration_seconds
            FROM pg_stat_activity
            WHERE backend_type = 'client backend'
              AND xact_start IS NOT NULL
              AND state IN ('active', 'idle in transaction', 'idle in transaction (aborted)')
              AND EXTRACT(EPOCH FROM (NOW() - xact_start)) >= %s
            ORDER BY xact_start ASC
            LIMIT %s
            """,
            (threshold_seconds, _QUERY_LIMIT),
        )
        rows = cur.fetchall()

    transactions: list[dict[str, Any]] = []
    for r in rows:
        transactions.append(
            {
                "pid": int(r["pid"]),
                "usename": str(r.get("usename") or ""),
                "datname": str(r.get("datname") or ""),
                "application_name": str(r.get("application_name") or ""),
                "client_addr": str(r["client_addr"]) if r.get("client_addr") else None,
                "state": str(r.get("state") or ""),
                "xact_start": r["xact_start"].isoformat() if r.get("xact_start") else None,
                "backend_start": r["backend_start"].isoformat() if r.get("backend_start") else None,
                "wait_event_type": str(r.get("wait_event_type") or ""),
                "wait_event": str(r.get("wait_event") or ""),
                "query": _redact_query(r.get("query")),
                "duration_seconds": int(r["duration_seconds"]),
            }
        )

    if not transactions:
        status, recommendation = "ok", "无长事务,数据库事务健康。"
    else:
        max_dur = max(int(t["duration_seconds"]) for t in transactions)
        idle_count = sum(1 for t in transactions if "idle" in str(t.get("state", "")).lower())
        if max_dur >= threshold_seconds * _CRITICAL_MULTIPLIER:
            status = "critical"
            recommendation = (
                f"发现 {len(transactions)} 条长事务(idle in transaction = {idle_count}),"
                f"最长 {max_dur}s 达 critical 阈值。建议:1) 排查应用层连接泄漏 / 未提交事务;"
                "2) 必要时 pg_terminate_backend(pid)(需 admin 权限)。"
            )
        else:
            status = "warning"
            recommendation = (
                f"发现 {len(transactions)} 条长事务(idle in transaction = {idle_count}),"
                f"最长 {max_dur}s。建议排查应用层事务边界。"
            )

    findings = [
        {
            "severity": "critical" if int(t["duration_seconds"]) >= threshold_seconds * _CRITICAL_MULTIPLIER else "warning",
            "metric": "long_running_trx",
            "value": f"{t['duration_seconds']}s",
            "threshold": f"{threshold_seconds}s",
            "pid": t["pid"],
            "state": t["state"],
            "datname": t["datname"],
            "usename": t["usename"],
        }
        for t in transactions
    ]

    return _wrap(
        status=status,
        findings=findings,
        raw_data={
            "cluster_endpoint": endpoint, "database": database,
            "threshold_seconds": threshold_seconds, "queried_at": _now_iso(),
            "transactions": transactions,
        },
        recommendation=recommendation,
    )


# ===========================================================================
# Tool 2 — 慢查询 top N(pg_stat_statements)
# ===========================================================================
@mcp.tool()
def inspect_top_queries(
    cluster_endpoint: str,
    top_n: int = _TOP_N_DEFAULT,
    order_by: str = "total_exec_time",
    database: str = "postgres",
) -> dict[str, Any]:
    """巡检 top N 最慢 / 最耗资源的 SQL(需 pg_stat_statements 扩展)— 找性能问题的元凶 SQL。实例级:统计覆盖所有库,database 参数不影响结果。

    Args:
        top_n: 返回前 N 条(默认 20,上限 100)
        order_by: 排序键 ∈ {total_exec_time, mean_exec_time, calls, rows}
    """
    endpoint = _resolve_endpoint(cluster_endpoint)
    n = max(1, min(top_n, _QUERY_LIMIT))
    valid_order = {"total_exec_time", "mean_exec_time", "calls", "rows"}
    if order_by not in valid_order:
        raise ValueError(f"order_by 必须 ∈ {sorted(valid_order)},收到 {order_by!r}")

    log.info("[top_queries] %s top=%d order=%s", endpoint, n, order_by)
    with _connect(endpoint, database) as conn, conn.cursor() as cur:
        if not _check_extension(cur, "pg_stat_statements"):
            return _wrap(
                status="warning",
                findings=[],
                raw_data={
                    "cluster_endpoint": endpoint, "queried_at": _now_iso(),
                    "extension_check": "pg_stat_statements not installed",
                },
                recommendation=(
                    "pg_stat_statements extension 未安装。请 admin 在 RDS parameter group "
                    "`shared_preload_libraries` 加 pg_stat_statements 后重启实例,然后跑 "
                    "`CREATE EXTENSION pg_stat_statements;`"
                ),
            )

        # order_by 已白名单校验,用 sql.Identifier 避免注入
        cur.execute(
            pgsql.SQL("""
                SELECT queryid::text AS queryid,
                       calls,
                       total_exec_time::numeric(20,2) AS total_exec_time_ms,
                       mean_exec_time::numeric(20,2) AS mean_exec_time_ms,
                       max_exec_time::numeric(20,2) AS max_exec_time_ms,
                       rows,
                       (rows::numeric / NULLIF(calls, 0))::numeric(20,2) AS avg_rows,
                       LEFT(query, 250) AS query_snippet
                FROM pg_stat_statements
                ORDER BY {} DESC NULLS LAST
                LIMIT %s
            """).format(pgsql.Identifier(order_by)),
            (n,),
        )
        rows = cur.fetchall()

    queries = [
        {
            "queryid": r["queryid"], "calls": int(r["calls"]),
            "total_exec_time_ms": float(r["total_exec_time_ms"] or 0),
            "mean_exec_time_ms": float(r["mean_exec_time_ms"] or 0),
            "max_exec_time_ms": float(r["max_exec_time_ms"] or 0),
            "rows": int(r["rows"] or 0),
            "avg_rows": float(r["avg_rows"] or 0),
            "query_snippet": _redact_query(r["query_snippet"]),
        }
        for r in rows
    ]

    # status:无数据 ok;mean > 1s warning;mean > 5s critical
    if not queries:
        status, recommendation = "ok", "pg_stat_statements 无数据(可能刚 reset)。"
    else:
        max_mean = max(q["mean_exec_time_ms"] for q in queries)
        if max_mean >= 5000:
            status = "critical"
            recommendation = (
                f"top {n} 查询中 mean exec time 最高 {max_mean:.0f}ms ≥ 5s,严重慢。"
                "建议:1) EXPLAIN ANALYZE 看执行计划;2) 检查索引;3) 看是否需要拆 SQL。"
            )
        elif max_mean >= 1000:
            status = "warning"
            recommendation = f"top {n} 查询中 mean exec time 最高 {max_mean:.0f}ms ≥ 1s,关注趋势。"
        else:
            status = "ok"
            recommendation = f"top {n} 查询性能正常,最慢 mean {max_mean:.0f}ms。"

    findings = [
        {
            "severity": "critical" if q["mean_exec_time_ms"] >= 5000
                        else "warning" if q["mean_exec_time_ms"] >= 1000 else "info",
            "metric": "slow_query", "queryid": q["queryid"],
            "value": f"mean={q['mean_exec_time_ms']:.0f}ms",
            "calls": q["calls"], "total_time_ms": q["total_exec_time_ms"],
        }
        for q in queries
    ]

    return _wrap(
        status=status, findings=findings,
        raw_data={
            "cluster_endpoint": endpoint, "top_n": n, "order_by": order_by,
            "queried_at": _now_iso(), "queries": queries,
        },
        recommendation=recommendation,
    )


# ===========================================================================
# Tool 3 — 索引使用率(冗余 + 候选缺失)
# ===========================================================================
@mcp.tool()
def inspect_index_usage(
    cluster_endpoint: str,
    database: str = "postgres",
) -> dict[str, Any]:
    """巡检索引使用率:找冗余索引(几乎没用到,可删省空间)+ 缺索引的表(顺序扫描占比高,候选加索引)。⚠ database 级:只看所连库的表,排查业务库请传 database=业务库名(默认 postgres 只看 postgres 库)。"""
    endpoint = _resolve_endpoint(cluster_endpoint)
    log.info("[index_usage] %s db=%s", endpoint, database)

    with _connect(endpoint, database) as conn, conn.cursor() as cur:
        # 冗余索引候选
        cur.execute(
            """
            SELECT schemaname, relname AS tablename, indexrelname AS indexname,
                   idx_scan, idx_tup_read, idx_tup_fetch,
                   pg_relation_size(indexrelid) AS index_size_bytes,
                   pg_size_pretty(pg_relation_size(indexrelid)) AS index_size_pretty
            FROM pg_stat_user_indexes
            WHERE idx_scan < 50
            ORDER BY pg_relation_size(indexrelid) DESC
            LIMIT %s
            """,
            (_TOP_N_DEFAULT,),
        )
        unused = [
            {
                "schemaname": r["schemaname"], "tablename": r["tablename"],
                "indexname": r["indexname"], "idx_scan": int(r["idx_scan"]),
                "index_size_bytes": int(r["index_size_bytes"]),
                "index_size_pretty": r["index_size_pretty"],
            }
            for r in cur.fetchall()
        ]

        # 表扫描占比高(seq_scan vs idx_scan)
        cur.execute(
            """
            SELECT schemaname, relname,
                   seq_scan, seq_tup_read, idx_scan, idx_tup_fetch,
                   (seq_scan * 100.0 / NULLIF(seq_scan + idx_scan, 0))::numeric(5,2) AS seq_pct,
                   n_live_tup
            FROM pg_stat_user_tables
            WHERE seq_scan + idx_scan > 100
              AND n_live_tup > 1000
            ORDER BY seq_pct DESC NULLS LAST
            LIMIT %s
            """,
            (_TOP_N_DEFAULT,),
        )
        seq_heavy = [
            {
                "schemaname": r["schemaname"], "relname": r["relname"],
                "seq_scan": int(r["seq_scan"]), "idx_scan": int(r["idx_scan"]),
                "seq_pct": float(r["seq_pct"] or 0),
                "n_live_tup": int(r["n_live_tup"]),
            }
            for r in cur.fetchall()
        ]

    # status:有 seq_pct > 50 的大表 → warning;有 seq_pct > 80 的大表 → critical
    high_seq = [t for t in seq_heavy if t["seq_pct"] >= 50 and t["n_live_tup"] >= 10000]
    very_high_seq = [t for t in seq_heavy if t["seq_pct"] >= 80 and t["n_live_tup"] >= 10000]
    status = "critical" if very_high_seq else "warning" if high_seq or unused else "ok"

    findings: list[dict[str, Any]] = []
    for u in unused[:10]:
        findings.append({
            "severity": "warning", "metric": "unused_index",
            "schema": u["schemaname"], "table": u["tablename"],
            "index": u["indexname"], "size": u["index_size_pretty"],
            "idx_scan": u["idx_scan"],
        })
    for t in seq_heavy[:10]:
        sev = "critical" if t["seq_pct"] >= 80 and t["n_live_tup"] >= 10000 else "warning"
        findings.append({
            "severity": sev, "metric": "seq_scan_heavy",
            "schema": t["schemaname"], "table": t["relname"],
            "seq_pct": f"{t['seq_pct']:.1f}%", "rows": t["n_live_tup"],
        })

    if status == "critical":
        rec = (
            f"发现 {len(very_high_seq)} 张大表(rows>10k)seq_pct ≥ 80%,严重缺索引。"
            f"另发现 {len(unused)} 个低使用索引(idx_scan<50)可考虑删除。"
        )
    elif status == "warning":
        rec = (
            f"发现 {len(high_seq)} 张表 seq_pct ≥ 50% 候选加索引;"
            f"{len(unused)} 个索引 idx_scan<50 候选删除。"
        )
    else:
        rec = "索引使用率健康。"

    return _wrap(
        status=status, findings=findings,
        raw_data={
            "cluster_endpoint": endpoint, "database": database, "queried_at": _now_iso(),
            "unused_indexes": unused, "seq_scan_heavy_tables": seq_heavy,
        },
        recommendation=rec,
    )


# ===========================================================================
# Tool 4 — 表膨胀(autovacuum 滞后)
# ===========================================================================
@mcp.tool()
def inspect_table_bloat(
    cluster_endpoint: str,
    dead_pct_threshold: float = 20.0,
    min_dead_tup: int = 1000,
    database: str = "postgres",
) -> dict[str, Any]:
    """巡检表膨胀:dead tuple 占比高说明 autovacuum 跟不上,表越来越大、查询变慢。⚠ database 级:只看所连库的表,排查业务库请传 database=业务库名(默认 postgres 只看 postgres 库,会漏掉业务表)。"""
    endpoint = _resolve_endpoint(cluster_endpoint)
    if dead_pct_threshold <= 0 or dead_pct_threshold > 100:
        raise ValueError(f"dead_pct_threshold 必须 ∈ (0, 100],收到 {dead_pct_threshold}")

    log.info("[table_bloat] %s db=%s threshold=%.1f%%", endpoint, database, dead_pct_threshold)
    with _connect(endpoint, database) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT schemaname, relname,
                   n_live_tup, n_dead_tup,
                   (n_dead_tup * 100.0 / NULLIF(n_live_tup + n_dead_tup, 0))::numeric(5,2) AS dead_pct,
                   last_autovacuum, last_autoanalyze,
                   last_vacuum, last_analyze,
                   pg_total_relation_size(schemaname || '.' || relname) AS total_size_bytes,
                   pg_size_pretty(pg_total_relation_size(schemaname || '.' || relname)) AS total_size_pretty
            FROM pg_stat_user_tables
            WHERE n_dead_tup > %s
              AND (n_dead_tup * 100.0 / NULLIF(n_live_tup + n_dead_tup, 0)) >= %s
            ORDER BY n_dead_tup DESC
            LIMIT %s
            """,
            (min_dead_tup, dead_pct_threshold, _QUERY_LIMIT),
        )
        rows = cur.fetchall()

    bloat = [
        {
            "schemaname": r["schemaname"], "relname": r["relname"],
            "n_live_tup": int(r["n_live_tup"]), "n_dead_tup": int(r["n_dead_tup"]),
            "dead_pct": float(r["dead_pct"] or 0),
            "last_autovacuum": r["last_autovacuum"].isoformat() if r["last_autovacuum"] else None,
            "last_autoanalyze": r["last_autoanalyze"].isoformat() if r["last_autoanalyze"] else None,
            "total_size_pretty": r["total_size_pretty"],
        }
        for r in rows
    ]

    if not bloat:
        status, rec = "ok", f"无 dead_pct ≥ {dead_pct_threshold}% 的膨胀表。"
    else:
        max_pct = max(b["dead_pct"] for b in bloat)
        if max_pct >= 50:
            status = "critical"
            rec = (
                f"发现 {len(bloat)} 张膨胀表,最高 dead_pct {max_pct:.1f}%。"
                "建议:1) 手动 VACUUM (verbose) 这些表;2) 调高 autovacuum_vacuum_scale_factor / "
                "autovacuum_naptime;3) 看是否有大量 UPDATE/DELETE 的业务模式可优化。"
            )
        else:
            status = "warning"
            rec = f"发现 {len(bloat)} 张膨胀表(dead_pct ≥ {dead_pct_threshold}%),关注 autovacuum 配置。"

    findings = [
        {
            "severity": "critical" if b["dead_pct"] >= 50 else "warning",
            "metric": "table_bloat",
            "schema": b["schemaname"], "table": b["relname"],
            "dead_pct": f"{b['dead_pct']:.1f}%",
            "dead_tup": b["n_dead_tup"], "live_tup": b["n_live_tup"],
            "size": b["total_size_pretty"],
            "last_autovacuum": b["last_autovacuum"],
        }
        for b in bloat
    ]

    return _wrap(
        status=status, findings=findings,
        raw_data={
            "cluster_endpoint": endpoint, "database": database,
            "dead_pct_threshold": dead_pct_threshold, "min_dead_tup": min_dead_tup,
            "queried_at": _now_iso(), "bloated_tables": bloat,
        },
        recommendation=rec,
    )


# ===========================================================================
# Tool 5 — Aurora replica 复制延迟
# ===========================================================================
@mcp.tool()
def inspect_replica_lag(
    cluster_endpoint: str,
    database: str = "postgres",
) -> dict[str, Any]:
    """巡检 Aurora 集群复制延迟(reader 落后 writer 多少毫秒)— Aurora 专用,走 aurora_replica_status()。必须连 writer endpoint(.cluster- 后缀)。非 Aurora 请用 inspect_pg_stat_replication。"""
    endpoint = _resolve_endpoint(cluster_endpoint)
    log.info("[replica_lag] %s db=%s", endpoint, database)

    with _connect(endpoint, database) as conn, conn.cursor() as cur:
        # 先看本节点是否 reader
        cur.execute(
            "SELECT pg_is_in_recovery() AS in_recovery, "
            "current_setting('server_version', true) AS version"
        )
        meta = cur.fetchone()
        in_recovery = bool(meta["in_recovery"])

        # 试 aurora_replica_status() — Aurora 特有,且实测**只能在 writer 上调**
        # (reader 上调会抛 InsufficientPrivilege 或 ReadOnlySqlTransaction)
        replicas_raw: list[dict[str, Any]] = []
        is_aurora = False
        aurora_error: str | None = None
        try:
            cur.execute(
                "SELECT server_id, session_id, replica_lag_in_msec, cpu, "
                "last_update_timestamp FROM aurora_replica_status()"
            )
            replicas_raw = list(cur.fetchall())
            is_aurora = True
        except psycopg.errors.UndefinedFunction:
            aurora_error = "function aurora_replica_status() does not exist (not Aurora?)"
        except (psycopg.errors.InsufficientPrivilege,
                psycopg.errors.FeatureNotSupported,
                psycopg.errors.ReadOnlySqlTransaction) as e:
            # reader 上调用 / 权限不足 — Aurora 但需要 writer endpoint
            is_aurora = True
            aurora_error = f"{type(e).__name__}: {str(e)[:100]}"

        # 如果连的是 reader,看自己的 lag(注意:Aurora 不支持
        # pg_last_xact_replay_timestamp() — Aurora 用 storage replication 而非
        # WAL replay,普通 PG 这个函数才有意义。Aurora 上跑会抛 unsupported,
        # 优雅捕获即可)
        self_lag_seconds: float | None = None
        if in_recovery:
            try:
                cur.execute(
                    "SELECT EXTRACT(EPOCH FROM (NOW() - pg_last_xact_replay_timestamp())) AS lag_seconds"
                )
                row = cur.fetchone()
                if row and row["lag_seconds"] is not None:
                    self_lag_seconds = float(row["lag_seconds"])
            except (psycopg.errors.FeatureNotSupported,
                    psycopg.errors.UndefinedFunction,
                    psycopg.errors.InternalError_):
                # Aurora 不支持此函数(用 aurora_replica_status() 代替)
                self_lag_seconds = None

    replicas = [
        {
            "server_id": str(r["server_id"]), "session_id": str(r["session_id"]),
            "replica_lag_ms": float(r["replica_lag_in_msec"] or 0),
            "cpu": float(r["cpu"] or 0),
            "last_update": r["last_update_timestamp"].isoformat() if r["last_update_timestamp"] else None,
        }
        for r in replicas_raw
    ]

    if not is_aurora or aurora_error:
        return _wrap(
            status="warning", findings=[],
            raw_data={
                "cluster_endpoint": endpoint, "queried_at": _now_iso(),
                "is_aurora": is_aurora, "in_recovery": in_recovery,
                "self_lag_seconds": self_lag_seconds,
                "aurora_error": aurora_error,
            },
            recommendation=(
                f"无法获取 Aurora replica status。是否 Aurora: {is_aurora}, "
                f"in_recovery: {in_recovery}, error: {aurora_error or 'none'}。"
                "建议:本工具应连 cluster writer endpoint(`.cluster-` 后缀,非 `.cluster-ro-`),"
                "因为 aurora_replica_status() 只能在 writer 上调用。"
                "若是标准 PG(非 Aurora)请用 inspect_pg_stat_replication。"
            ),
        )

    # status:lag_ms > 1000 warning;> 5000 critical
    max_lag_ms = max((r["replica_lag_ms"] for r in replicas), default=0.0)
    if max_lag_ms >= 5000:
        status = "critical"
        rec = f"replica lag 最高 {max_lag_ms:.0f}ms ≥ 5s,reader 严重落后,读到老数据风险高。"
    elif max_lag_ms >= 1000:
        status = "warning"
        rec = f"replica lag 最高 {max_lag_ms:.0f}ms ≥ 1s,关注趋势。"
    else:
        status = "ok"
        rec = f"replica lag 健康(最高 {max_lag_ms:.0f}ms)。"

    findings = [
        {
            "severity": "critical" if r["replica_lag_ms"] >= 5000
                        else "warning" if r["replica_lag_ms"] >= 1000 else "info",
            "metric": "replica_lag",
            "server_id": r["server_id"],
            "lag_ms": r["replica_lag_ms"], "cpu": r["cpu"],
        }
        for r in replicas
    ]

    return _wrap(
        status=status, findings=findings,
        raw_data={
            "cluster_endpoint": endpoint, "queried_at": _now_iso(),
            "is_aurora": True, "in_recovery": in_recovery,
            "self_lag_seconds": self_lag_seconds, "replicas": replicas,
        },
        recommendation=rec,
    )


# ===========================================================================
# Tool 6 — 连接分布(连接 leak 候选)
# ===========================================================================
@mcp.tool()
def inspect_connections(
    cluster_endpoint: str,
    database: str = "postgres",
) -> dict[str, Any]:
    """巡检连接数与分布(按 user/app/state 聚合),排查连接耗尽 / 连接池泄漏(idle in transaction 堆积)。实例级:看到所有库的连接,database 参数不影响结果。"""
    endpoint = _resolve_endpoint(cluster_endpoint)
    log.info("[connections] %s db=%s", endpoint, database)

    with _connect(endpoint, database) as conn, conn.cursor() as cur:
        cur.execute("SHOW max_connections")
        max_conn = int(cur.fetchone()["max_connections"])

        cur.execute(
            """
            SELECT usename, application_name, state,
                   count(*) AS conn_count,
                   EXTRACT(EPOCH FROM max(NOW() - state_change))::int AS oldest_conn_age_seconds
            FROM pg_stat_activity
            WHERE backend_type = 'client backend'
            GROUP BY usename, application_name, state
            ORDER BY conn_count DESC
            LIMIT %s
            """,
            (_QUERY_LIMIT,),
        )
        groups = [
            {
                "usename": r["usename"] or "", "application_name": r["application_name"] or "",
                "state": r["state"] or "",
                "conn_count": int(r["conn_count"]),
                "oldest_conn_age_seconds": int(r["oldest_conn_age_seconds"] or 0),
            }
            for r in cur.fetchall()
        ]

        cur.execute(
            "SELECT count(*) AS total FROM pg_stat_activity WHERE backend_type = 'client backend'"
        )
        total = int(cur.fetchone()["total"])

    used_pct = total * 100.0 / max(max_conn, 1)
    if used_pct >= 80:
        status, sev = "critical", "critical"
        rec = (
            f"客户端连接 {total}/{max_conn}({used_pct:.1f}%),≥ 80% 即将耗尽。"
            "建议:1) 看 idle in transaction 是否有连接 leak;2) 应用层启用连接池;"
            "3) 紧急可调高 max_connections(需重启)。"
        )
    elif used_pct >= 50:
        status, sev = "warning", "warning"
        rec = f"客户端连接 {total}/{max_conn}({used_pct:.1f}%),关注趋势。"
    else:
        status, sev = "ok", "info"
        rec = f"连接数健康({total}/{max_conn},{used_pct:.1f}%)。"

    findings = [
        {
            "severity": sev, "metric": "connection_group",
            "usename": g["usename"], "app": g["application_name"], "state": g["state"],
            "count": g["conn_count"], "oldest_age_s": g["oldest_conn_age_seconds"],
        }
        for g in groups[:10]
    ]

    return _wrap(
        status=status, findings=findings,
        raw_data={
            "cluster_endpoint": endpoint, "queried_at": _now_iso(),
            "max_connections": max_conn, "total_client_connections": total,
            "used_pct": round(used_pct, 2), "groups": groups,
        },
        recommendation=rec,
    )


# ===========================================================================
# Tool 7 — 锁等待 / 阻塞链
# ===========================================================================
@mcp.tool()
def inspect_blocking_chains(
    cluster_endpoint: str,
    database: str = "postgres",
) -> dict[str, Any]:
    """巡检锁阻塞链:谁在等锁、被谁阻塞、等了多久 — 排查"查询突然卡住 / 业务 hang"。实例级:看到所有库的锁等待,database 参数不影响结果。"""
    endpoint = _resolve_endpoint(cluster_endpoint)
    log.info("[blocking_chains] %s db=%s", endpoint, database)

    with _connect(endpoint, database) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT blocked.pid AS blocked_pid,
                   blocked.usename AS blocked_user,
                   blocked.application_name AS blocked_app,
                   blocked.query AS blocked_query,
                   blocked.wait_event_type AS wait_event_type,
                   blocked.wait_event AS wait_event,
                   EXTRACT(EPOCH FROM (NOW() - blocked.query_start))::int AS blocked_duration_seconds,
                   blocking.pid AS blocking_pid,
                   blocking.usename AS blocking_user,
                   blocking.application_name AS blocking_app,
                   blocking.query AS blocking_query,
                   blocking.state AS blocking_state
            FROM pg_stat_activity blocked
            JOIN pg_stat_activity blocking
              ON blocking.pid = ANY(pg_blocking_pids(blocked.pid))
            WHERE blocked.wait_event_type = 'Lock'
            ORDER BY blocked_duration_seconds DESC
            LIMIT %s
            """,
            (_QUERY_LIMIT,),
        )
        rows = cur.fetchall()

    chains = [
        {
            "blocked_pid": int(r["blocked_pid"]),
            "blocked_user": r["blocked_user"] or "", "blocked_app": r["blocked_app"] or "",
            "blocked_query": _redact_query(r["blocked_query"]),
            "wait_event_type": r["wait_event_type"] or "", "wait_event": r["wait_event"] or "",
            "blocked_duration_seconds": int(r["blocked_duration_seconds"] or 0),
            "blocking_pid": int(r["blocking_pid"]),
            "blocking_user": r["blocking_user"] or "", "blocking_app": r["blocking_app"] or "",
            "blocking_query": _redact_query(r["blocking_query"]),
            "blocking_state": r["blocking_state"] or "",
        }
        for r in rows
    ]

    if not chains:
        status, rec = "ok", "无锁阻塞。"
    else:
        max_dur = max(c["blocked_duration_seconds"] for c in chains)
        if max_dur >= 60:
            status = "critical"
            rec = (
                f"发现 {len(chains)} 条阻塞链,最长被阻塞 {max_dur}s ≥ 1 分钟。"
                "建议:1) pg_terminate_backend(blocking_pid) 杀阻塞源(需 admin);"
                "2) 排查 blocking_query 业务逻辑是否有大事务 / 未提交。"
            )
        else:
            status = "warning"
            rec = f"发现 {len(chains)} 条阻塞链,最长 {max_dur}s。关注是否升级。"

    findings = [
        {
            "severity": "critical" if c["blocked_duration_seconds"] >= 60 else "warning",
            "metric": "lock_blocking",
            "blocked_pid": c["blocked_pid"], "blocking_pid": c["blocking_pid"],
            "duration_s": c["blocked_duration_seconds"],
            "wait_event": f"{c['wait_event_type']}/{c['wait_event']}",
        }
        for c in chains
    ]

    return _wrap(
        status=status, findings=findings,
        raw_data={
            "cluster_endpoint": endpoint, "database": database,
            "queried_at": _now_iso(), "blocking_chains": chains,
        },
        recommendation=rec,
    )


# ===========================================================================
# Tool 8 — 表 / 索引大小 top N
# ===========================================================================
@mcp.tool()
def inspect_table_sizes(
    cluster_endpoint: str,
    top_n: int = _TOP_N_DEFAULT,
    database: str = "postgres",
) -> dict[str, Any]:
    """巡检表 / 索引大小 top N,找最占空间的表(容量规划 / 找异常大表)。⚠ database 级:只看所连库的表,排查业务库请传 database=业务库名(默认 postgres)。"""
    endpoint = _resolve_endpoint(cluster_endpoint)
    n = max(1, min(top_n, _QUERY_LIMIT))
    log.info("[table_sizes] %s db=%s top=%d", endpoint, database, n)

    with _connect(endpoint, database) as conn, conn.cursor() as cur:
        # 数据库级总大小
        cur.execute(
            "SELECT pg_database_size(current_database()) AS db_size, "
            "pg_size_pretty(pg_database_size(current_database())) AS db_size_pretty"
        )
        db = cur.fetchone()

        # 表大小 top N(包含索引 + toast)
        cur.execute(
            """
            SELECT schemaname, relname,
                   n_live_tup,
                   pg_total_relation_size(schemaname || '.' || relname) AS total_bytes,
                   pg_relation_size(schemaname || '.' || relname) AS table_bytes,
                   pg_total_relation_size(schemaname || '.' || relname) -
                       pg_relation_size(schemaname || '.' || relname) AS index_bytes,
                   pg_size_pretty(pg_total_relation_size(schemaname || '.' || relname)) AS total_pretty,
                   pg_size_pretty(pg_relation_size(schemaname || '.' || relname)) AS table_pretty,
                   pg_size_pretty(pg_total_relation_size(schemaname || '.' || relname) -
                                  pg_relation_size(schemaname || '.' || relname)) AS index_pretty
            FROM pg_stat_user_tables
            ORDER BY pg_total_relation_size(schemaname || '.' || relname) DESC
            LIMIT %s
            """,
            (n,),
        )
        tables = [
            {
                "schemaname": r["schemaname"], "relname": r["relname"],
                "n_live_tup": int(r["n_live_tup"]),
                "total_bytes": int(r["total_bytes"]), "total_pretty": r["total_pretty"],
                "table_bytes": int(r["table_bytes"]), "table_pretty": r["table_pretty"],
                "index_bytes": int(r["index_bytes"]), "index_pretty": r["index_pretty"],
            }
            for r in cur.fetchall()
        ]

    # status:db_size > 100GB warning;> 500GB critical(可调)
    db_bytes = int(db["db_size"])
    db_gb = db_bytes / 1024 ** 3
    if db_gb >= 500:
        status = "critical"
        rec = f"数据库大小 {db_gb:.1f} GB,极大,关注存储成本 + 备份时长。"
    elif db_gb >= 100:
        status = "warning"
        rec = f"数据库大小 {db_gb:.1f} GB,较大。建议归档历史数据 / 分区。"
    else:
        status = "ok"
        rec = f"数据库大小 {db_gb:.2f} GB,健康。"

    findings = [
        {
            "severity": "info", "metric": "table_size",
            "schema": t["schemaname"], "table": t["relname"],
            "size": t["total_pretty"], "rows": t["n_live_tup"],
        }
        for t in tables[:10]
    ]

    return _wrap(
        status=status, findings=findings,
        raw_data={
            "cluster_endpoint": endpoint, "database": database,
            "queried_at": _now_iso(),
            "db_size_bytes": db_bytes, "db_size_pretty": db["db_size_pretty"],
            "top_tables": tables,
        },
        recommendation=rec,
    )


# ===========================================================================
# Tool 9 — 活跃 client 分布
# ===========================================================================
@mcp.tool()
def inspect_active_clients(
    cluster_endpoint: str,
    database: str = "postgres",
) -> dict[str, Any]:
    """巡检活跃客户端来源分布(按 user + 来源 IP + 应用名 聚合)— 排查"谁在连数据库 / 有没有非预期来源"。实例级:看到所有库的连接,database 参数不影响结果。"""
    endpoint = _resolve_endpoint(cluster_endpoint)
    log.info("[active_clients] %s db=%s", endpoint, database)

    with _connect(endpoint, database) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT usename,
                   client_addr::text AS client_addr,
                   count(*) AS conn_count,
                   array_agg(DISTINCT application_name) FILTER (WHERE application_name IS NOT NULL) AS apps,
                   max(NOW() - backend_start) AS oldest_conn_age
            FROM pg_stat_activity
            WHERE backend_type = 'client backend'
            GROUP BY usename, client_addr
            ORDER BY conn_count DESC
            LIMIT %s
            """,
            (_QUERY_LIMIT,),
        )
        clients = [
            {
                "usename": r["usename"] or "",
                "client_addr": r["client_addr"] if r["client_addr"] else None,
                "conn_count": int(r["conn_count"]),
                "apps": list(r["apps"] or []),
                "oldest_conn_age_seconds": int(r["oldest_conn_age"].total_seconds())
                    if r["oldest_conn_age"] is not None else 0,
            }
            for r in cur.fetchall()
        ]

    total_clients = len(clients)
    total_conns = sum(c["conn_count"] for c in clients)

    # status:简单暴露事实,不强判别(usename 非业务白名单需要外部知识,这里只列)
    status = "ok"
    rec = f"当前 {total_clients} 个独立 (user, client_ip) 组合,共 {total_conns} 个连接。审阅是否含非预期来源。"

    findings = [
        {
            "severity": "info", "metric": "client_activity",
            "user": c["usename"], "ip": c["client_addr"],
            "conns": c["conn_count"], "apps": c["apps"][:5],
        }
        for c in clients[:15]
    ]

    return _wrap(
        status=status, findings=findings,
        raw_data={
            "cluster_endpoint": endpoint, "database": database,
            "queried_at": _now_iso(),
            "total_distinct_clients": total_clients, "total_connections": total_conns,
            "clients": clients,
        },
        recommendation=rec,
    )


# ===========================================================================
# Tool 10 — Sequence 容量(int4 列耗尽风险,Alan case 同款)
# ===========================================================================
@mcp.tool()
def inspect_sequence_capacity(
    cluster_endpoint: str,
    warning_pct: float = 70.0,
    critical_pct: float = 90.0,
    database: str = "",
    scan_all_databases: bool = True,
    max_databases: int = _MAX_SCAN_DATABASES_DEFAULT,
) -> dict[str, Any]:
    """巡检 sequence 容量耗尽风险:自增列(int4/int2)关联的 sequence 快用到上限会导致 INSERT 报 "integer out of range" 业务全停。自动扫全 cluster 各库(默认,db 数超 max_databases 截断),无需指定 database。

    生产场景:历史建表用 `id INTEGER` + 关联 sequence,sequence 默认 max_value=2^63
    但**关联列是 INTEGER 时实际上限是 2^31-1 = 2,147,483,647**。当 sequence
    nextval() 返回 > 21 亿,INSERT 抛 `integer out of range`,业务全停。

    本 tool 基于**关联列实际类型**计算真上限(不是 sequence 自身的 max_value):
    - 关联列 smallint(int2)→ 真上限 2^15-1 = 32,767
    - 关联列 integer(int4)→ 真上限 2^31-1 = 2,147,483,647
    - 关联列 bigint(int8)→ 真上限 = sequence 自身 max_value

    Args:
        cluster_endpoint: PG cluster endpoint(必传,如 my-cluster.cluster-xxx.rds.amazonaws.com)
        warning_pct: 用量 ≥ 此值 → warning(默认 70.0)
        critical_pct: 用量 ≥ 此值 → critical(默认 90.0)
        database: 巡检指定 database(传则只扫这个;留空 + scan_all=True 自动扫全 cluster)
        scan_all_databases: True(默认)= 扫全 cluster 所有非模板 db;
                            False = 只扫 database 指定的(database 留空时 fallback "postgres")

    返回(conventions A8):
      raw_data.sequences  按 effective_used_pct DESC 排序的全部 sequence,
                          每条含:database / schema / name / last_value /
                          owned_by / owned_column_type /
                          effective_max_value / effective_used_pct /
                          recommended_sql(自动生成的迁移 SQL 建议)
    """
    endpoint = _resolve_endpoint(cluster_endpoint)
    if not (0 < warning_pct <= 100) or not (0 < critical_pct <= 100):
        raise ValueError("warning_pct / critical_pct 必须 ∈ (0, 100]")
    if warning_pct >= critical_pct:
        raise ValueError(f"warning_pct ({warning_pct}) 必须 < critical_pct ({critical_pct})")

    # 决定要扫哪些 db(带 P2 性能护栏:db 数超 max_databases 则截断 + 提示)
    db_list, cap_note = _resolve_scan_db_list(
        endpoint, database=database,
        scan_all_databases=scan_all_databases, max_databases=max_databases,
    )

    log.info("[sequence_capacity] %s scanning %d db(s): %s",
             endpoint, len(db_list), db_list)

    all_sequences: list[dict[str, Any]] = []
    db_errors: dict[str, str] = {}

    for db in db_list:
        try:
            with _connect(endpoint, db) as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    WITH seq_owned AS (
                        SELECT
                            c_seq.oid AS sequence_oid,
                            n_seq.nspname AS sequence_schema,
                            c_seq.relname AS sequence_name,
                            c_tab.oid AS table_oid,
                            n_tab.nspname AS table_schema,
                            c_tab.relname AS table_name,
                            a_col.attname AS column_name,
                            a_col.atttypid AS column_typid,
                            format_type(a_col.atttypid, a_col.atttypmod) AS column_type
                        FROM pg_class c_seq
                        JOIN pg_namespace n_seq ON n_seq.oid = c_seq.relnamespace
                        LEFT JOIN pg_depend d
                            ON d.objid = c_seq.oid
                           AND d.classid = 'pg_class'::regclass
                           AND d.deptype = 'a'
                        LEFT JOIN pg_class c_tab ON c_tab.oid = d.refobjid
                        LEFT JOIN pg_namespace n_tab ON n_tab.oid = c_tab.relnamespace
                        LEFT JOIN pg_attribute a_col
                            ON a_col.attrelid = d.refobjid
                           AND a_col.attnum = d.refobjsubid
                        WHERE c_seq.relkind = 'S'
                          AND n_seq.nspname NOT IN ('pg_catalog', 'information_schema')
                    )
                    SELECT
                        s.schemaname,
                        s.sequencename,
                        s.last_value,
                        s.max_value AS seq_max_value,
                        s.data_type AS seq_data_type,
                        so.table_schema AS owned_schema,
                        so.table_name AS owned_table,
                        so.column_name AS owned_column,
                        so.column_type AS owned_column_type,
                        CASE
                            WHEN so.column_typid = 21 THEN 32767::numeric
                            WHEN so.column_typid = 23 THEN 2147483647::numeric
                            WHEN so.column_typid = 20 THEN s.max_value::numeric
                            ELSE s.max_value::numeric
                        END AS effective_max_value,
                        so.column_typid AS owned_column_typid
                    FROM pg_sequences s
                    LEFT JOIN seq_owned so
                        ON so.sequence_schema = s.schemaname
                       AND so.sequence_name = s.sequencename
                    """,
                )
                rows = cur.fetchall()

            for r in rows:
                last_value = int(r["last_value"]) if r["last_value"] is not None else 0
                eff_max = int(r["effective_max_value"])
                used_pct = (last_value * 100.0 / eff_max) if eff_max > 0 else 0.0
                owned_full = (
                    f"{r['owned_schema']}.{r['owned_table']}.{r['owned_column']}"
                    if r["owned_table"] else None
                )
                col_type = (r["owned_column_type"] or "").lower()
                sequence_full = f'"{r["schemaname"]}"."{r["sequencename"]}"'

                if col_type.startswith("integer") and used_pct >= warning_pct:
                    owned_table_full = f'"{r["owned_schema"]}"."{r["owned_table"]}"'
                    recommended_sql = (
                        f"-- DB={db} 切表列类型 integer → bigint:\n"
                        f"\\c {db}\n"
                        f"ALTER TABLE {owned_table_full} ALTER COLUMN \"{r['owned_column']}\" TYPE bigint;\n"
                        f"ALTER SEQUENCE {sequence_full} AS bigint;"
                    )
                elif col_type.startswith("smallint") and used_pct >= warning_pct:
                    recommended_sql = (
                        f"-- DB={db} 列是 smallint(实际上限 32767),强烈建议升 bigint:\n"
                        f"\\c {db}\n"
                        f"ALTER TABLE \"{r['owned_schema']}\".\"{r['owned_table']}\" "
                        f"ALTER COLUMN \"{r['owned_column']}\" TYPE bigint;"
                    )
                else:
                    recommended_sql = None

                all_sequences.append({
                    "database": db,
                    "schema": r["schemaname"],
                    "name": r["sequencename"],
                    "full_name": f"{db}.{r['schemaname']}.{r['sequencename']}",
                    "last_value": last_value,
                    "sequence_max_value": int(r["seq_max_value"]),
                    "sequence_type": str(r["seq_data_type"]),
                    "owned_by": owned_full,
                    "owned_column_type": col_type or None,
                    "effective_max_value": eff_max,
                    "effective_used_pct": round(used_pct, 4),
                    "recommended_sql": recommended_sql,
                })
        except Exception as e:
            # 某 db 没权限连 / 不可达,记下来继续扫别的(graceful 降级)
            db_errors[db] = f"{type(e).__name__}: {str(e)[:200]}"
            continue

    # 按用量降序
    all_sequences.sort(key=lambda x: x["effective_used_pct"], reverse=True)

    over_critical = [s for s in all_sequences if s["effective_used_pct"] >= critical_pct]
    over_warning = [
        s for s in all_sequences
        if warning_pct <= s["effective_used_pct"] < critical_pct
    ]

    if over_critical:
        status = "critical"
        top = over_critical[0]
        rec = (
            f"⚠ {len(over_critical)} 个 sequence 用量 ≥ {critical_pct}%,即将耗尽。"
            f" 最严重:{top['full_name']}({top['effective_used_pct']:.2f}% of {top['effective_max_value']:,},"
            f"owned_by={top['owned_by']},column_type={top['owned_column_type']})。"
            f" **必须立即执行迁移**(参考 raw_data.sequences[].recommended_sql),"
            f"否则下次 nextval 返回值将超出列类型上限,触发 'integer out of range' 业务全停。"
        )
    elif over_warning:
        status = "warning"
        top = over_warning[0]
        rec = (
            f"{len(over_warning)} 个 sequence 用量 ≥ {warning_pct}%,需要规划迁移窗口。"
            f" 最高:{top['full_name']}({top['effective_used_pct']:.2f}%,column_type={top['owned_column_type']})。"
        )
    else:
        status = "ok"
        max_pct = max((s["effective_used_pct"] for s in all_sequences), default=0)
        if all_sequences:
            rec = (
                f"扫描 {len(db_list)} 个 db 共 {len(all_sequences)} 个 sequence,"
                f"最高用量 {max_pct:.2f}%,健康。"
            )
        else:
            rec = f"扫描 {len(db_list)} 个 db 均无 sequence。"

    if db_errors:
        rec += f" ⚠ {len(db_errors)} 个 db 扫描失败:{list(db_errors.keys())}"
    if cap_note:
        rec += " " + cap_note

    findings = [
        {
            "severity": "critical" if s["effective_used_pct"] >= critical_pct
                        else "warning" if s["effective_used_pct"] >= warning_pct
                        else "info",
            "metric": "sequence_capacity",
            "sequence": s["full_name"],
            "used_pct": f"{s['effective_used_pct']:.2f}%",
            "last_value": s["last_value"],
            "effective_max_value": s["effective_max_value"],
            "owned_by": s["owned_by"],
            "owned_column_type": s["owned_column_type"],
        }
        for s in all_sequences
        if s["effective_used_pct"] >= warning_pct
    ]

    return _wrap(
        status=status, findings=findings,
        raw_data={
            "cluster_endpoint": endpoint,
            "scanned_databases": db_list,
            "max_databases": max_databases,
            "scan_capped": cap_note is not None,
            "warning_pct": warning_pct, "critical_pct": critical_pct,
            "queried_at": _now_iso(),
            "total_sequences": len(all_sequences),
            "db_errors": db_errors if db_errors else None,
            "sequences": all_sequences,
        },
        recommendation=rec,
    )


# ===========================================================================
# Tool 11 — Transaction ID wraparound 风险(B1 关键)
# ===========================================================================
@mcp.tool()
def inspect_xid_wraparound(
    cluster_endpoint: str,
    warning_pct: float = 50.0,
    critical_pct: float = 80.0,
) -> dict[str, Any]:
    """巡检事务 ID wraparound 风险(xid age 接近 20 亿会导致数据库进只读保护、业务全停)— autovacuum 失效时的致命风险。实例级:自动看全 cluster 各库 + 表级 age,无需指定 database。

    PG 用 32-bit transaction ID,超 ~2 billion 进只读模式。autovacuum 应自动 freeze
    旧 row 防止;若 autovacuum 失效,xid age 单调增长 → 业务全停。

    本 tool 同时看两个维度:
      1. 各 db 级 age(datfrozenxid):决定 cluster wraparound 风险
      2. 各表级 age(relfrozenxid) top N:找哪些表是 vacuum freeze 不到的"钉子户"

    阈值参考:autovacuum_freeze_max_age 默认 200M,达 1.6B 即 80% 接近 wraparound。

    Args:
        warning_pct: db age / 2B * 100 >= 此值 → warning(默认 50%)
        critical_pct: 同上 → critical(默认 80%)
    """
    endpoint = _resolve_endpoint(cluster_endpoint)
    if not (0 < warning_pct < critical_pct <= 100):
        raise ValueError("warning_pct < critical_pct 且 ∈ (0, 100]")

    log.info("[xid_wraparound] %s warn=%.1f%% crit=%.1f%%",
             endpoint, warning_pct, critical_pct)

    # 各 db age:这个 SQL 不依赖 user table,任何 db 都能查全集群;连 postgres 即可
    with _connect(endpoint, "postgres") as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT datname,
                   age(datfrozenxid) AS xid_age,
                   ROUND(age(datfrozenxid)::numeric / 2147483648 * 100, 2) AS age_pct,
                   datfrozenxid::text AS frozen_xid
            FROM pg_database
            WHERE datallowconn
            ORDER BY age(datfrozenxid) DESC
            """
        )
        databases = [
            {
                "datname": r["datname"],
                "xid_age": int(r["xid_age"]),
                "age_pct": float(r["age_pct"] or 0),
                "frozen_xid": str(r["frozen_xid"]),
            }
            for r in cur.fetchall()
        ]

        # autovacuum_freeze_max_age 当前值(决定何时触发 wraparound prevention vacuum)
        cur.execute("SHOW autovacuum_freeze_max_age")
        autovac_freeze_max_age = int(cur.fetchone()["autovacuum_freeze_max_age"])

    # 各表 age — 在 age 最大的 db 跑(用第一个 db,不是 postgres)
    worst_db = databases[0]["datname"] if databases else "postgres"
    table_top: list[dict[str, Any]] = []
    table_query_error: str | None = None
    try:
        with _connect(endpoint, worst_db) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT n.nspname AS schemaname,
                       c.relname,
                       age(c.relfrozenxid) AS xid_age,
                       ROUND(age(c.relfrozenxid)::numeric / 2147483648 * 100, 2) AS age_pct,
                       pg_size_pretty(pg_total_relation_size(c.oid)) AS size
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE c.relkind IN ('r', 'm', 't')
                  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
                ORDER BY age(c.relfrozenxid) DESC
                LIMIT %s
                """,
                (_TOP_N_DEFAULT,),
            )
            table_top = [
                {
                    "schemaname": r["schemaname"], "relname": r["relname"],
                    "xid_age": int(r["xid_age"]),
                    "age_pct": float(r["age_pct"] or 0),
                    "size": r["size"],
                }
                for r in cur.fetchall()
            ]
    except Exception as e:
        table_query_error = f"{type(e).__name__}: {str(e)[:200]}"

    max_db_pct = max((d["age_pct"] for d in databases), default=0.0)
    if max_db_pct >= critical_pct:
        status = "critical"
        worst = max(databases, key=lambda d: d["age_pct"])
        rec = (
            f"⚠ db {worst['datname']} xid age = {worst['age_pct']:.1f}% of 2B,"
            f"≥ {critical_pct}% 接近 wraparound!立即执行: "
            "1) 检查 autovacuum 是否在跑 — pg_stat_progress_vacuum 看进度; "
            "2) 调高 autovacuum_max_workers / 调低 autovacuum_vacuum_cost_delay; "
            "3) 必要时手工 VACUUM (FREEZE) 表级别(看 raw_data.tables top N)。"
        )
    elif max_db_pct >= warning_pct:
        status = "warning"
        worst = max(databases, key=lambda d: d["age_pct"])
        rec = (
            f"db {worst['datname']} xid age = {worst['age_pct']:.1f}%,"
            f"≥ {warning_pct}% 进入观察。建议关注 autovacuum 是否跟上。"
        )
    else:
        status = "ok"
        rec = (f"全 cluster xid age 健康,最高 {max_db_pct:.2f}% of 2B"
               f"(autovacuum_freeze_max_age={autovac_freeze_max_age}). ")

    findings: list[dict[str, Any]] = []
    for d in databases:
        sev = ("critical" if d["age_pct"] >= critical_pct
               else "warning" if d["age_pct"] >= warning_pct else "info")
        findings.append({
            "severity": sev, "metric": "db_xid_age",
            "datname": d["datname"], "age_pct": f"{d['age_pct']:.2f}%",
            "xid_age": d["xid_age"],
        })
    for t in table_top[:5]:
        if t["age_pct"] >= warning_pct:
            findings.append({
                "severity": "warning", "metric": "table_xid_age",
                "table": f"{t['schemaname']}.{t['relname']}",
                "age_pct": f"{t['age_pct']:.2f}%",
                "size": t["size"],
            })

    return _wrap(
        status=status, findings=findings,
        raw_data={
            "cluster_endpoint": endpoint,
            "queried_at": _now_iso(),
            "warning_pct": warning_pct, "critical_pct": critical_pct,
            "autovacuum_freeze_max_age": autovac_freeze_max_age,
            "databases": databases,
            "tables_top_by_age_in_db": worst_db,
            "tables": table_top,
            "table_query_error": table_query_error,
        },
        recommendation=rec,
    )


# ===========================================================================
# Tool 12 — VACUUM 进度(B1)
# ===========================================================================
@mcp.tool()
def inspect_vacuum_progress(
    cluster_endpoint: str,
    database: str = "",
    scan_all_databases: bool = True,
    max_databases: int = _MAX_SCAN_DATABASES_DEFAULT,
) -> dict[str, Any]:
    """巡检正在运行的 VACUUM / ANALYZE 进度(跑到百分之几、还要多久)— 配合 xid_wraparound 看"救火 vacuum 能不能赶上"。自动扫全 cluster 各库(db 数超 max_databases 截断),无需指定 database。

    PG 13+ 提供 progress view,看 phase / heap_blks_scanned / heap_blks_total 算
    完成百分比。结合 inspect_xid_wraparound,客户能知道"自动救火 vacuum 跑到哪了"。

    Args:
        scan_all_databases: True(默认)= 扫所有 db 找 progress;False = 只扫指定 db
    """
    endpoint = _resolve_endpoint(cluster_endpoint)

    db_list, cap_note = _resolve_scan_db_list(
        endpoint, database=database,
        scan_all_databases=scan_all_databases, max_databases=max_databases,
    )

    log.info("[vacuum_progress] %s scanning %d db(s)", endpoint, len(db_list))

    all_vac: list[dict[str, Any]] = []
    all_ana: list[dict[str, Any]] = []
    db_errors: dict[str, str] = {}

    for db in db_list:
        try:
            with _connect(endpoint, db) as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT pid, datname, relid::regclass::text AS relname,
                           phase, heap_blks_total, heap_blks_scanned,
                           heap_blks_vacuumed, index_vacuum_count,
                           max_dead_tuples, num_dead_tuples
                    FROM pg_stat_progress_vacuum
                    """
                )
                for r in cur.fetchall():
                    total = int(r["heap_blks_total"] or 0)
                    scanned = int(r["heap_blks_scanned"] or 0)
                    pct = round(scanned * 100.0 / total, 2) if total > 0 else None
                    all_vac.append({
                        "pid": int(r["pid"]),
                        "datname": str(r["datname"]),
                        "relname": str(r["relname"] or ""),
                        "phase": str(r["phase"] or ""),
                        "heap_blks_scanned": scanned,
                        "heap_blks_total": total,
                        "scan_pct": pct,
                        "heap_blks_vacuumed": int(r["heap_blks_vacuumed"] or 0),
                        "index_vacuum_count": int(r["index_vacuum_count"] or 0),
                        "max_dead_tuples": int(r["max_dead_tuples"] or 0),
                        "num_dead_tuples": int(r["num_dead_tuples"] or 0),
                    })

                # ANALYZE 进度(PG 13+)
                try:
                    cur.execute(
                        """
                        SELECT pid, datname, relid::regclass::text AS relname,
                               phase, sample_blks_total, sample_blks_scanned
                        FROM pg_stat_progress_analyze
                        """
                    )
                    for r in cur.fetchall():
                        total = int(r["sample_blks_total"] or 0)
                        scanned = int(r["sample_blks_scanned"] or 0)
                        pct = round(scanned * 100.0 / total, 2) if total > 0 else None
                        all_ana.append({
                            "pid": int(r["pid"]),
                            "datname": str(r["datname"]),
                            "relname": str(r["relname"] or ""),
                            "phase": str(r["phase"] or ""),
                            "sample_blks_scanned": scanned,
                            "sample_blks_total": total,
                            "scan_pct": pct,
                        })
                except psycopg.errors.UndefinedTable:
                    pass  # PG < 13
        except Exception as e:
            db_errors[db] = f"{type(e).__name__}: {str(e)[:200]}"
            continue

    if not all_vac and not all_ana:
        status, rec = "ok", f"扫描 {len(db_list)} 个 db,当前无 VACUUM / ANALYZE 在跑。"
    else:
        status = "ok"  # vacuum 在跑本身是好事,不算 warning
        rec = (
            f"发现 {len(all_vac)} 个 VACUUM、{len(all_ana)} 个 ANALYZE 在跑。"
            "若是手工触发,看 raw_data 进度;若是 autovacuum 救火,关注是否能在 wraparound 前完成。"
        )
    if db_errors:
        rec += f" ⚠ {len(db_errors)} 个 db 扫描失败:{list(db_errors.keys())}"
    if cap_note:
        rec += " " + cap_note

    findings = [
        {
            "severity": "info", "metric": "vacuum_in_progress",
            "table": v["relname"], "datname": v["datname"],
            "phase": v["phase"], "progress": f"{v['scan_pct']:.1f}%" if v["scan_pct"] is not None else "?",
        }
        for v in all_vac
    ] + [
        {
            "severity": "info", "metric": "analyze_in_progress",
            "table": a["relname"], "datname": a["datname"],
            "phase": a["phase"], "progress": f"{a['scan_pct']:.1f}%" if a["scan_pct"] is not None else "?",
        }
        for a in all_ana
    ]

    return _wrap(
        status=status, findings=findings,
        raw_data={
            "cluster_endpoint": endpoint, "queried_at": _now_iso(),
            "scanned_databases": db_list,
            "max_databases": max_databases,
            "scan_capped": cap_note is not None,
            "vacuum_in_progress": all_vac,
            "analyze_in_progress": all_ana,
            "db_errors": db_errors if db_errors else None,
        },
        recommendation=rec,
    )


# ===========================================================================
# Tool 13 — pg_settings 配置 + pending_restart(D1 / B2 / B3 / G1 通用)
# ===========================================================================
@mcp.tool()
def inspect_settings(
    cluster_endpoint: str,
    category: str = "all",
    database: str = "postgres",
) -> dict[str, Any]:
    """巡检关键参数配置 + 有没有"改了但还没重启生效"的参数(pending_restart)— 排查"参数为何不生效"/确认调优是否落地。实例级,无需指定 database。

    Args:
        category: 子集,选 1 个:
          - "all"        所有 DBA 关心的核心参数(下方默认列表)
          - "memory"     shared_buffers / work_mem / maintenance_work_mem / effective_cache_size
          - "autovacuum" autovacuum_* 全集
          - "connection" max_connections / superuser_reserved_connections / idle_in_transaction_session_timeout
          - "wal"        wal_level / max_wal_size / checkpoint_*
          - "replication" max_replication_slots / max_wal_senders / hot_standby_feedback / max_standby_*
          - "logging"    log_min_duration_statement / log_lock_waits / log_temp_files
          - "extensions" shared_preload_libraries / rds.allowed_extensions
    """
    endpoint = _resolve_endpoint(cluster_endpoint)
    valid_cats = {"all", "memory", "autovacuum", "connection", "wal",
                  "replication", "logging", "extensions"}
    if category not in valid_cats:
        raise ValueError(f"category 必须 ∈ {sorted(valid_cats)},收到 {category!r}")

    log.info("[settings] %s category=%s", endpoint, category)

    # 各 category 的参数白名单
    cat_params: dict[str, list[str]] = {
        "memory": [
            "shared_buffers", "work_mem", "maintenance_work_mem",
            "effective_cache_size", "temp_buffers", "wal_buffers",
        ],
        "autovacuum": [
            "autovacuum", "autovacuum_max_workers", "autovacuum_naptime",
            "autovacuum_vacuum_threshold", "autovacuum_vacuum_scale_factor",
            "autovacuum_analyze_threshold", "autovacuum_analyze_scale_factor",
            "autovacuum_freeze_max_age", "autovacuum_multixact_freeze_max_age",
            "autovacuum_vacuum_cost_delay", "autovacuum_vacuum_cost_limit",
        ],
        "connection": [
            "max_connections", "superuser_reserved_connections",
            "idle_in_transaction_session_timeout", "statement_timeout",
            "lock_timeout", "tcp_keepalives_idle", "tcp_keepalives_interval",
        ],
        "wal": [
            "wal_level", "max_wal_size", "min_wal_size",
            "checkpoint_timeout", "checkpoint_completion_target",
            "wal_keep_size", "max_slot_wal_keep_size",
            "rds.logical_replication",
        ],
        "replication": [
            "max_replication_slots", "max_wal_senders",
            "hot_standby", "hot_standby_feedback",
            "max_standby_streaming_delay", "max_standby_archive_delay",
            "wal_receiver_status_interval",
        ],
        "logging": [
            "log_min_duration_statement", "log_lock_waits",
            "log_temp_files", "log_checkpoints",
            "log_autovacuum_min_duration", "log_statement",
        ],
        "extensions": [
            "shared_preload_libraries", "rds.allowed_extensions",
            "rds.extensions",
        ],
    }
    if category == "all":
        # 合并所有 category(deduplicate)
        names = sorted(set(p for params in cat_params.values() for p in params))
    else:
        names = cat_params[category]

    with _connect(endpoint, database) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT name, setting, unit, category, short_desc,
                   context, source, vartype, min_val, max_val,
                   pending_restart, boot_val
            FROM pg_settings
            WHERE name = ANY(%s)
            ORDER BY name
            """,
            (names,),
        )
        settings = [
            {
                "name": r["name"],
                "setting": r["setting"],
                "unit": r["unit"],
                "category": r["category"],
                "short_desc": r["short_desc"],
                "context": r["context"],
                "source": r["source"],
                "vartype": r["vartype"],
                "pending_restart": bool(r["pending_restart"]),
                "default_value": r["boot_val"],
            }
            for r in cur.fetchall()
        ]

        # 全表 pending_restart 项(超出本次 category)
        cur.execute(
            "SELECT name, setting, source FROM pg_settings WHERE pending_restart = true ORDER BY name"
        )
        all_pending = [
            {"name": r["name"], "setting": r["setting"], "source": r["source"]}
            for r in cur.fetchall()
        ]

    pending_in_cat = [s for s in settings if s["pending_restart"]]
    if all_pending:
        status = "warning"
        rec = (
            f"⚠ {len(all_pending)} 个参数已修改但**待重启生效**:"
            f"{[p['name'] for p in all_pending]}。重启窗口要规划。"
        )
    else:
        status = "ok"
        rec = f"返回 {len(settings)} 个 {category} 类参数,无 pending_restart。"

    findings = [
        {
            "severity": "warning", "metric": "pending_restart",
            "name": p["name"], "value": p["setting"], "source": p["source"],
        }
        for p in all_pending
    ]

    return _wrap(
        status=status, findings=findings,
        raw_data={
            "cluster_endpoint": endpoint, "queried_at": _now_iso(),
            "category": category,
            "settings": settings,
            "all_pending_restart": all_pending,
        },
        recommendation=rec,
    )


# ===========================================================================
# Tool 14 — Replication slots(E2 关键 — slot leak 撑爆 WAL)
# ===========================================================================
@mcp.tool()
def inspect_replication_slots(
    cluster_endpoint: str,
    inactive_lag_warning_mb: int = 1024,
    database: str = "postgres",
) -> dict[str, Any]:
    """巡检复制槽(replication slot)状态 + 泄漏检测 — 失活的 slot 会一直占着 WAL 不让清理,撑爆磁盘(RDS 经典磁盘满 incident)。实例级,无需指定 database。

    inactive 的 slot 仍占着 WAL 不让 PG 清理 → 磁盘撑爆。这是 RDS 经典磁盘
    满 incident 之一(随 logical replication / Aurora 物理 standby slot 出现)。

    阈值:inactive slot 累计 lag ≥ inactive_lag_warning_mb 即 warning。
    """
    endpoint = _resolve_endpoint(cluster_endpoint)
    log.info("[replication_slots] %s warn=%dMB", endpoint, inactive_lag_warning_mb)

    with _connect(endpoint, database) as conn, conn.cursor() as cur:
        # 注:RDS PG 16+ 有 wal_status / safe_wal_size 列;旧版本可能没。
        # 用 to_jsonb 把全行 dump 也行,这里挑常见列。
        cur.execute(
            """
            SELECT slot_name, plugin, slot_type, datoid::regclass::text AS dbname,
                   active, active_pid, restart_lsn, confirmed_flush_lsn,
                   xmin, catalog_xmin,
                   pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn) AS lag_bytes,
                   pg_wal_lsn_diff(pg_current_wal_lsn(), confirmed_flush_lsn) AS confirmed_lag_bytes
            FROM pg_replication_slots
            ORDER BY pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn) DESC NULLS LAST
            """
        )
        slots = []
        for r in cur.fetchall():
            lag_b = r["lag_bytes"]
            slots.append({
                "slot_name": r["slot_name"],
                "plugin": str(r["plugin"] or ""),
                "slot_type": str(r["slot_type"] or ""),
                "dbname": str(r["dbname"] or ""),
                "active": bool(r["active"]),
                "active_pid": int(r["active_pid"]) if r["active_pid"] else None,
                "restart_lsn": str(r["restart_lsn"]) if r["restart_lsn"] else None,
                "confirmed_flush_lsn": str(r["confirmed_flush_lsn"]) if r["confirmed_flush_lsn"] else None,
                "lag_bytes": int(lag_b) if lag_b is not None else None,
                "lag_pretty": _bytes_pretty(int(lag_b)) if lag_b is not None else "?",
                "confirmed_lag_bytes": int(r["confirmed_lag_bytes"]) if r["confirmed_lag_bytes"] is not None else None,
            })

        # max_slot_wal_keep_size — 保护性参数
        cur.execute(
            "SELECT name, setting FROM pg_settings "
            "WHERE name IN ('max_slot_wal_keep_size','max_replication_slots','max_wal_senders')"
        )
        protections = {r["name"]: r["setting"] for r in cur.fetchall()}

    inactive_slots = [s for s in slots if not s["active"]]
    inactive_total_lag = sum(s["lag_bytes"] or 0 for s in inactive_slots)
    inactive_total_lag_mb = inactive_total_lag / (1024 * 1024)

    if inactive_total_lag_mb >= inactive_lag_warning_mb:
        status = "critical"
        rec = (
            f"⚠ {len(inactive_slots)} 个 inactive replication slot 累计占 "
            f"{inactive_total_lag_mb:.0f}MB WAL ≥ {inactive_lag_warning_mb}MB,"
            "正在阻止 WAL 清理 → 磁盘可能撑爆。立即:"
            "1) 确认 inactive slot 是否还需要(订阅端断了?)"
            "2) DROP 不需要的 slot:`SELECT pg_drop_replication_slot('<name>');`"
            "3) 设保护:`max_slot_wal_keep_size=<X>` 防 unbounded 增长。"
        )
    elif inactive_slots:
        status = "warning"
        rec = (
            f"{len(inactive_slots)} 个 inactive slot 累计占 {inactive_total_lag_mb:.0f}MB WAL,"
            "关注是否长期不接订阅端。"
        )
    elif slots:
        status = "ok"
        rec = f"{len(slots)} 个 slot 全部 active,WAL 清理正常。"
    else:
        status = "ok"
        rec = "无 replication slot。"

    findings = [
        {
            "severity": "critical" if not s["active"] and (s["lag_bytes"] or 0) >= 100 * 1024 * 1024
                        else "warning" if not s["active"]
                        else "info",
            "metric": "replication_slot",
            "slot_name": s["slot_name"],
            "active": s["active"], "lag": s["lag_pretty"],
            "type": s["slot_type"], "plugin": s["plugin"],
        }
        for s in slots
    ]

    return _wrap(
        status=status, findings=findings,
        raw_data={
            "cluster_endpoint": endpoint, "queried_at": _now_iso(),
            "inactive_lag_warning_mb": inactive_lag_warning_mb,
            "total_slots": len(slots),
            "inactive_count": len(inactive_slots),
            "inactive_total_lag_bytes": inactive_total_lag,
            "protections": protections,
            "slots": slots,
        },
        recommendation=rec,
    )


def _bytes_pretty(n: int) -> str:
    """字节转可读单位(同 Valkey 容器实现)。"""
    nf = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(nf) < 1024:
            return f"{nf:.2f} {unit}"
        nf /= 1024
    return f"{nf:.2f} PB"


# ===========================================================================
# Tool 15 — Logical replication(E2 publication / subscription)
# ===========================================================================
@mcp.tool()
def inspect_logical_replication(
    cluster_endpoint: str,
    database: str = "",
    scan_all_databases: bool = True,
    max_databases: int = _MAX_SCAN_DATABASES_DEFAULT,
) -> dict[str, Any]:
    """巡检 logical replication 配置:publication / subscription 及订阅状态(有没有 disabled 的订阅)。自动扫全 cluster 各库(db 数超 max_databases 截断),无需指定 database。

    publication 和 subscription 都是 per-database 概念,需要扫各 db。
    """
    endpoint = _resolve_endpoint(cluster_endpoint)

    db_list, cap_note = _resolve_scan_db_list(
        endpoint, database=database,
        scan_all_databases=scan_all_databases, max_databases=max_databases,
    )

    log.info("[logical_replication] %s scanning %d db(s)", endpoint, len(db_list))

    publications: list[dict[str, Any]] = []
    subscriptions: list[dict[str, Any]] = []
    db_errors: dict[str, str] = {}

    for db in db_list:
        try:
            with _connect(endpoint, db) as conn, conn.cursor() as cur:
                # publications
                cur.execute(
                    """
                    SELECT pubname, puballtables, pubinsert, pubupdate, pubdelete, pubtruncate
                    FROM pg_publication
                    """
                )
                for r in cur.fetchall():
                    publications.append({
                        "database": db,
                        "pubname": r["pubname"],
                        "all_tables": bool(r["puballtables"]),
                        "insert": bool(r["pubinsert"]),
                        "update": bool(r["pubupdate"]),
                        "delete": bool(r["pubdelete"]),
                        "truncate": bool(r["pubtruncate"]),
                    })

                # subscriptions(注:pg_subscription 需要 superuser 或 pg_read_all_settings;
                # mcp_devops_ro 有 pg_monitor 应该够看)
                try:
                    cur.execute(
                        """
                        SELECT s.subname,
                               s.subenabled,
                               s.subconninfo,
                               s.subslotname,
                               s.subpublications
                        FROM pg_subscription s
                        WHERE s.subdbid = (SELECT oid FROM pg_database WHERE datname = current_database())
                        """
                    )
                    for r in cur.fetchall():
                        # subconninfo 含密码,绝不暴露(SHALL NOT #12)
                        # 只露 host/dbname/user(strip password)
                        conn_info = str(r["subconninfo"] or "")
                        safe_conn = " ".join(
                            kv for kv in conn_info.split()
                            if not kv.lower().startswith(("password=", "passfile="))
                        )
                        subscriptions.append({
                            "database": db,
                            "subname": r["subname"],
                            "enabled": bool(r["subenabled"]),
                            "conninfo_safe": safe_conn[:300],
                            "slot_name": str(r["subslotname"] or ""),
                            "publications": list(r["subpublications"] or []),
                        })

                    # subscription 状态
                    cur.execute(
                        """
                        SELECT s.subname,
                               st.received_lsn::text AS received_lsn,
                               st.last_msg_send_time, st.last_msg_receipt_time,
                               st.latest_end_lsn::text AS latest_end_lsn
                        FROM pg_stat_subscription st
                        JOIN pg_subscription s ON s.oid = st.subid
                        WHERE s.subdbid = (SELECT oid FROM pg_database WHERE datname = current_database())
                        """
                    )
                    sub_status = {r["subname"]: r for r in cur.fetchall()}
                    for sub in subscriptions:
                        if sub["database"] != db:
                            continue
                        st = sub_status.get(sub["subname"])
                        if st:
                            sub["received_lsn"] = st["received_lsn"]
                            sub["last_msg_send_time"] = st["last_msg_send_time"].isoformat() if st["last_msg_send_time"] else None
                            sub["last_msg_receipt_time"] = st["last_msg_receipt_time"].isoformat() if st["last_msg_receipt_time"] else None
                except psycopg.errors.InsufficientPrivilege as e:
                    db_errors[db] = f"InsufficientPrivilege on pg_subscription: {str(e)[:100]}"

        except Exception as e:
            db_errors[db] = f"{type(e).__name__}: {str(e)[:200]}"
            continue

    if not publications and not subscriptions:
        status, rec = "ok", "未找到 publication / subscription(本 cluster 未启用 logical replication 或权限不足)。"
    else:
        disabled_subs = [s for s in subscriptions if not s["enabled"]]
        if disabled_subs:
            status = "warning"
            rec = (
                f"发现 {len(disabled_subs)} 个 subscription 处于 disabled 状态。"
                f"共 {len(publications)} 个 pub / {len(subscriptions)} 个 sub。"
            )
        else:
            status = "ok"
            rec = f"{len(publications)} 个 pub / {len(subscriptions)} 个 sub,全部 enabled。"

    if db_errors:
        rec += f" ⚠ {len(db_errors)} 个 db 扫描失败:{list(db_errors.keys())}"
    if cap_note:
        rec += " " + cap_note
    findings: list[dict[str, Any]] = []
    for p in publications:
        findings.append({
            "severity": "info", "metric": "publication",
            "database": p["database"], "pubname": p["pubname"],
            "all_tables": p["all_tables"],
        })
    for s in subscriptions:
        findings.append({
            "severity": "warning" if not s["enabled"] else "info",
            "metric": "subscription",
            "database": s["database"], "subname": s["subname"],
            "enabled": s["enabled"], "slot_name": s["slot_name"],
        })

    return _wrap(
        status=status, findings=findings,
        raw_data={
            "cluster_endpoint": endpoint, "queried_at": _now_iso(),
            "scanned_databases": db_list,
            "max_databases": max_databases,
            "scan_capped": cap_note is not None,
            "publications": publications,
            "subscriptions": subscriptions,
            "db_errors": db_errors if db_errors else None,
        },
        recommendation=rec,
    )


# ===========================================================================
# Tool 16 — Extensions(G1)
# ===========================================================================
@mcp.tool()
def inspect_extensions(
    cluster_endpoint: str,
    database: str = "postgres",
) -> dict[str, Any]:
    """巡检已装 / 可装 extensions + shared_preload_libraries + rds.allowed_extensions(排查"某扩展为何不可用"/确认 pg_stat_statements 等是否就位)。⚠ extension 是 database 级:排查业务库请传 database=业务库名。"""
    endpoint = _resolve_endpoint(cluster_endpoint)
    log.info("[extensions] %s db=%s", endpoint, database)

    with _connect(endpoint, database) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT extname, extversion FROM pg_extension ORDER BY extname"
        )
        installed = [
            {"name": r["extname"], "version": r["extversion"]}
            for r in cur.fetchall()
        ]

        cur.execute(
            """
            SELECT name, default_version, comment
            FROM pg_available_extensions
            WHERE installed_version IS NULL
            ORDER BY name
            """
        )
        available = [
            {"name": r["name"], "default_version": r["default_version"],
             "comment": (r["comment"] or "")[:200]}
            for r in cur.fetchall()
        ]

        # shared_preload_libraries / rds.allowed_extensions
        cur.execute(
            "SELECT name, setting FROM pg_settings "
            "WHERE name IN ('shared_preload_libraries', 'rds.allowed_extensions')"
        )
        config = {r["name"]: r["setting"] for r in cur.fetchall()}

    spl = config.get("shared_preload_libraries", "") or ""
    spl_list = [s.strip() for s in spl.split(",") if s.strip()]
    rds_allowed = config.get("rds.allowed_extensions", "") or ""

    # 关键检查:pg_stat_statements 是否在 SPL 同时已 CREATE EXTENSION
    pgss_in_spl = "pg_stat_statements" in spl_list
    pgss_installed = any(e["name"] == "pg_stat_statements" for e in installed)

    findings: list[dict[str, Any]] = []
    rec_parts: list[str] = []

    if pgss_in_spl and not pgss_installed:
        findings.append({
            "severity": "warning", "metric": "extension_not_created",
            "name": "pg_stat_statements",
            "note": "在 shared_preload_libraries 但未 CREATE EXTENSION",
        })
        rec_parts.append(
            "pg_stat_statements 在 SPL 但 db 内未 CREATE EXTENSION。"
            "跑 `CREATE EXTENSION pg_stat_statements;`"
        )
    if not pgss_in_spl:
        findings.append({
            "severity": "warning", "metric": "missing_in_spl",
            "name": "pg_stat_statements",
            "note": "未在 shared_preload_libraries — inspect_top_queries 不可用",
        })
        rec_parts.append(
            "pg_stat_statements 未在 shared_preload_libraries。"
            "在 RDS parameter group 加 `pg_stat_statements`,重启实例。"
        )

    status = "warning" if findings else "ok"
    if not rec_parts:
        rec_parts.append(
            f"{len(installed)} 个 extensions 已装。pg_stat_statements 就位。"
        )

    return _wrap(
        status=status, findings=findings,
        raw_data={
            "cluster_endpoint": endpoint, "database": database,
            "queried_at": _now_iso(),
            "installed": installed,
            "available_not_installed": available[:50],  # 不全列,通常很多
            "shared_preload_libraries": spl_list,
            "rds_allowed_extensions": rds_allowed,
        },
        recommendation=" ".join(rec_parts),
    )


# ===========================================================================
# Tool 18 — Wait events(Z1 / B3 性能根因)
# ===========================================================================
@mcp.tool()
def inspect_wait_events(
    cluster_endpoint: str,
    top_n: int = _TOP_N_DEFAULT,
    database: str = "postgres",
) -> dict[str, Any]:
    """巡检 wait event 分布(连接都在等什么:锁 / IO / CPU)— 性能排查第一步,快速定位瓶颈方向再用专项工具深入。实例级:看到所有库的活动,database 参数不影响结果。

    PG 性能问题 90% 通过看 wait_event 主导分布快速定位:
      - Lock / LWLock      → 锁竞争(转 inspect_blocking_chains)
      - IO / DataFileRead  → 磁盘 / IOPS / shared_buffers 不够(转 inspect_cache_hit_ratio)
      - Client / IPC       → 客户端 idle / 网络 / 应用逻辑
      - CPU(active 但无 wait) → 真 CPU 瓶颈(转 inspect_top_queries)
    """
    endpoint = _resolve_endpoint(cluster_endpoint)
    n = max(1, min(top_n, 100))
    log.info("[wait_events] %s top=%d", endpoint, n)

    with _connect(endpoint, database) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(wait_event_type, '<no_wait>') AS wait_event_type,
                   COALESCE(wait_event, '<no_wait>') AS wait_event,
                   state,
                   count(*) AS conn_count
            FROM pg_stat_activity
            WHERE backend_type = 'client backend'
              AND state IS NOT NULL
            GROUP BY wait_event_type, wait_event, state
            ORDER BY conn_count DESC
            LIMIT %s
            """,
            (n,),
        )
        events = [
            {
                "wait_event_type": str(r["wait_event_type"]),
                "wait_event": str(r["wait_event"]),
                "state": str(r["state"] or ""),
                "conn_count": int(r["conn_count"]),
            }
            for r in cur.fetchall()
        ]

        # 总活跃连接 - active 状态
        cur.execute(
            "SELECT count(*) AS active "
            "FROM pg_stat_activity WHERE backend_type = 'client backend' AND state = 'active'"
        )
        total_active = int(cur.fetchone()["active"])

    # 聚合到 wait_event_type 维度
    type_agg: dict[str, int] = {}
    for e in events:
        t = e["wait_event_type"]
        type_agg[t] = type_agg.get(t, 0) + e["conn_count"]
    type_top = sorted(type_agg.items(), key=lambda x: x[1], reverse=True)

    if not events:
        status, rec = "ok", "无活跃连接。"
    else:
        top_type, top_count = type_top[0]
        if top_type == "Lock":
            status = "warning"
            rec = (f"Lock 是 top wait type({top_count} 个连接在等),"
                   "建议 `inspect_blocking_chains` 找阻塞源。")
        elif top_type in ("IO", "Buffer", "DataFileRead"):
            status = "warning"
            rec = (f"{top_type} 是 top wait type({top_count} 个连接在等),"
                   "建议 `inspect_cache_hit_ratio` 看 buffer 命中率;"
                   "持续高需调大 shared_buffers 或加 IOPS。")
        elif top_type == "<no_wait>":
            status = "ok"
            rec = (f"top {top_count} 个连接 active 且无 wait,纯 CPU bound — "
                   "建议 `inspect_top_queries` 找慢 SQL。")
        else:
            status = "ok"
            rec = f"top wait type = {top_type}({top_count} 个连接)。"

    findings = [
        {
            "severity": "info", "metric": "wait_event",
            "wait_event_type": e["wait_event_type"],
            "wait_event": e["wait_event"],
            "state": e["state"], "count": e["conn_count"],
        }
        for e in events[:15]
    ]

    return _wrap(
        status=status, findings=findings,
        raw_data={
            "cluster_endpoint": endpoint, "database": database,
            "queried_at": _now_iso(),
            "total_active_connections": total_active,
            "top_wait_event_types": [{"type": t, "count": c} for t, c in type_top],
            "events": events,
        },
        recommendation=rec,
    )


# ===========================================================================
# Tool 19 — Cache hit ratio(B3)
# ===========================================================================
@mcp.tool()
def inspect_cache_hit_ratio(
    cluster_endpoint: str,
    database: str = "postgres",
    top_n: int = _TOP_N_DEFAULT,
) -> dict[str, Any]:
    """巡检 shared_buffer 缓存命中率(全库 + 命中率最低的 top N 表),命中率低说明 shared_buffers 不够或有热点表频繁读盘。⚠ database 级:排查业务库请传 database=业务库名(默认 postgres)。

    PG 期待 cache hit ratio >= 95%;低于 90% 通常意味着 shared_buffers 不够大,
    或者有热点表频繁从磁盘读。
    """
    endpoint = _resolve_endpoint(cluster_endpoint)
    n = max(1, min(top_n, 100))
    log.info("[cache_hit_ratio] %s db=%s top=%d", endpoint, database, n)

    with _connect(endpoint, database) as conn, conn.cursor() as cur:
        # 全 db 总计
        cur.execute(
            """
            SELECT
                sum(heap_blks_hit) AS hit, sum(heap_blks_read) AS read,
                sum(idx_blks_hit) AS idx_hit, sum(idx_blks_read) AS idx_read
            FROM pg_statio_user_tables
            """
        )
        total = cur.fetchone()
        heap_hit = int(total["hit"] or 0)
        heap_read = int(total["read"] or 0)
        idx_hit = int(total["idx_hit"] or 0)
        idx_read = int(total["idx_read"] or 0)
        heap_ratio = (heap_hit / max(heap_hit + heap_read, 1)) * 100 if (heap_hit + heap_read) > 0 else None
        idx_ratio = (idx_hit / max(idx_hit + idx_read, 1)) * 100 if (idx_hit + idx_read) > 0 else None

        # per-table 命中率最低 top N(只看读量足够的表 — 至少 1000 reads)
        cur.execute(
            """
            SELECT schemaname, relname,
                   heap_blks_hit, heap_blks_read,
                   ROUND((heap_blks_hit::numeric / NULLIF(heap_blks_hit + heap_blks_read, 0)) * 100, 2) AS hit_ratio
            FROM pg_statio_user_tables
            WHERE heap_blks_hit + heap_blks_read > 1000
            ORDER BY (heap_blks_hit::numeric / NULLIF(heap_blks_hit + heap_blks_read, 0)) ASC NULLS LAST
            LIMIT %s
            """,
            (n,),
        )
        worst_tables = [
            {
                "schemaname": r["schemaname"], "relname": r["relname"],
                "heap_blks_hit": int(r["heap_blks_hit"]),
                "heap_blks_read": int(r["heap_blks_read"]),
                "hit_ratio_pct": float(r["hit_ratio"] or 0),
            }
            for r in cur.fetchall()
        ]

    if heap_ratio is None:
        status, rec = "warning", "pg_statio_user_tables 无数据(可能刚启动 / stat reset)。"
    elif heap_ratio < 90:
        status = "critical"
        rec = (f"heap cache hit ratio = {heap_ratio:.2f}% < 90%。"
               "建议:1) 调大 shared_buffers(parameter group);"
               "2) 看 worst_tables 是否需要业务层缓存或访问模式优化。")
    elif heap_ratio < 95:
        status = "warning"
        rec = f"heap cache hit ratio = {heap_ratio:.2f}%(目标 ≥ 95%)。"
    else:
        status = "ok"
        rec = f"heap cache hit ratio = {heap_ratio:.2f}%,优秀。"

    findings: list[dict[str, Any]] = []
    if heap_ratio is not None and heap_ratio < 95:
        findings.append({
            "severity": "critical" if heap_ratio < 90 else "warning",
            "metric": "global_heap_hit_ratio",
            "value": f"{heap_ratio:.2f}%", "threshold": "95%",
        })
    for t in worst_tables[:5]:
        if t["hit_ratio_pct"] < 90:
            findings.append({
                "severity": "warning", "metric": "table_low_hit_ratio",
                "table": f"{t['schemaname']}.{t['relname']}",
                "ratio": f"{t['hit_ratio_pct']:.1f}%",
            })

    return _wrap(
        status=status, findings=findings,
        raw_data={
            "cluster_endpoint": endpoint, "database": database,
            "queried_at": _now_iso(),
            "global_heap_hit_pct": round(heap_ratio, 2) if heap_ratio is not None else None,
            "global_index_hit_pct": round(idx_ratio, 2) if idx_ratio is not None else None,
            "global_heap_hit": heap_hit, "global_heap_read": heap_read,
            "global_idx_hit": idx_hit, "global_idx_read": idx_read,
            "worst_tables": worst_tables,
        },
        recommendation=rec,
    )


# ===========================================================================
# Tool 19 — pg_stat_replication(E1 标准 PG 流复制,通用 — 非 Aurora-only)
# ===========================================================================
@mcp.tool()
def inspect_pg_stat_replication(
    cluster_endpoint: str,
    database: str = "postgres",
) -> dict[str, Any]:
    """巡检标准 PG 流复制状态(replica 的 write/flush/replay 延迟)— 用于标准 RDS PG / 自建 PG。Aurora 集群请改用 inspect_replica_lag。实例级,无需指定 database。

    与 `inspect_replica_lag`(Aurora 专用 `aurora_replica_status()`)互补:
      - 标准 PG / RDS PG(非 Aurora):用本 tool
      - Aurora MySQL/PG:用 `inspect_replica_lag`(它走 aurora_replica_status())
    """
    endpoint = _resolve_endpoint(cluster_endpoint)
    log.info("[pg_stat_replication] %s db=%s", endpoint, database)

    with _connect(endpoint, database) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT pid, usename, application_name,
                   client_addr::text AS client_addr,
                   state, sync_state,
                   sent_lsn::text AS sent_lsn,
                   write_lsn::text AS write_lsn,
                   flush_lsn::text AS flush_lsn,
                   replay_lsn::text AS replay_lsn,
                   pg_wal_lsn_diff(sent_lsn, write_lsn) AS write_lag_bytes,
                   pg_wal_lsn_diff(sent_lsn, flush_lsn) AS flush_lag_bytes,
                   pg_wal_lsn_diff(sent_lsn, replay_lsn) AS replay_lag_bytes,
                   write_lag, flush_lag, replay_lag
            FROM pg_stat_replication
            ORDER BY pg_wal_lsn_diff(sent_lsn, replay_lsn) DESC NULLS LAST
            """
        )
        replicas = []
        for r in cur.fetchall():
            replicas.append({
                "pid": int(r["pid"]),
                "usename": str(r["usename"] or ""),
                "application_name": str(r["application_name"] or ""),
                "client_addr": str(r["client_addr"]) if r["client_addr"] else None,
                "state": str(r["state"] or ""),
                "sync_state": str(r["sync_state"] or ""),
                "sent_lsn": r["sent_lsn"],
                "replay_lsn": r["replay_lsn"],
                "write_lag_bytes": int(r["write_lag_bytes"]) if r["write_lag_bytes"] is not None else None,
                "flush_lag_bytes": int(r["flush_lag_bytes"]) if r["flush_lag_bytes"] is not None else None,
                "replay_lag_bytes": int(r["replay_lag_bytes"]) if r["replay_lag_bytes"] is not None else None,
                "replay_lag_pretty": _bytes_pretty(int(r["replay_lag_bytes"])) if r["replay_lag_bytes"] is not None else "?",
                "write_lag_seconds": float(r["write_lag"].total_seconds()) if r["write_lag"] else None,
                "replay_lag_seconds": float(r["replay_lag"].total_seconds()) if r["replay_lag"] else None,
            })

    if not replicas:
        # 可能本 endpoint 是 Aurora 或者是 standby — 看 pg_is_in_recovery
        with _connect(endpoint, database) as conn, conn.cursor() as cur:
            cur.execute("SELECT pg_is_in_recovery() AS in_recovery")
            in_rec = bool(cur.fetchone()["in_recovery"])
        if in_rec:
            return _wrap(
                status="warning", findings=[],
                raw_data={
                    "cluster_endpoint": endpoint, "queried_at": _now_iso(),
                    "in_recovery": True, "replicas": [],
                },
                recommendation=(
                    "本 endpoint 是 standby/replica,无法看 pg_stat_replication"
                    "(只在 primary/writer 上才有数据)。如果是 Aurora,请用 inspect_replica_lag。"
                ),
            )
        return _wrap(
            status="ok", findings=[],
            raw_data={"cluster_endpoint": endpoint, "queried_at": _now_iso(), "replicas": []},
            recommendation="primary 上无 streaming replica(可能是 standalone / 或 replica 未连接)。",
        )

    max_replay_seconds = max((r["replay_lag_seconds"] or 0 for r in replicas), default=0.0)
    if max_replay_seconds >= 5:
        status = "critical"
        rec = f"replica 最大 replay lag {max_replay_seconds:.1f}s ≥ 5s,严重落后。"
    elif max_replay_seconds >= 1:
        status = "warning"
        rec = f"replica 最大 replay lag {max_replay_seconds:.1f}s ≥ 1s。"
    else:
        status = "ok"
        rec = f"流复制健康,{len(replicas)} 个 replica,最大 replay lag {max_replay_seconds:.2f}s。"

    findings = [
        {
            "severity": "critical" if (r["replay_lag_seconds"] or 0) >= 5
                        else "warning" if (r["replay_lag_seconds"] or 0) >= 1
                        else "info",
            "metric": "stream_replica_lag",
            "client_addr": r["client_addr"], "state": r["state"],
            "sync_state": r["sync_state"],
            "replay_lag": r["replay_lag_pretty"],
            "replay_lag_s": r["replay_lag_seconds"],
        }
        for r in replicas
    ]

    return _wrap(
        status=status, findings=findings,
        raw_data={
            "cluster_endpoint": endpoint, "queried_at": _now_iso(),
            "replicas": replicas,
        },
        recommendation=rec,
    )


# ===========================================================================
# Tool 20 — Database sizes(F1 容量规划)
# ===========================================================================
@mcp.tool()
def inspect_database_sizes(
    cluster_endpoint: str,
) -> dict[str, Any]:
    """巡检全 cluster 各 database 的大小 + 总容量 — 容量规划 / 找占空间最多的库。实例级:自动列出所有库,无需指定 database。"""
    endpoint = _resolve_endpoint(cluster_endpoint)
    log.info("[database_sizes] %s", endpoint)

    with _connect(endpoint, "postgres") as conn, conn.cursor() as cur:
        # 只对"当前用户有 CONNECT 权限"的库算大小:rdsadmin 等 RDS 内部管理库
        # 任何普通用户(含 master)都无权访问,pg_database_size() 会报错 —— 这是
        # RDS 托管服务的硬限制,不是权限配错。用 has_database_privilege 过滤掉,
        # 无权访问的库 size 记为 NULL 并单独列出(skipped),不让整个工具失败。
        cur.execute(
            """
            SELECT datname,
                   has_database_privilege(current_user, datname, 'CONNECT') AS can_connect,
                   CASE WHEN has_database_privilege(current_user, datname, 'CONNECT')
                        THEN pg_database_size(datname) END AS size_bytes,
                   CASE WHEN has_database_privilege(current_user, datname, 'CONNECT')
                        THEN pg_size_pretty(pg_database_size(datname)) END AS size_pretty,
                   pg_get_userbyid(datdba) AS owner,
                   datistemplate AS is_template
            FROM pg_database
            ORDER BY
                has_database_privilege(current_user, datname, 'CONNECT') DESC,
                CASE WHEN has_database_privilege(current_user, datname, 'CONNECT')
                     THEN pg_database_size(datname) ELSE 0 END DESC
            """
        )
        databases = []
        skipped: list[str] = []
        for r in cur.fetchall():
            if not r["can_connect"]:
                skipped.append(r["datname"])
                continue
            databases.append({
                "datname": r["datname"],
                "size_bytes": int(r["size_bytes"]),
                "size_pretty": r["size_pretty"],
                "owner": r["owner"],
                "is_template": bool(r["is_template"]),
            })

    total_bytes = sum(d["size_bytes"] for d in databases if not d["is_template"])
    total_gb = total_bytes / (1024 ** 3)

    if total_gb >= 1000:
        status = "warning"
        rec = f"cluster 总大小 {total_gb:.1f} GB(≥ 1TB),关注 storage 成本 + 备份时长。"
    elif total_gb >= 500:
        status = "warning"
        rec = f"cluster 总大小 {total_gb:.1f} GB,接近 storage 容量上限,关注。"
    else:
        status = "ok"
        rec = f"cluster 总大小 {total_gb:.2f} GB,健康。"

    if skipped:
        rec += (f" (跳过无 CONNECT 权限的内部库:{skipped} —— RDS 托管库如 rdsadmin "
                "任何用户都无法访问,属正常。)")

    findings = [
        {
            "severity": "info", "metric": "database_size",
            "datname": d["datname"], "size": d["size_pretty"],
            "is_template": d["is_template"],
        }
        for d in databases[:10]
    ]

    return _wrap(
        status=status, findings=findings,
        raw_data={
            "cluster_endpoint": endpoint, "queried_at": _now_iso(),
            "cluster_total_bytes": total_bytes,
            "cluster_total_gb": round(total_gb, 2),
            "databases": databases,
            "skipped_no_connect": skipped if skipped else None,
        },
        recommendation=rec,
    )


# ===========================================================================
# Tool 21 — Index bloat(索引膨胀,inspect_table_bloat 的盲区补充)
# ===========================================================================
@mcp.tool()
def inspect_index_bloat(
    cluster_endpoint: str,
    database: str = "postgres",
    min_index_size_mb: int = 10,
    bloat_pct_threshold: float = 30.0,
    top_n: int = _TOP_N_DEFAULT,
) -> dict[str, Any]:
    """巡检索引膨胀(B-tree 索引页变稀疏、比实际数据大几倍,拖慢查询+占磁盘)。补 inspect_table_bloat 只看表不看索引的盲区。⚠ database 级:排查业务库请传 database=业务库名(默认 postgres)。

    `inspect_table_bloat` 只看表的 dead tuple;索引膨胀是独立问题:
    频繁 UPDATE/DELETE 会让 B-tree 索引页变稀疏,索引比实际数据大几倍,
    拖慢查询 + 占大量磁盘。表不大但索引巨大 / REINDEX 后变快,就是这个。

    用标准 B-tree 膨胀估算(基于 pg_class.relpages 与 tuple 估算页数对比,
    不依赖 pgstattuple extension —— 那个要全索引扫描,太重)。估算有 ±20%
    误差,但足够识别"明显膨胀"的索引。

    Args:
        min_index_size_mb: 只看 ≥ 此大小的索引(默认 10MB,小索引膨胀无意义)
        bloat_pct_threshold: 膨胀率 ≥ 此值入 findings(默认 30%)
    """
    endpoint = _resolve_endpoint(cluster_endpoint)
    n = max(1, min(top_n, _QUERY_LIMIT))
    if not (0 < bloat_pct_threshold <= 100):
        raise ValueError("bloat_pct_threshold 必须 ∈ (0, 100]")
    log.info("[index_bloat] %s db=%s min=%dMB thr=%.1f%%",
             endpoint, database, min_index_size_mb, bloat_pct_threshold)

    min_bytes = min_index_size_mb * 1024 * 1024

    # 标准 B-tree 膨胀估算 SQL(社区经典做法,基于统计信息估算理想页数)。
    # 不用 pgstattuple(要全扫索引,违反"不卡库"原则)。
    with _connect(endpoint, database) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT schemaname, tablename, indexname,
                   index_bytes,
                   pg_size_pretty(index_bytes) AS index_size_pretty,
                   CASE WHEN index_bytes = 0 THEN 0
                        ELSE ROUND(100.0 * (index_bytes - est_ideal_bytes)
                             / NULLIF(index_bytes, 0), 1)
                   END AS bloat_pct,
                   pg_size_pretty(GREATEST(index_bytes - est_ideal_bytes, 0)) AS wasted_pretty,
                   idx_scan
            FROM (
                SELECT
                    n.nspname AS schemaname,
                    ct.relname AS tablename,
                    ci.relname AS indexname,
                    pg_relation_size(ci.oid) AS index_bytes,
                    -- 理想页数估算:tuple 数 * (索引行宽 + 开销) / 可用页空间
                    ceil(
                        ci.reltuples * (
                            -- 估算每个索引 entry 的字节宽度(键宽 + item pointer 开销)
                            COALESCE(SUM(s.avg_width), 8) + 8
                        ) / (current_setting('block_size')::numeric * 0.9)
                    )::bigint * current_setting('block_size')::bigint AS est_ideal_bytes,
                    COALESCE(psi.idx_scan, 0) AS idx_scan
                FROM pg_index i
                JOIN pg_class ci ON ci.oid = i.indexrelid
                JOIN pg_class ct ON ct.oid = i.indrelid
                JOIN pg_namespace n ON n.oid = ci.relnamespace
                LEFT JOIN pg_stats s
                    ON s.schemaname = n.nspname AND s.tablename = ct.relname
                LEFT JOIN pg_stat_user_indexes psi ON psi.indexrelid = ci.oid
                WHERE ci.relkind = 'i'
                  AND i.indisvalid
                  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
                  AND pg_relation_size(ci.oid) >= %s
                GROUP BY n.nspname, ct.relname, ci.relname, ci.oid, ci.reltuples, psi.idx_scan
            ) sub
            ORDER BY bloat_pct DESC NULLS LAST
            LIMIT %s
            """,
            (min_bytes, n),
        )
        rows = cur.fetchall()

    indexes = [
        {
            "schemaname": r["schemaname"], "tablename": r["tablename"],
            "indexname": r["indexname"],
            "index_size_bytes": int(r["index_bytes"]),
            "index_size_pretty": r["index_size_pretty"],
            "bloat_pct_estimate": float(r["bloat_pct"] or 0),
            "wasted_estimate_pretty": r["wasted_pretty"],
            "idx_scan": int(r["idx_scan"] or 0),
        }
        for r in rows
    ]

    bloated = [ix for ix in indexes if ix["bloat_pct_estimate"] >= bloat_pct_threshold]

    if not bloated:
        status = "ok"
        rec = (f"扫描 ≥{min_index_size_mb}MB 的索引,无估算膨胀率 ≥ {bloat_pct_threshold}% 的。"
               "(注:估算法有 ±20% 误差,精确值需 pgstattuple。)")
    else:
        worst = bloated[0]
        max_bloat = worst["bloat_pct_estimate"]
        if max_bloat >= 50:
            status = "critical"
        else:
            status = "warning"
        rec = (
            f"发现 {len(bloated)} 个索引估算膨胀率 ≥ {bloat_pct_threshold}%,"
            f"最高 {worst['indexname']}({max_bloat:.0f}%,浪费约 {worst['wasted_estimate_pretty']})。"
            "建议:REINDEX INDEX CONCURRENTLY <索引名>(在线重建,不锁表);"
            "若索引 idx_scan=0 且膨胀,考虑直接 DROP。"
            "(膨胀率为估算值,REINDEX 前可用 pgstattuple 精确确认。)"
        )

    findings = [
        {
            "severity": "critical" if ix["bloat_pct_estimate"] >= 50 else "warning",
            "metric": "index_bloat",
            "index": f"{ix['schemaname']}.{ix['indexname']}",
            "table": ix["tablename"],
            "size": ix["index_size_pretty"],
            "bloat_pct": f"{ix['bloat_pct_estimate']:.0f}%",
            "wasted": ix["wasted_estimate_pretty"],
            "idx_scan": ix["idx_scan"],
        }
        for ix in bloated[:15]
    ]

    return _wrap(
        status=status, findings=findings,
        raw_data={
            "cluster_endpoint": endpoint, "database": database,
            "queried_at": _now_iso(),
            "min_index_size_mb": min_index_size_mb,
            "bloat_pct_threshold": bloat_pct_threshold,
            "estimation_method": "statistical (relpages vs estimated ideal); ±20% error, not pgstattuple",
            "indexes": indexes,
        },
        recommendation=rec,
    )


# ===========================================================================
# Tool 22 — Autovacuum tuning(bloat 根因层:为什么 autovacuum 跟不上)
# ===========================================================================
@mcp.tool()
def inspect_autovacuum_tuning(
    cluster_endpoint: str,
    database: str = "postgres",
    top_n: int = _TOP_N_DEFAULT,
) -> dict[str, Any]:
    """巡检 autovacuum 配置健康度,回答"表为什么会膨胀"的根因(autovacuum 是否开、触发阈值是否合理、是否跟不上)。配合 inspect_table_bloat 用。⚠ database 级:排查业务库请传 database=业务库名(默认 postgres)。

    `inspect_table_bloat` 告诉你哪些表膨胀,`inspect_vacuum_progress` 告诉你
    vacuum 跑到哪;本 tool 补的是**根因**:autovacuum 触发阈值 vs 实际 dead
    tuple,找出"dead tuple 已堆积但还没到触发线 / 触发了但跟不上"的表,
    以及全局 autovacuum 参数是否需要调。

    触发公式(PG 默认):
      autovacuum 触发阈值 = autovacuum_vacuum_threshold(默认 50)
                          + autovacuum_vacuum_scale_factor(默认 0.2)* n_live_tup
    大表用默认 0.2 意味着要积累 20% dead tuple 才触发 → 大表必膨胀。
    """
    endpoint = _resolve_endpoint(cluster_endpoint)
    n = max(1, min(top_n, _QUERY_LIMIT))
    log.info("[autovacuum_tuning] %s db=%s top=%d", endpoint, database, n)

    with _connect(endpoint, database) as conn, conn.cursor() as cur:
        # 全局 autovacuum 关键参数
        cur.execute(
            """
            SELECT name, setting
            FROM pg_settings
            WHERE name IN (
                'autovacuum', 'autovacuum_max_workers', 'autovacuum_naptime',
                'autovacuum_vacuum_threshold', 'autovacuum_vacuum_scale_factor',
                'autovacuum_vacuum_cost_delay', 'autovacuum_vacuum_cost_limit',
                'autovacuum_analyze_threshold', 'autovacuum_analyze_scale_factor'
            )
            """
        )
        gsettings = {r["name"]: r["setting"] for r in cur.fetchall()}

        g_threshold = float(gsettings.get("autovacuum_vacuum_threshold", 50))
        g_scale = float(gsettings.get("autovacuum_vacuum_scale_factor", 0.2))

        # 每张表:dead tuple、距触发还差多少、是否有 per-table 参数覆盖
        cur.execute(
            """
            SELECT schemaname, relname,
                   n_live_tup, n_dead_tup, n_mod_since_analyze,
                   last_autovacuum, last_autoanalyze,
                   autovacuum_count, analyze_count
            FROM pg_stat_user_tables
            WHERE n_dead_tup > 0
            ORDER BY n_dead_tup DESC
            LIMIT %s
            """,
            (n,),
        )
        rows = cur.fetchall()

    tables: list[dict[str, Any]] = []
    for r in rows:
        live = int(r["n_live_tup"] or 0)
        dead = int(r["n_dead_tup"] or 0)
        # 该表的 autovacuum 触发阈值(用全局参数估算,未考虑 per-table override
        # —— per-table override 需要查 pg_class.reloptions,这里给全局估算足够定位)
        trigger_at = g_threshold + g_scale * live
        pct_to_trigger = (dead / trigger_at * 100) if trigger_at > 0 else 0
        tables.append({
            "schemaname": r["schemaname"], "relname": r["relname"],
            "n_live_tup": live, "n_dead_tup": dead,
            "n_mod_since_analyze": int(r["n_mod_since_analyze"] or 0),
            "estimated_trigger_threshold": int(trigger_at),
            "dead_vs_trigger_pct": round(pct_to_trigger, 1),
            "autovacuum_count": int(r["autovacuum_count"] or 0),
            "last_autovacuum": r["last_autovacuum"].isoformat() if r["last_autovacuum"] else None,
        })

    # 找"dead tuple 已超过触发阈值但仍没被 vacuum 干净"的表(autovacuum 跟不上)
    lagging = [t for t in tables if t["dead_vs_trigger_pct"] >= 100]
    # 大表用默认 scale_factor(0.2)→ 触发太晚的隐患
    big_default_scale = [
        t for t in tables
        if t["n_live_tup"] >= 1_000_000 and g_scale >= 0.1
    ]

    findings: list[dict[str, Any]] = []
    rec_parts: list[str] = []
    status = "ok"

    if gsettings.get("autovacuum") == "off":
        status = "critical"
        rec_parts.append("⚠ autovacuum 全局 OFF!表会无限膨胀 + xid wraparound 风险,立即开启。")
        findings.append({"severity": "critical", "metric": "autovacuum_off",
                         "value": "off"})

    if lagging:
        status = "warning" if status != "critical" else status
        rec_parts.append(
            f"{len(lagging)} 张表 dead tuple 已超 autovacuum 触发阈值仍未清干净,"
            "autovacuum 跟不上。建议:1) 调高 autovacuum_max_workers;"
            "2) 调低 autovacuum_vacuum_cost_delay(让 vacuum 更快);"
            "3) 对高写入大表设 per-table autovacuum_vacuum_scale_factor=0.05。"
        )
        for t in lagging[:10]:
            findings.append({
                "severity": "warning", "metric": "autovacuum_lagging",
                "table": f"{t['schemaname']}.{t['relname']}",
                "dead_tup": t["n_dead_tup"],
                "trigger_at": t["estimated_trigger_threshold"],
                "dead_vs_trigger": f"{t['dead_vs_trigger_pct']:.0f}%",
            })

    if big_default_scale:
        if status == "ok":
            status = "warning"
        rec_parts.append(
            f"{len(big_default_scale)} 张大表(>100w 行)用默认 scale_factor={g_scale},"
            "意味着要积累大量 dead tuple 才触发 vacuum,大表必膨胀。"
            "建议对这些表设 per-table autovacuum_vacuum_scale_factor=0.02~0.05。"
        )
        for t in big_default_scale[:10]:
            findings.append({
                "severity": "info", "metric": "big_table_default_scale",
                "table": f"{t['schemaname']}.{t['relname']}",
                "live_tup": t["n_live_tup"],
            })

    if not rec_parts:
        rec_parts.append(
            f"autovacuum 配置健康(scale_factor={g_scale}, "
            f"max_workers={gsettings.get('autovacuum_max_workers')})。"
        )

    return _wrap(
        status=status, findings=findings,
        raw_data={
            "cluster_endpoint": endpoint, "database": database,
            "queried_at": _now_iso(),
            "global_settings": gsettings,
            "tables": tables,
            "lagging_count": len(lagging),
        },
        recommendation=" ".join(rec_parts),
    )


# ===========================================================================
# Tool 23 — Schema objects(库内表概览:Agent 排障的"发现入口")
# ===========================================================================
@mcp.tool()
def inspect_schema_objects(
    cluster_endpoint: str,
    database: str = "postgres",
    schema: str = "",
    top_n: int = 100,
) -> dict[str, Any]:
    """列出库里有哪些表(表名/类型/估算行数/大小)— 排障的发现入口:不知道库里有什么时先用这个,再用 table_bloat/index_usage 等深入。⚠ database 级:必须传 database=业务库名(默认 postgres 只看 postgres 库)。只读元数据,不碰任何业务数据。

    返回每张表:schema / 表名 / 类型(table/partitioned/foreign/matview)/
    估算行数(reltuples,统计值非精确 count)/ 总大小(含索引+toast)/ 注释。
    按总大小降序,默认 top 100。

    Args:
        cluster_endpoint: PG cluster endpoint(必传)
        database: 要看的库名(⚠ 必传业务库,默认 postgres 只看 postgres 库)
        schema: 只看某个 schema(留空 = 所有非系统 schema)
        top_n: 返回前 N 张表(按大小降序,默认 100,上限 500)
    """
    endpoint = _resolve_endpoint(cluster_endpoint)
    n = max(1, min(top_n, 500))
    log.info("[schema_objects] %s db=%s schema=%s top=%d", endpoint, database, schema or "*", n)

    with _connect(endpoint, database) as conn, conn.cursor() as cur:
        # 纯元数据:pg_class + pg_namespace,只读结构不碰数据(SHALL NOT #3)。
        # relkind: r=表 p=分区表 f=外部表 m=物化视图(不含普通 view,巡检关注有存储的对象)
        cur.execute(
            """
            SELECT n.nspname AS schemaname,
                   c.relname AS objname,
                   CASE c.relkind
                        WHEN 'r' THEN 'table'
                        WHEN 'p' THEN 'partitioned_table'
                        WHEN 'f' THEN 'foreign_table'
                        WHEN 'm' THEN 'materialized_view'
                        ELSE c.relkind::text
                   END AS objtype,
                   c.reltuples::bigint AS est_rows,
                   pg_total_relation_size(c.oid) AS total_bytes,
                   pg_size_pretty(pg_total_relation_size(c.oid)) AS total_pretty,
                   obj_description(c.oid, 'pg_class') AS comment
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind IN ('r', 'p', 'f', 'm')
              AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
              AND (%s = '' OR n.nspname = %s)
            ORDER BY pg_total_relation_size(c.oid) DESC
            LIMIT %s
            """,
            (schema, schema, n),
        )
        objects = [
            {
                "schema": r["schemaname"],
                "name": r["objname"],
                "type": r["objtype"],
                "estimated_rows": int(r["est_rows"]) if r["est_rows"] is not None and r["est_rows"] >= 0 else None,
                "total_size_bytes": int(r["total_bytes"]),
                "total_size_pretty": r["total_pretty"],
                "comment": _redact_query(r["comment"]) if r["comment"] else None,
            }
            for r in cur.fetchall()
        ]

    # 按 schema 聚合一个概览
    by_schema: dict[str, int] = {}
    for o in objects:
        by_schema[o["schema"]] = by_schema.get(o["schema"], 0) + 1

    if not objects:
        status = "ok"
        rec = (f"库 {database!r}"
               + (f" schema {schema!r}" if schema else "")
               + " 下没有表 / 物化视图(可能是空库,或表都在系统 schema)。"
                 "若要看业务表,确认 database 传的是业务库名而非 postgres。")
    else:
        status = "ok"
        biggest = objects[0]
        rec = (
            f"库 {database!r} 共 {len(objects)} 个对象(top {n} 按大小),"
            f"分布在 {len(by_schema)} 个 schema。最大:{biggest['schema']}.{biggest['name']}"
            f"({biggest['total_size_pretty']},约 {biggest['estimated_rows']} 行)。"
            "深入排查可用 inspect_table_bloat / inspect_index_usage / inspect_index_bloat。"
        )

    findings = [
        {
            "severity": "info", "metric": "schema_object",
            "object": f"{o['schema']}.{o['name']}",
            "type": o["type"], "size": o["total_size_pretty"],
            "est_rows": o["estimated_rows"],
        }
        for o in objects[:15]
    ]

    return _wrap(
        status=status, findings=findings,
        raw_data={
            "cluster_endpoint": endpoint, "database": database,
            "schema_filter": schema or None, "queried_at": _now_iso(),
            "object_count": len(objects),
            "objects_per_schema": by_schema,
            "objects": objects,
        },
        recommendation=rec,
    )


# ===========================================================================
# Tool 24 — Current queries(实时查询快照,不依赖 pg_stat_statements)
# ===========================================================================
@mcp.tool()
def inspect_current_queries(
    cluster_endpoint: str,
    top_n: int = _TOP_N_DEFAULT,
    min_duration_seconds: int = 0,
    database: str = "postgres",
) -> dict[str, Any]:
    """实时抓取"数据库现在正在跑什么"(基于 pg_stat_activity,不依赖任何扩展)— 当 pg_stat_statements 没装、或要看此刻的现场时用。与 inspect_top_queries(历史累计、需扩展)、inspect_long_transactions(仅长事务)互补。实例级:看到所有库,database 参数不影响结果。

    Args:
        top_n: 返回前 N 条(按运行时长降序,默认 20,上限 100)
        min_duration_seconds: 只看运行 ≥ 此秒数的查询(默认 0 = 全部非 idle)
    """
    endpoint = _resolve_endpoint(cluster_endpoint)
    n = max(1, min(top_n, _QUERY_LIMIT))
    log.info("[current_queries] %s top=%d min_dur=%ds", endpoint, n, min_duration_seconds)

    with _connect(endpoint, database) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT pid, usename, datname, application_name,
                   client_addr::text AS client_addr,
                   state, wait_event_type, wait_event,
                   backend_type,
                   EXTRACT(EPOCH FROM (NOW() - query_start))::int AS duration_seconds,
                   query_start, query
            FROM pg_stat_activity
            WHERE state IS DISTINCT FROM 'idle'
              AND pid <> pg_backend_pid()
              AND query_start IS NOT NULL
              AND EXTRACT(EPOCH FROM (NOW() - query_start)) >= %s
            ORDER BY (NOW() - query_start) DESC NULLS LAST
            LIMIT %s
            """,
            (min_duration_seconds, n),
        )
        rows = cur.fetchall()

    queries = [
        {
            "pid": int(r["pid"]),
            "usename": str(r.get("usename") or ""),
            "datname": str(r.get("datname") or ""),
            "application_name": str(r.get("application_name") or ""),
            "client_addr": str(r["client_addr"]) if r.get("client_addr") else None,
            "state": str(r.get("state") or ""),
            "wait_event_type": str(r.get("wait_event_type") or ""),
            "wait_event": str(r.get("wait_event") or ""),
            "backend_type": str(r.get("backend_type") or ""),
            "duration_seconds": int(r["duration_seconds"] or 0),
            "query_start": r["query_start"].isoformat() if r.get("query_start") else None,
            "query": _redact_query(r.get("query")),
        }
        for r in rows
    ]

    if not queries:
        status, rec = "ok", "当前无正在运行的查询(全部 idle)。"
    else:
        max_dur = max(q["duration_seconds"] for q in queries)
        if max_dur >= 300:
            status = "critical"
            rec = (f"{len(queries)} 条查询在跑,最长 {max_dur}s ≥ 5 分钟。"
                   "建议看 query / wait_event 定位;长查询可能需 pg_cancel_backend(pid)(需 admin)。")
        elif max_dur >= 60:
            status = "warning"
            rec = f"{len(queries)} 条查询在跑,最长 {max_dur}s ≥ 1 分钟,关注。"
        else:
            status = "ok"
            rec = f"{len(queries)} 条查询在跑,最长 {max_dur}s,正常。"

    findings = [
        {
            "severity": "critical" if q["duration_seconds"] >= 300
                        else "warning" if q["duration_seconds"] >= 60 else "info",
            "metric": "running_query",
            "pid": q["pid"], "duration_s": q["duration_seconds"],
            "state": q["state"],
            "wait": f"{q['wait_event_type']}/{q['wait_event']}" if q["wait_event_type"] else None,
            "datname": q["datname"], "usename": q["usename"],
        }
        for q in queries[:15]
    ]

    return _wrap(
        status=status, findings=findings,
        raw_data={
            "cluster_endpoint": endpoint, "queried_at": _now_iso(),
            "min_duration_seconds": min_duration_seconds,
            "running_query_count": len(queries),
            "queries": queries,
        },
        recommendation=rec,
    )


# ===========================================================================
# Tool 25 — Table / index I/O stats(磁盘 I/O 热点)
# ===========================================================================
@mcp.tool()
def inspect_table_io_stats(
    cluster_endpoint: str,
    database: str = "postgres",
    top_n: int = _TOP_N_DEFAULT,
) -> dict[str, Any]:
    """找磁盘 I/O 热点:哪张表 / 哪个索引从磁盘读得最多(基于 pg_statio_user_tables/indexes)。排查慢查询时定位"谁在猛读盘"。与 inspect_cache_hit_ratio(全局命中率评分)互补,这个是 I/O 量排行。⚠ database 级:排查业务库请传 database=业务库名(默认 postgres)。"""
    endpoint = _resolve_endpoint(cluster_endpoint)
    n = max(1, min(top_n, _QUERY_LIMIT))
    log.info("[table_io_stats] %s db=%s top=%d", endpoint, database, n)

    with _connect(endpoint, database) as conn, conn.cursor() as cur:
        # 表级 I/O:按磁盘读块数(heap_blks_read)降序 = 最猛读盘的表
        cur.execute(
            """
            SELECT schemaname, relname,
                   heap_blks_read, heap_blks_hit,
                   idx_blks_read, idx_blks_hit,
                   COALESCE(toast_blks_read, 0) AS toast_blks_read,
                   ROUND((heap_blks_hit::numeric / NULLIF(heap_blks_hit + heap_blks_read, 0)) * 100, 2) AS heap_hit_pct
            FROM pg_statio_user_tables
            WHERE heap_blks_read + idx_blks_read > 0
            ORDER BY (heap_blks_read + COALESCE(idx_blks_read,0) + COALESCE(toast_blks_read,0)) DESC
            LIMIT %s
            """,
            (n,),
        )
        tables = [
            {
                "schemaname": r["schemaname"], "relname": r["relname"],
                "heap_blks_read": int(r["heap_blks_read"] or 0),
                "heap_blks_hit": int(r["heap_blks_hit"] or 0),
                "idx_blks_read": int(r["idx_blks_read"] or 0),
                "idx_blks_hit": int(r["idx_blks_hit"] or 0),
                "toast_blks_read": int(r["toast_blks_read"] or 0),
                "heap_hit_pct": float(r["heap_hit_pct"] or 0),
            }
            for r in cur.fetchall()
        ]

        # 索引级 I/O:按磁盘读块数降序
        cur.execute(
            """
            SELECT schemaname, relname, indexrelname,
                   idx_blks_read, idx_blks_hit,
                   ROUND((idx_blks_hit::numeric / NULLIF(idx_blks_hit + idx_blks_read, 0)) * 100, 2) AS idx_hit_pct
            FROM pg_statio_user_indexes
            WHERE idx_blks_read > 0
            ORDER BY idx_blks_read DESC
            LIMIT %s
            """,
            (n,),
        )
        indexes = [
            {
                "schemaname": r["schemaname"], "relname": r["relname"],
                "indexrelname": r["indexrelname"],
                "idx_blks_read": int(r["idx_blks_read"] or 0),
                "idx_blks_hit": int(r["idx_blks_hit"] or 0),
                "idx_hit_pct": float(r["idx_hit_pct"] or 0),
            }
            for r in cur.fetchall()
        ]

    top_table_reads = tables[0]["heap_blks_read"] if tables else 0
    # 找命中率低又读量大的表(真正的 I/O 痛点)
    hot = [t for t in tables if t["heap_blks_read"] > 10000 and t["heap_hit_pct"] < 90]
    if hot:
        status = "warning"
        rec = (f"{len(hot)} 张表读盘量大且 cache 命中率 < 90%(最猛:{hot[0]['schemaname']}.{hot[0]['relname']})。"
               "建议:对这些表的高频查询查执行计划 / 加索引,或评估调大 shared_buffers。")
    else:
        status = "ok"
        rec = (f"I/O 热点表已列出(top 读盘 {top_table_reads} 块)。"
               "未发现读量大且命中率低的明显痛点。" if tables else "无显著磁盘 I/O(数据多在 cache)。")

    findings = [
        {
            "severity": "warning" if (t["heap_blks_read"] > 10000 and t["heap_hit_pct"] < 90) else "info",
            "metric": "table_io",
            "table": f"{t['schemaname']}.{t['relname']}",
            "heap_blks_read": t["heap_blks_read"],
            "heap_hit_pct": f"{t['heap_hit_pct']:.1f}%",
        }
        for t in tables[:10]
    ]

    return _wrap(
        status=status, findings=findings,
        raw_data={
            "cluster_endpoint": endpoint, "database": database,
            "queried_at": _now_iso(),
            "tables_by_io": tables, "indexes_by_io": indexes,
        },
        recommendation=rec,
    )


# ===========================================================================
# Tool 26 — Checkpoint / bgwriter stats(写入压力 / 性能抖动根因)
# ===========================================================================
@mcp.tool()
def inspect_checkpoint_stats(
    cluster_endpoint: str,
    database: str = "postgres",
) -> dict[str, Any]:
    """巡检 checkpoint / bgwriter 健康度 — 高写入场景下 checkpoint 太频繁(被 WAL 量触发而非定时)会导致周期性 IO 抖动、性能毛刺。这是 wait_events 看不到的根因。实例级,无需指定 database。自动兼容 PG <17(pg_stat_bgwriter)与 PG 17+(pg_stat_checkpointer)。"""
    endpoint = _resolve_endpoint(cluster_endpoint)
    log.info("[checkpoint_stats] %s", endpoint)

    data: dict[str, Any] = {}
    pg17_plus = False
    with _connect(endpoint, database) as conn, conn.cursor() as cur:
        # 取 server 版本判断用哪个视图
        cur.execute("SHOW server_version_num")
        version_num = int(cur.fetchone()["server_version_num"])
        pg17_plus = version_num >= 170000

        if pg17_plus:
            # PG 17+:checkpoint 统计搬到 pg_stat_checkpointer,bgwriter 留 buffers
            cur.execute(
                """
                SELECT num_timed AS checkpoints_timed,
                       num_requested AS checkpoints_req,
                       buffers_written AS buffers_checkpoint,
                       write_time, sync_time
                FROM pg_stat_checkpointer
                """
            )
            chk = cur.fetchone()
            cur.execute(
                "SELECT buffers_clean, maxwritten_clean, buffers_alloc FROM pg_stat_bgwriter"
            )
            bg = cur.fetchone()
            data = {**dict(chk), **dict(bg)}
        else:
            # PG <17:全在 pg_stat_bgwriter
            cur.execute(
                """
                SELECT checkpoints_timed, checkpoints_req,
                       buffers_checkpoint, buffers_clean, maxwritten_clean,
                       buffers_backend, buffers_backend_fsync, buffers_alloc,
                       checkpoint_write_time AS write_time,
                       checkpoint_sync_time AS sync_time
                FROM pg_stat_bgwriter
                """
            )
            data = dict(cur.fetchone())

    timed = int(data.get("checkpoints_timed") or 0)
    req = int(data.get("checkpoints_req") or 0)
    total_chk = timed + req
    req_pct = (req * 100.0 / total_chk) if total_chk > 0 else 0.0
    maxwritten = int(data.get("maxwritten_clean") or 0)

    raw = {
        "cluster_endpoint": endpoint, "queried_at": _now_iso(),
        "pg17_plus": pg17_plus,
        "checkpoints_timed": timed,
        "checkpoints_requested": req,
        "requested_pct": round(req_pct, 2),
        "buffers_checkpoint": int(data.get("buffers_checkpoint") or 0),
        "buffers_clean": int(data.get("buffers_clean") or 0),
        "maxwritten_clean": maxwritten,
        "buffers_backend": int(data.get("buffers_backend") or 0) if not pg17_plus else None,
        "write_time_ms": float(data.get("write_time") or 0),
        "sync_time_ms": float(data.get("sync_time") or 0),
    }

    findings: list[dict[str, Any]] = []
    rec_parts: list[str] = []
    status = "ok"

    # requested checkpoint 占比高 = checkpoint 被 WAL 量逼着提前触发 = max_wal_size 太小
    if total_chk > 10 and req_pct >= 50:
        status = "warning"
        findings.append({"severity": "warning", "metric": "requested_checkpoint_pct",
                         "value": f"{req_pct:.0f}%", "threshold": "50%"})
        rec_parts.append(
            f"requested checkpoint 占比 {req_pct:.0f}%(timed={timed}, requested={req})偏高 — "
            "说明 checkpoint 经常被 WAL 量逼着提前触发,会造成 IO 抖动。"
            "建议调大 max_wal_size,让 checkpoint 更多由 checkpoint_timeout 定时触发。"
        )
    if maxwritten > 0:
        rec_parts.append(
            f"bgwriter maxwritten_clean={maxwritten}(因写满 bgwriter_lru_maxpages 而停止的次数)— "
            "非零说明 bgwriter 清理跟不上,可调高 bgwriter_lru_maxpages。"
        )
    if not rec_parts:
        rec_parts.append(
            f"checkpoint 健康(timed={timed}, requested={req},requested 占比 {req_pct:.0f}%)。"
        )

    return _wrap(status=status, findings=findings, raw_data=raw,
                 recommendation=" ".join(rec_parts))


# ===========================================================================
# Tool 27 — Table stats freshness(统计信息过期 → 查询计划走偏)
# ===========================================================================
@mcp.tool()
def inspect_table_stats_freshness(
    cluster_endpoint: str,
    database: str = "postgres",
    mod_ratio_threshold: float = 0.1,
    top_n: int = _TOP_N_DEFAULT,
) -> dict[str, Any]:
    """找统计信息过期的表:自上次 ANALYZE 后改动比例过高,优化器会用过期统计算出错误的查询计划(走错索引 / 选错 join)。排查"某查询突然变慢但 SQL 没变"。与 inspect_autovacuum_tuning 数据同源、视角不同(这个专看"统计新鲜度→计划准确性")。⚠ database 级:排查业务库请传 database=业务库名(默认 postgres)。

    Args:
        mod_ratio_threshold: n_mod_since_analyze / n_live_tup ≥ 此值即标记过期(默认 0.1 = 10%)
    """
    endpoint = _resolve_endpoint(cluster_endpoint)
    n = max(1, min(top_n, _QUERY_LIMIT))
    if not (0 < mod_ratio_threshold <= 10):
        raise ValueError("mod_ratio_threshold 必须 ∈ (0, 10]")
    log.info("[stats_freshness] %s db=%s ratio=%.2f", endpoint, database, mod_ratio_threshold)

    with _connect(endpoint, database) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT schemaname, relname,
                   n_live_tup, n_mod_since_analyze,
                   ROUND(n_mod_since_analyze::numeric / NULLIF(n_live_tup, 0), 4) AS mod_ratio,
                   last_analyze, last_autoanalyze
            FROM pg_stat_user_tables
            WHERE n_mod_since_analyze > 0
              AND n_mod_since_analyze >= n_live_tup * %s
              AND n_live_tup > 1000
            ORDER BY n_mod_since_analyze::numeric / NULLIF(n_live_tup, 0) DESC NULLS LAST
            LIMIT %s
            """,
            (mod_ratio_threshold, n),
        )
        stale = [
            {
                "schemaname": r["schemaname"], "relname": r["relname"],
                "n_live_tup": int(r["n_live_tup"] or 0),
                "n_mod_since_analyze": int(r["n_mod_since_analyze"] or 0),
                "mod_ratio": float(r["mod_ratio"] or 0),
                "last_analyze": r["last_analyze"].isoformat() if r["last_analyze"] else None,
                "last_autoanalyze": r["last_autoanalyze"].isoformat() if r["last_autoanalyze"] else None,
            }
            for r in cur.fetchall()
        ]

    if not stale:
        status = "ok"
        rec = f"无统计信息明显过期的表(改动比例 ≥ {mod_ratio_threshold:.0%} 且 >1000 行)。"
    else:
        worst = stale[0]
        never_analyzed = [s for s in stale if not s["last_analyze"] and not s["last_autoanalyze"]]
        status = "warning"
        rec = (
            f"{len(stale)} 张表统计信息可能过期(改动比例最高 {worst['mod_ratio']:.0%}:"
            f"{worst['schemaname']}.{worst['relname']})。优化器可能用过期统计生成错误计划。"
            f"建议对这些表手动 ANALYZE,或调低 autovacuum_analyze_scale_factor。"
        )
        if never_analyzed:
            rec += f" ⚠ 其中 {len(never_analyzed)} 张表从未被 analyze 过。"

    findings = [
        {
            "severity": "warning", "metric": "stale_statistics",
            "table": f"{s['schemaname']}.{s['relname']}",
            "mod_ratio": f"{s['mod_ratio']:.0%}",
            "n_mod_since_analyze": s["n_mod_since_analyze"],
            "last_analyze": s["last_analyze"] or s["last_autoanalyze"] or "never",
        }
        for s in stale[:15]
    ]

    return _wrap(
        status=status, findings=findings,
        raw_data={
            "cluster_endpoint": endpoint, "database": database,
            "queried_at": _now_iso(),
            "mod_ratio_threshold": mod_ratio_threshold,
            "stale_tables": stale,
        },
        recommendation=rec,
    )


# ===========================================================================
# Tool 28 — Temp file usage(work_mem 不足 → 排序/hash 落盘)
# ===========================================================================
@mcp.tool()
def inspect_temp_file_usage(
    cluster_endpoint: str,
) -> dict[str, Any]:
    """巡检临时文件使用:排序 / hash / 大结果集在 work_mem 装不下时会落盘成临时文件,严重拖慢查询。temp_bytes 大说明 work_mem 不足或有未优化的大查询。实例级:自动看全 cluster 各库(基于 pg_stat_database),无需指定 database。"""
    endpoint = _resolve_endpoint(cluster_endpoint)
    log.info("[temp_file_usage] %s", endpoint)

    with _connect(endpoint, "postgres") as conn, conn.cursor() as cur:
        # pg_stat_database 是 cluster 级,一条连接看全部库,无需逐库扫
        cur.execute(
            """
            SELECT datname, temp_files, temp_bytes,
                   pg_size_pretty(temp_bytes) AS temp_pretty,
                   blks_read, blks_hit,
                   stats_reset
            FROM pg_stat_database
            WHERE datname IS NOT NULL AND temp_bytes > 0
            ORDER BY temp_bytes DESC
            """
        )
        dbs = [
            {
                "datname": r["datname"],
                "temp_files": int(r["temp_files"] or 0),
                "temp_bytes": int(r["temp_bytes"] or 0),
                "temp_pretty": r["temp_pretty"],
                "stats_reset": r["stats_reset"].isoformat() if r["stats_reset"] else None,
            }
            for r in cur.fetchall()
        ]

        # 当前 work_mem 值(给建议参考)
        cur.execute("SHOW work_mem")
        work_mem = cur.fetchone()["work_mem"]

    total_temp_bytes = sum(d["temp_bytes"] for d in dbs)
    total_temp_files = sum(d["temp_files"] for d in dbs)

    if not dbs:
        status = "ok"
        rec = "无临时文件产生(排序/hash 都在 work_mem 内完成,健康)。"
    else:
        total_gb = total_temp_bytes / (1024 ** 3)
        worst = dbs[0]
        if total_gb >= 10:
            status = "warning"
            rec = (
                f"累计临时文件 {worst['temp_pretty'] if len(dbs)==1 else f'{total_gb:.1f} GB'}"
                f"(共 {total_temp_files} 个文件,最多:{worst['datname']})。"
                f"当前 work_mem={work_mem}。建议:1) 找产生大排序/hash 的查询优化;"
                "2) 对特定会话调大 work_mem(注意每连接每操作都吃这个值,别全局调太大)。"
                "注:temp_bytes 是自 stats_reset 以来累计值,看趋势而非绝对值。"
            )
        else:
            status = "ok"
            rec = (f"有临时文件产生(累计 {total_gb:.2f} GB,work_mem={work_mem}),"
                   "量不大。temp_bytes 是累计值,若持续快速增长再关注。")

    findings = [
        {
            "severity": "warning" if d["temp_bytes"] >= 1024**3 else "info",
            "metric": "temp_files",
            "datname": d["datname"],
            "temp_files": d["temp_files"], "temp_bytes_pretty": d["temp_pretty"],
        }
        for d in dbs[:10]
    ]

    return _wrap(
        status=status, findings=findings,
        raw_data={
            "cluster_endpoint": endpoint, "queried_at": _now_iso(),
            "work_mem": work_mem,
            "total_temp_bytes": total_temp_bytes,
            "total_temp_files": total_temp_files,
            "databases": dbs,
        },
        recommendation=rec,
    )


if __name__ == "__main__":
    log.info("starting RDS PostgreSQL inspect MCP server on :8000 with 28 tools")
    mcp.run(transport="streamable-http")
