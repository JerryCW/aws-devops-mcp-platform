"""RDS / Aurora MySQL 巡检 MCP server(语义化只读巡检 tool,IAM 形态)。

关键设计:**endpoint 不注入容器**。本 server 是通用 MySQL 巡检工具,不绑定任何
实例。每个 tool 的 `cluster_endpoint` 是**必传参数**,由 DevOps Agent 在调用时按
用户上下文给出(如"巡检 cluster-A 的锁等待" → cluster_endpoint=cluster-A...)。
一套 server 天然巡检 N 个同类 MySQL 实例。

适用引擎:RDS MySQL 8.0 / Aurora MySQL 3(8.0 兼容)。
**主要面向 MySQL 8.0**(performance_schema.data_lock_waits 等 8.0 视图);
5.7 上部分工具(锁等待)优雅降级。

暴露的 tool 全集(对标 PG server,映射到 MySQL 数据面排查):

【核心 — 事务 / 锁 / 连接】
  inspect_processlist          活跃线程快照(information_schema.PROCESSLIST,不依赖扩展)
  inspect_long_transactions    长事务(INNODB_TRX,排查未提交事务导致的锁堆积)
  inspect_lock_waits           锁等待阻塞链(performance_schema.data_lock_waits,8.0)
  inspect_metadata_locks       元数据锁(DDL 被 DML 阻塞,performance_schema.metadata_locks)
  inspect_connections          连接数 / 来源分布 / max_connections 占比
  inspect_active_clients        活跃 client 按 user+host 聚合(连接泄漏候选)

【性能 — 慢查询 / buffer pool / I/O】
  inspect_buffer_pool          InnoDB buffer pool 命中率 + 脏页 + 等待
  inspect_slow_queries         慢查询 top N(performance_schema.events_statements_summary)
  inspect_current_queries      实时慢查询(PROCESSLIST 中 TIME 超阈值,不依赖扩展)
  inspect_table_io             表级 I/O 热点(performance_schema.table_io_waits_summary)
  inspect_temp_tables          内部临时表落盘(Created_tmp_disk_tables,排查 sort/join 慢)
  inspect_global_status        关键 GLOBAL STATUS 计数器(QPS / 连接 / 中止 / 慢查询)

【容量 / 配置 / 对象】
  inspect_table_sizes          表 / 索引大小 top N(information_schema.TABLES)
  inspect_schema_objects       列出库里有哪些表(表名/行数/大小,排障发现入口)
  inspect_index_usage          未使用索引(sys.schema_unused_indexes,冗余索引候选)
  inspect_auto_increment       auto_increment 容量耗尽风险(int 列爆 21 亿)
  inspect_variables            关键 GLOBAL VARIABLES(参数配置审阅)

【复制 / 高可用】
  inspect_replica_status       复制延迟 + 链路状态(SHOW REPLICA STATUS / Aurora)

容器外部注入(由 target stack 在 Runtime 上设环境变量,**不含 endpoint**):
  DB_PORT                   默认 3306
  DB_SECRET_NAME            Secrets Manager 路径(mcp_devops_ro 凭据)
  AWS_REGION                Secret 所在 region
  LOG_LEVEL                 可选,默认 INFO

设计契约:
  - FastMCP host=0.0.0.0 port=8000 stateless_http=True(SHALL NOT #6 / #17)
  - **Secret 5 分钟 TTL 缓存**(P7)
  - **连接强制 MAX_EXECUTION_TIME=15s**(巡检 SQL 不卡业务 + LLM 不超时)
  - 用 PyMySQL(纯 Python,无 C 扩展,arm64 装包快)
  - 所有 tool SQL 固化,参数化绑定(SHALL NOT #3)
  - DB 返回的 query 文本走 `_redact_query()`:截断 + 标注 [FROM_DB | UNTRUSTED]
  - 返回结构严格 conventions A8(status / findings / raw_data / recommendation)
  - 严禁打印 password 内容(SHALL NOT #12)
  - 只读账号:容器用 mcp_devops_ro,权限 GRANT SELECT, PROCESS, REPLICATION CLIENT
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
import pymysql
from mcp.server.fastmcp import FastMCP
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

# 巡检 SQL 执行超时(P1):MySQL 8.0 用 MAX_EXECUTION_TIME(毫秒),SELECT 级 hint;
# 同时设 session 级 max_execution_time 兜底。任何巡检 SQL ≥ 15s 自动 abort。
_MAX_EXECUTION_TIME_MS: int = 15000

# Secret 缓存 TTL(P7)
_SECRET_TTL_SECONDS: int = 300

# query 文本防注入截断(S1 / S2)
_QUERY_TRUNCATE_LEN: int = 200
_QUERY_REDACT_PREFIX: str = "[FROM_DB | UNTRUSTED]: "

# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
_LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=_LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("rds_mysql_inspect")

# ---------------------------------------------------------------------------
# 容器启动时一次性读取的环境变量(不含 endpoint —— endpoint 是 tool 调用必传)
# ---------------------------------------------------------------------------
_AWS_REGION: str = os.environ.get("AWS_REGION", "us-east-1")
_DB_SECRET_NAME: str = os.environ.get("DB_SECRET_NAME", "")
_DEFAULT_DB_PORT: int = int(os.environ.get("DB_PORT", "3306"))

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
_secret_cache: dict[str, tuple[float, tuple[str, str]]] = {}
_secret_cache_lock = Lock()


def _fetch_db_credentials() -> tuple[str, str]:
    """从 Secrets Manager 拉 mcp_devops_ro 凭据,带 5 分钟 TTL 缓存。线程安全。"""
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
        sm = boto3.client("secretsmanager", region_name=_AWS_REGION)
        raw = sm.get_secret_value(SecretId=_DB_SECRET_NAME)["SecretString"]
        payload = json.loads(raw)
        user = payload["username"]
        pwd = payload["password"]
        if pwd == _PLACEHOLDER_PASSWORD:
            raise RuntimeError(
                f"Secret {_DB_SECRET_NAME!r} 中 password 仍是 PLACEHOLDER,请灌真密码"
            )
        _secret_cache[_DB_SECRET_NAME] = (now, (user, pwd))
        return user, pwd


def _resolve_endpoint(cluster_endpoint: str) -> str:
    """校验 cluster_endpoint 非空(必传)。

    MCP server 是通用 MySQL 巡检工具,不绑定任何实例。endpoint 必须由调用方
    (DevOps Agent)在 tool 调用时传入,容器内不注入任何默认 endpoint。
    """
    endpoint = (cluster_endpoint or "").strip()
    if not endpoint:
        raise ValueError(
            "cluster_endpoint 必传:请传入要巡检的 MySQL cluster/instance endpoint"
            "(如 my-db.cluster-xxxx.us-east-1.rds.amazonaws.com)。"
            "本工具是通用巡检工具,不绑定特定实例。"
        )
    return endpoint


def _connect(endpoint: str, database: str | None = None) -> pymysql.connections.Connection:
    """建 MySQL 连接 — 只读账号 + TLS + 巡检超时。

    用 PyMySQL 关键字参数(不拼连接串)— 密码含特殊字符也安全。
    session 级 max_execution_time(8.0,毫秒)兜底:巡检 SQL 超时自动 abort 不卡业务。
    DictCursor 返回 dict 便于 JSON 序列化。

    注:program_name 进 performance_schema.session_connect_attrs(不显示在 PROCESSLIST,
    与 PG application_name 不同);用于审计追溯巡检连接来源。
    """
    user, pwd = _fetch_db_credentials()
    conn = pymysql.connect(
        host=endpoint,
        port=_DEFAULT_DB_PORT,
        user=user,
        password=pwd,
        database=database,
        connect_timeout=_CONNECT_TIMEOUT_SECONDS,
        read_timeout=_CONNECT_TIMEOUT_SECONDS + 5,
        write_timeout=_CONNECT_TIMEOUT_SECONDS + 5,
        ssl={"ssl": True},  # RDS/Aurora TLS;不强制校验 CA(自签链路)
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        program_name="mcp_devops_inspect",
    )
    # session 级巡检超时兜底(8.0;5.7 无此变量则忽略)
    try:
        with conn.cursor() as cur:
            cur.execute("SET SESSION max_execution_time = %s", (_MAX_EXECUTION_TIME_MS,))
    except pymysql.MySQLError:
        pass
    return conn


def _redact_query(query: str | None, max_len: int = _QUERY_TRUNCATE_LEN) -> str | None:
    """截断 + 标注 SQL 文本,防 prompt injection / PII 外漏(S1 / S2)。"""
    if not query:
        return None
    truncated = query[:max_len].rstrip()
    if len(query) > max_len:
        truncated += "...[truncated]"
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


def _to_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except (ValueError, TypeError):
        return default


def _to_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (ValueError, TypeError):
        return default


def _bytes_to_human(n: int) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024:
            return f"{f:.2f} {unit}"
        f /= 1024
    return f"{f:.2f} PB"


def _global_status(cur, names: list[str]) -> dict[str, int]:
    """批量读 performance_schema.global_status(8.0)/ SHOW GLOBAL STATUS 兜底。

    返回 {VARIABLE_NAME: int_value}。MySQL 8.0 用 performance_schema.global_status,
    无权限或 5.7 降级走 SHOW GLOBAL STATUS。
    """
    result: dict[str, int] = {}
    placeholders = ",".join(["%s"] * len(names))
    try:
        cur.execute(
            f"SELECT VARIABLE_NAME, VARIABLE_VALUE FROM performance_schema.global_status "
            f"WHERE VARIABLE_NAME IN ({placeholders})",
            tuple(names),
        )
        for r in cur.fetchall():
            result[str(r["VARIABLE_NAME"])] = _to_int(r["VARIABLE_VALUE"])
    except pymysql.MySQLError:
        for nm in names:
            try:
                cur.execute("SHOW GLOBAL STATUS LIKE %s", (nm,))
                row = cur.fetchone()
                if row:
                    result[nm] = _to_int(row.get("Value"))
            except pymysql.MySQLError:
                continue
    return result


def _global_variables(cur, names: list[str]) -> dict[str, str]:
    """批量读 performance_schema.global_variables(8.0)/ SHOW GLOBAL VARIABLES 兜底。"""
    result: dict[str, str] = {}
    placeholders = ",".join(["%s"] * len(names))
    try:
        cur.execute(
            f"SELECT VARIABLE_NAME, VARIABLE_VALUE FROM performance_schema.global_variables "
            f"WHERE VARIABLE_NAME IN ({placeholders})",
            tuple(names),
        )
        for r in cur.fetchall():
            result[str(r["VARIABLE_NAME"])] = str(r["VARIABLE_VALUE"])
    except pymysql.MySQLError:
        for nm in names:
            try:
                cur.execute("SHOW GLOBAL VARIABLES LIKE %s", (nm,))
                row = cur.fetchone()
                if row:
                    result[nm] = str(row.get("Value"))
            except pymysql.MySQLError:
                continue
    return result


# ===========================================================================
# Tool 1 — Processlist 活跃线程快照
# ===========================================================================
@mcp.tool()
def inspect_processlist(
    cluster_endpoint: str,
    include_sleep: bool = False,
) -> dict[str, Any]:
    """巡检活跃线程快照(information_schema.PROCESSLIST):正在跑什么、各线程状态/耗时 — 排障第一步,不依赖任何扩展。默认不含 Sleep 连接。实例级。endpoint 必传。"""
    endpoint = _resolve_endpoint(cluster_endpoint)
    log.info("[processlist] %s include_sleep=%s", endpoint, include_sleep)

    conn = _connect(endpoint)
    try:
        with conn.cursor() as cur:
            where = "" if include_sleep else "WHERE COMMAND != 'Sleep'"
            cur.execute(
                f"""
                SELECT ID, USER, HOST, DB, COMMAND, TIME, STATE, INFO
                FROM information_schema.PROCESSLIST
                {where}
                ORDER BY TIME DESC
                LIMIT %s
                """,
                (_QUERY_LIMIT,),
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    threads = []
    state_counts: dict[str, int] = {}
    for r in rows:
        cmd = str(r.get("COMMAND") or "")
        state_counts[cmd] = state_counts.get(cmd, 0) + 1
        threads.append({
            "id": _to_int(r.get("ID")),
            "user": str(r.get("USER") or ""),
            "host": str(r.get("HOST") or ""),
            "db": str(r.get("DB") or ""),
            "command": cmd,
            "time_seconds": _to_int(r.get("TIME")),
            "state": str(r.get("STATE") or ""),
            "query": _redact_query(r.get("INFO")),
        })

    raw_data = {
        "cluster_endpoint": endpoint, "queried_at": _now_iso(),
        "active_thread_count": len(threads),
        "command_distribution": state_counts,
        "threads": threads,
    }
    findings: list[dict[str, Any]] = []
    status = "ok"
    long_running = [t for t in threads if t["command"] in ("Query", "Execute") and t["time_seconds"] >= 60]
    if long_running:
        max_t = max(t["time_seconds"] for t in long_running)
        status = "critical" if max_t >= 300 else "warning"
        for t in long_running[:10]:
            findings.append({"severity": "critical" if t["time_seconds"] >= 300 else "warning",
                             "metric": "long_running_query", "thread_id": t["id"],
                             "time_s": t["time_seconds"], "state": t["state"]})
        rec = (f"发现 {len(long_running)} 个运行 ≥ 60s 的查询(最长 {max_t}s)。"
               "建议看 INFO 字段定位 SQL,必要时 CALL mysql.rds_kill(thread_id)。")
    else:
        rec = f"{len(threads)} 个活跃线程,无长时间运行查询。"

    return _wrap(status=status, findings=findings, raw_data=raw_data, recommendation=rec)


# ===========================================================================
# Tool 2 — 长事务(INNODB_TRX)
# ===========================================================================
@mcp.tool()
def inspect_long_transactions(
    cluster_endpoint: str,
    threshold_seconds: int = _DEFAULT_THRESHOLD_SECONDS,
) -> dict[str, Any]:
    """巡检长事务(information_schema.INNODB_TRX):未提交/运行超阈值的 InnoDB 事务 — 排查"事务不提交"导致的锁堆积、undo 膨胀、purge 滞后。实例级。endpoint 必传。"""
    endpoint = _resolve_endpoint(cluster_endpoint)
    if threshold_seconds < 1:
        raise ValueError(f"threshold_seconds 必须 >= 1,收到 {threshold_seconds}")
    log.info("[long_transactions] %s threshold=%ds", endpoint, threshold_seconds)

    conn = _connect(endpoint)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT t.trx_id, t.trx_state, t.trx_started,
                       TIMESTAMPDIFF(SECOND, t.trx_started, NOW()) AS age_seconds,
                       t.trx_mysql_thread_id AS thread_id,
                       t.trx_rows_locked, t.trx_rows_modified,
                       t.trx_isolation_level, t.trx_query,
                       p.USER AS user, p.HOST AS host, p.DB AS db
                FROM information_schema.INNODB_TRX t
                LEFT JOIN information_schema.PROCESSLIST p
                       ON p.ID = t.trx_mysql_thread_id
                WHERE TIMESTAMPDIFF(SECOND, t.trx_started, NOW()) >= %s
                ORDER BY t.trx_started ASC
                LIMIT %s
                """,
                (threshold_seconds, _QUERY_LIMIT),
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    transactions = []
    for r in rows:
        transactions.append({
            "trx_id": str(r.get("trx_id") or ""),
            "trx_state": str(r.get("trx_state") or ""),
            "trx_started": r["trx_started"].isoformat() if r.get("trx_started") else None,
            "age_seconds": _to_int(r.get("age_seconds")),
            "thread_id": _to_int(r.get("thread_id")),
            "user": str(r.get("user") or ""), "host": str(r.get("host") or ""),
            "db": str(r.get("db") or ""),
            "rows_locked": _to_int(r.get("trx_rows_locked")),
            "rows_modified": _to_int(r.get("trx_rows_modified")),
            "isolation_level": str(r.get("trx_isolation_level") or ""),
            "query": _redact_query(r.get("trx_query")),
        })

    if not transactions:
        status, rec = "ok", "无长事务,InnoDB 事务健康。"
    else:
        max_age = max(t["age_seconds"] for t in transactions)
        if max_age >= threshold_seconds * _CRITICAL_MULTIPLIER:
            status = "critical"
            rec = (f"发现 {len(transactions)} 条长事务,最长 {max_age}s 达 critical。"
                   "建议:1) 排查应用层未提交事务/连接泄漏;2) 必要时 CALL mysql.rds_kill(thread_id)。"
                   "长事务会阻塞 undo purge,导致历史版本堆积、表膨胀。")
        else:
            status = "warning"
            rec = f"发现 {len(transactions)} 条长事务,最长 {max_age}s,排查应用层事务边界。"

    findings = [
        {"severity": "critical" if t["age_seconds"] >= threshold_seconds * _CRITICAL_MULTIPLIER else "warning",
         "metric": "long_transaction", "trx_id": t["trx_id"], "age_s": t["age_seconds"],
         "state": t["trx_state"], "rows_locked": t["rows_locked"], "user": t["user"]}
        for t in transactions
    ]
    return _wrap(status=status, findings=findings,
                 raw_data={"cluster_endpoint": endpoint, "threshold_seconds": threshold_seconds,
                           "queried_at": _now_iso(), "transactions": transactions},
                 recommendation=rec)


# ===========================================================================
# Tool 3 — 锁等待阻塞链(8.0 performance_schema.data_lock_waits)
# ===========================================================================
@mcp.tool()
def inspect_lock_waits(cluster_endpoint: str) -> dict[str, Any]:
    """巡检行锁等待阻塞链(performance_schema.data_lock_waits,MySQL 8.0):谁在等谁的锁、阻塞了多久 — 排查锁等待超时、事务卡顿。5.7 会优雅降级提示。实例级。endpoint 必传。"""
    endpoint = _resolve_endpoint(cluster_endpoint)
    log.info("[lock_waits] %s", endpoint)

    conn = _connect(endpoint)
    try:
        with conn.cursor() as cur:
            try:
                cur.execute(
                    """
                    SELECT
                      w.REQUESTING_ENGINE_TRANSACTION_ID AS waiting_trx,
                      rt.trx_mysql_thread_id AS waiting_thread,
                      TIMESTAMPDIFF(SECOND, rt.trx_started, NOW()) AS waiting_age_s,
                      rt.trx_query AS waiting_query,
                      w.BLOCKING_ENGINE_TRANSACTION_ID AS blocking_trx,
                      bt.trx_mysql_thread_id AS blocking_thread,
                      bt.trx_query AS blocking_query,
                      bp.USER AS blocking_user, bp.HOST AS blocking_host,
                      ol.OBJECT_SCHEMA AS lock_schema, ol.OBJECT_NAME AS lock_table,
                      ol.LOCK_TYPE AS lock_type, ol.LOCK_MODE AS lock_mode
                    FROM performance_schema.data_lock_waits w
                    JOIN information_schema.INNODB_TRX rt
                      ON rt.trx_id = w.REQUESTING_ENGINE_TRANSACTION_ID
                    JOIN information_schema.INNODB_TRX bt
                      ON bt.trx_id = w.BLOCKING_ENGINE_TRANSACTION_ID
                    LEFT JOIN performance_schema.data_locks ol
                      ON ol.ENGINE_LOCK_ID = w.REQUESTING_ENGINE_LOCK_ID
                    LEFT JOIN information_schema.PROCESSLIST bp
                      ON bp.ID = bt.trx_mysql_thread_id
                    ORDER BY waiting_age_s DESC
                    LIMIT %s
                    """,
                    (_QUERY_LIMIT,),
                )
                rows = cur.fetchall()
            except pymysql.MySQLError as e:
                return _wrap(
                    status="warning", findings=[],
                    raw_data={"cluster_endpoint": endpoint, "queried_at": _now_iso(),
                              "error": str(e)[:200]},
                    recommendation=("读 performance_schema.data_lock_waits 失败(MySQL 5.7 用 "
                                    "information_schema.INNODB_LOCK_WAITS,或缺 PROCESS/SELECT 权限)。"
                                    "确认实例为 8.0 且 mcp_devops_ro 有 SELECT on performance_schema。"),
                )
    finally:
        conn.close()

    waits = []
    for r in rows:
        waits.append({
            "waiting_trx": str(r.get("waiting_trx") or ""),
            "waiting_thread": _to_int(r.get("waiting_thread")),
            "waiting_age_seconds": _to_int(r.get("waiting_age_s")),
            "waiting_query": _redact_query(r.get("waiting_query")),
            "blocking_trx": str(r.get("blocking_trx") or ""),
            "blocking_thread": _to_int(r.get("blocking_thread")),
            "blocking_query": _redact_query(r.get("blocking_query")),
            "blocking_user": str(r.get("blocking_user") or ""),
            "blocking_host": str(r.get("blocking_host") or ""),
            "lock_schema": str(r.get("lock_schema") or ""),
            "lock_table": str(r.get("lock_table") or ""),
            "lock_type": str(r.get("lock_type") or ""),
            "lock_mode": str(r.get("lock_mode") or ""),
        })

    if not waits:
        return _wrap(status="ok", findings=[],
                     raw_data={"cluster_endpoint": endpoint, "queried_at": _now_iso(), "lock_waits": []},
                     recommendation="当前无行锁等待,锁健康。")

    max_wait = max(w["waiting_age_seconds"] for w in waits)
    status = "critical" if max_wait >= 30 else "warning"
    findings = [
        {"severity": "critical" if w["waiting_age_seconds"] >= 30 else "warning",
         "metric": "lock_wait", "waiting_thread": w["waiting_thread"],
         "blocking_thread": w["blocking_thread"], "wait_s": w["waiting_age_seconds"],
         "table": f"{w['lock_schema']}.{w['lock_table']}"}
        for w in waits
    ]
    rec = (f"发现 {len(waits)} 条锁等待,最长 {max_wait}s。阻塞源 thread="
           f"{waits[0]['blocking_thread']}。建议:定位 blocking 事务,必要时 "
           "CALL mysql.rds_kill(blocking_thread)。检查 innodb_lock_wait_timeout(默认 50s)。")
    return _wrap(status=status, findings=findings,
                 raw_data={"cluster_endpoint": endpoint, "queried_at": _now_iso(), "lock_waits": waits},
                 recommendation=rec)


# ===========================================================================
# Tool 4 — 元数据锁(DDL 被 DML 阻塞)
# ===========================================================================
@mcp.tool()
def inspect_metadata_locks(cluster_endpoint: str) -> dict[str, Any]:
    """巡检元数据锁(performance_schema.metadata_locks):排查 DDL(ALTER TABLE)被长事务/未提交 DML 持有的 MDL 阻塞 —"加字段卡住"的经典原因。需 performance_schema 开启 wait/lock/metadata instrument。实例级。endpoint 必传。"""
    endpoint = _resolve_endpoint(cluster_endpoint)
    log.info("[metadata_locks] %s", endpoint)

    conn = _connect(endpoint)
    try:
        with conn.cursor() as cur:
            try:
                cur.execute(
                    """
                    SELECT ml.OBJECT_SCHEMA AS object_schema, ml.OBJECT_NAME AS object_name,
                           ml.LOCK_TYPE AS lock_type, ml.LOCK_STATUS AS lock_status,
                           ml.OWNER_THREAD_ID AS owner_thread_id,
                           t.PROCESSLIST_ID AS processlist_id,
                           t.PROCESSLIST_USER AS user, t.PROCESSLIST_HOST AS host,
                           t.PROCESSLIST_TIME AS time_seconds,
                           t.PROCESSLIST_INFO AS query
                    FROM performance_schema.metadata_locks ml
                    LEFT JOIN performance_schema.threads t
                           ON t.THREAD_ID = ml.OWNER_THREAD_ID
                    WHERE ml.OBJECT_TYPE = 'TABLE'
                    ORDER BY ml.LOCK_STATUS, t.PROCESSLIST_TIME DESC
                    LIMIT %s
                    """,
                    (_QUERY_LIMIT,),
                )
                rows = cur.fetchall()
            except pymysql.MySQLError as e:
                return _wrap(
                    status="warning", findings=[],
                    raw_data={"cluster_endpoint": endpoint, "queried_at": _now_iso(),
                              "error": str(e)[:200]},
                    recommendation=("读 metadata_locks 失败:确认 performance_schema 已开启,"
                                    "且 wait/lock/metadata instrument 与 threads consumer 启用。"),
                )
    finally:
        conn.close()

    locks = []
    pending = []
    for r in rows:
        item = {
            "object": f"{r.get('object_schema')}.{r.get('object_name')}",
            "lock_type": str(r.get("lock_type") or ""),
            "lock_status": str(r.get("lock_status") or ""),
            "thread_id": _to_int(r.get("processlist_id")),
            "user": str(r.get("user") or ""), "host": str(r.get("host") or ""),
            "time_seconds": _to_int(r.get("time_seconds")),
            "query": _redact_query(r.get("query")),
        }
        locks.append(item)
        if item["lock_status"] == "PENDING":
            pending.append(item)

    raw_data = {"cluster_endpoint": endpoint, "queried_at": _now_iso(),
                "total_table_mdl": len(locks), "pending_locks": pending, "all_locks": locks[:50]}
    if not pending:
        return _wrap(status="ok", findings=[], raw_data=raw_data,
                     recommendation=f"{len(locks)} 个表级 MDL,无 PENDING(无 DDL 被阻塞)。")
    status = "critical" if any(p["time_seconds"] >= 60 for p in pending) else "warning"
    findings = [{"severity": "critical" if p["time_seconds"] >= 60 else "warning",
                 "metric": "metadata_lock_pending", "object": p["object"],
                 "wait_s": p["time_seconds"], "user": p["user"]} for p in pending]
    rec = (f"发现 {len(pending)} 个 PENDING 元数据锁(DDL 等待)。"
           "DDL 被一个持有该表 MDL 的长事务/未提交 DML 阻塞,且会反过来阻塞后续所有访问该表的查询。"
           "建议:用 inspect_long_transactions 找持锁事务,提交或 kill 它。")
    return _wrap(status=status, findings=findings, raw_data=raw_data, recommendation=rec)


# ===========================================================================
# Tool 5 — 连接数 / max_connections 占比
# ===========================================================================
@mcp.tool()
def inspect_connections(cluster_endpoint: str) -> dict[str, Any]:
    """巡检连接数:当前连接 / max_connections 占比 / 历史峰值 / 中止连接数 — 排查连接耗尽、连接池配置不当。实例级。endpoint 必传。"""
    endpoint = _resolve_endpoint(cluster_endpoint)
    log.info("[connections] %s", endpoint)

    conn = _connect(endpoint)
    try:
        with conn.cursor() as cur:
            status_vals = _global_status(cur, [
                "Threads_connected", "Threads_running", "Max_used_connections",
                "Aborted_connects", "Aborted_clients", "Connection_errors_max_connections",
            ])
            var_vals = _global_variables(cur, ["max_connections"])
    finally:
        conn.close()

    threads_connected = status_vals.get("Threads_connected", 0)
    threads_running = status_vals.get("Threads_running", 0)
    max_used = status_vals.get("Max_used_connections", 0)
    max_conn = _to_int(var_vals.get("max_connections"), 0)
    used_pct = (threads_connected * 100.0 / max_conn) if max_conn > 0 else 0.0
    peak_pct = (max_used * 100.0 / max_conn) if max_conn > 0 else 0.0

    raw_data = {
        "cluster_endpoint": endpoint, "queried_at": _now_iso(),
        "threads_connected": threads_connected, "threads_running": threads_running,
        "max_used_connections": max_used, "max_connections": max_conn,
        "used_pct": round(used_pct, 2), "peak_pct": round(peak_pct, 2),
        "aborted_connects": status_vals.get("Aborted_connects", 0),
        "aborted_clients": status_vals.get("Aborted_clients", 0),
        "connection_errors_max_connections": status_vals.get("Connection_errors_max_connections", 0),
    }
    findings: list[dict[str, Any]] = []
    status = "ok"
    rec_parts = []
    if used_pct >= 90:
        status = "critical"
        findings.append({"severity": "critical", "metric": "connections_used_pct",
                         "value": f"{used_pct:.1f}%", "threshold": "90%"})
        rec_parts.append(f"连接使用 {used_pct:.1f}% 极高,即将耗尽 max_connections={max_conn}。")
    elif used_pct >= 75:
        status = "warning"
        findings.append({"severity": "warning", "metric": "connections_used_pct",
                         "value": f"{used_pct:.1f}%", "threshold": "75%"})
        rec_parts.append(f"连接使用 {used_pct:.1f}% 偏高。")
    if status_vals.get("Connection_errors_max_connections", 0) > 0:
        status = "critical" if status != "critical" else status
        findings.append({"severity": "critical", "metric": "connection_errors_max_connections",
                         "value": str(status_vals["Connection_errors_max_connections"])})
        rec_parts.append("曾因 max_connections 耗尽拒绝连接,考虑上 RDS Proxy 或调大 max_connections。")
    if not rec_parts:
        rec_parts.append(f"连接健康:{threads_connected}/{max_conn} ({used_pct:.1f}%),峰值 {max_used}。")
    return _wrap(status=status, findings=findings, raw_data=raw_data,
                 recommendation=" ".join(rec_parts))


# ===========================================================================
# Tool 6 — 活跃 client 按 user+host 聚合
# ===========================================================================
@mcp.tool()
def inspect_active_clients(cluster_endpoint: str) -> dict[str, Any]:
    """巡检活跃客户端:按 user+host 聚合连接数 + 各自最长运行时间 — 排查连接来自哪、连接池泄漏(某来源连接数异常多)。实例级。endpoint 必传。"""
    endpoint = _resolve_endpoint(cluster_endpoint)
    log.info("[active_clients] %s", endpoint)

    conn = _connect(endpoint)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT USER AS user,
                       SUBSTRING_INDEX(HOST, ':', 1) AS host_ip,
                       COUNT(*) AS conn_count,
                       SUM(CASE WHEN COMMAND != 'Sleep' THEN 1 ELSE 0 END) AS active_count,
                       MAX(TIME) AS max_time
                FROM information_schema.PROCESSLIST
                GROUP BY USER, SUBSTRING_INDEX(HOST, ':', 1)
                ORDER BY conn_count DESC
                LIMIT %s
                """,
                (_TOP_N_DEFAULT,),
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    groups = [{
        "user": str(r.get("user") or ""), "host_ip": str(r.get("host_ip") or ""),
        "conn_count": _to_int(r.get("conn_count")), "active_count": _to_int(r.get("active_count")),
        "max_time_seconds": _to_int(r.get("max_time")),
    } for r in rows]
    total = sum(g["conn_count"] for g in groups)

    raw_data = {"cluster_endpoint": endpoint, "queried_at": _now_iso(),
                "total_connections": total, "distinct_sources": len(groups), "groups": groups}
    findings: list[dict[str, Any]] = []
    top = groups[0] if groups else None
    if top and total > 0 and top["conn_count"] >= max(50, total * 0.5):
        findings.append({"severity": "warning", "metric": "connection_concentration",
                         "user": top["user"], "host": top["host_ip"], "count": top["conn_count"]})
        status = "warning"
        rec = (f"连接来源集中:{top['user']}@{top['host_ip']} 占 {top['conn_count']}/{total}。"
               "若远超预期,排查该来源连接池配置/泄漏。")
    else:
        status = "ok"
        rec = f"{total} 连接来自 {len(groups)} 个 (user,host),分布正常。"
    return _wrap(status=status, findings=findings, raw_data=raw_data, recommendation=rec)


# ===========================================================================
# Tool 7 — InnoDB buffer pool 命中率
# ===========================================================================
@mcp.tool()
def inspect_buffer_pool(cluster_endpoint: str) -> dict[str, Any]:
    """巡检 InnoDB buffer pool:命中率 / 脏页占比 / 等待 free page / pending reads — 命中率低或频繁等 free page 说明 buffer pool 不足或脏页刷盘跟不上。实例级。endpoint 必传。"""
    endpoint = _resolve_endpoint(cluster_endpoint)
    log.info("[buffer_pool] %s", endpoint)

    conn = _connect(endpoint)
    try:
        with conn.cursor() as cur:
            s = _global_status(cur, [
                "Innodb_buffer_pool_reads", "Innodb_buffer_pool_read_requests",
                "Innodb_buffer_pool_pages_total", "Innodb_buffer_pool_pages_dirty",
                "Innodb_buffer_pool_pages_free", "Innodb_buffer_pool_wait_free",
                "Innodb_buffer_pool_pages_data", "Innodb_data_pending_reads",
            ])
            v = _global_variables(cur, ["innodb_buffer_pool_size", "innodb_page_size"])
    finally:
        conn.close()

    reads = s.get("Innodb_buffer_pool_reads", 0)
    read_requests = s.get("Innodb_buffer_pool_read_requests", 0)
    hit_ratio = (1 - reads / read_requests) * 100 if read_requests > 0 else None
    pages_total = s.get("Innodb_buffer_pool_pages_total", 0)
    pages_dirty = s.get("Innodb_buffer_pool_pages_dirty", 0)
    pages_free = s.get("Innodb_buffer_pool_pages_free", 0)
    wait_free = s.get("Innodb_buffer_pool_wait_free", 0)
    dirty_pct = (pages_dirty * 100.0 / pages_total) if pages_total > 0 else 0.0
    bp_size = _to_int(v.get("innodb_buffer_pool_size"), 0)

    raw_data = {
        "cluster_endpoint": endpoint, "queried_at": _now_iso(),
        "hit_ratio_pct": round(hit_ratio, 4) if hit_ratio is not None else None,
        "buffer_pool_reads": reads, "read_requests": read_requests,
        "pages_total": pages_total, "pages_dirty": pages_dirty, "pages_free": pages_free,
        "dirty_pct": round(dirty_pct, 2), "wait_free_total": wait_free,
        "pending_reads": s.get("Innodb_data_pending_reads", 0),
        "buffer_pool_size_bytes": bp_size, "buffer_pool_size_pretty": _bytes_to_human(bp_size),
    }
    findings: list[dict[str, Any]] = []
    status = "ok"
    rec_parts = []
    if hit_ratio is not None and read_requests > 100000:
        if hit_ratio < 95:
            status = "critical" if hit_ratio < 90 else "warning"
            findings.append({"severity": "critical" if hit_ratio < 90 else "warning",
                             "metric": "buffer_pool_hit_ratio", "value": f"{hit_ratio:.2f}%",
                             "threshold": "95%"})
            rec_parts.append(f"buffer pool 命中率 {hit_ratio:.2f}% 偏低,大量磁盘读。"
                             "考虑增大 innodb_buffer_pool_size(默认实例内存 75%)或优化大表扫描查询。")
        else:
            rec_parts.append(f"buffer pool 命中率 {hit_ratio:.2f}% 良好。")
    if wait_free > 0:
        status = "warning" if status == "ok" else status
        findings.append({"severity": "warning", "metric": "buffer_pool_wait_free", "value": str(wait_free)})
        rec_parts.append(f"曾 {wait_free} 次等待 free page(累计),脏页刷盘跟不上写入,关注 I/O。")
    if dirty_pct >= 75:
        status = "warning" if status == "ok" else status
        findings.append({"severity": "warning", "metric": "dirty_pages_pct", "value": f"{dirty_pct:.1f}%"})
        rec_parts.append(f"脏页占比 {dirty_pct:.1f}% 高,checkpoint 压力大。")
    if not rec_parts:
        rec_parts.append("InnoDB buffer pool 健康。")
    return _wrap(status=status, findings=findings, raw_data=raw_data,
                 recommendation=" ".join(rec_parts))


# ===========================================================================
# Tool 8 — 慢查询 top N(events_statements_summary_by_digest)
# ===========================================================================
@mcp.tool()
def inspect_slow_queries(
    cluster_endpoint: str,
    top_n: int = _TOP_N_DEFAULT,
    order_by: str = "total_latency",
) -> dict[str, Any]:
    """巡检 top N 慢 SQL(performance_schema.events_statements_summary_by_digest):按总耗时/平均耗时/调用次数/扫描行数聚合 — 找性能元凶 SQL(归一化模板,不含具体参数值)。实例级。endpoint 必传。

    Args:
        top_n: 返回前 N(默认 20,上限 100)
        order_by: 排序键 ∈ {total_latency, avg_latency, calls, rows_examined}
    """
    endpoint = _resolve_endpoint(cluster_endpoint)
    n = max(1, min(top_n, _QUERY_LIMIT))
    order_map = {
        "total_latency": "SUM_TIMER_WAIT",
        "avg_latency": "AVG_TIMER_WAIT",
        "calls": "COUNT_STAR",
        "rows_examined": "SUM_ROWS_EXAMINED",
    }
    if order_by not in order_map:
        raise ValueError(f"order_by 必须 ∈ {sorted(order_map)},收到 {order_by!r}")
    log.info("[slow_queries] %s top=%d order=%s", endpoint, n, order_by)

    conn = _connect(endpoint)
    try:
        with conn.cursor() as cur:
            try:
                cur.execute(
                    f"""
                    SELECT SCHEMA_NAME AS schema_name,
                           DIGEST AS digest,
                           COUNT_STAR AS calls,
                           ROUND(SUM_TIMER_WAIT/1e12, 3) AS total_latency_s,
                           ROUND(AVG_TIMER_WAIT/1e9, 3) AS avg_latency_ms,
                           ROUND(MAX_TIMER_WAIT/1e9, 3) AS max_latency_ms,
                           SUM_ROWS_EXAMINED AS rows_examined,
                           SUM_ROWS_SENT AS rows_sent,
                           SUM_NO_INDEX_USED AS no_index_used,
                           SUM_CREATED_TMP_DISK_TABLES AS tmp_disk_tables,
                           DIGEST_TEXT AS digest_text
                    FROM performance_schema.events_statements_summary_by_digest
                    WHERE SCHEMA_NAME IS NOT NULL
                      AND SCHEMA_NAME NOT IN ('performance_schema','information_schema','mysql','sys')
                    ORDER BY {order_map[order_by]} DESC
                    LIMIT %s
                    """,
                    (n,),
                )
                rows = cur.fetchall()
            except pymysql.MySQLError as e:
                return _wrap(status="warning", findings=[],
                             raw_data={"cluster_endpoint": endpoint, "queried_at": _now_iso(),
                                       "error": str(e)[:200]},
                             recommendation=("读 events_statements_summary_by_digest 失败:确认 "
                                             "performance_schema=ON 且 statements_digest consumer 启用。"))
    finally:
        conn.close()

    queries = []
    for r in rows:
        queries.append({
            "schema": str(r.get("schema_name") or ""),
            "digest": str(r.get("digest") or "")[:32],
            "calls": _to_int(r.get("calls")),
            "total_latency_s": _to_float(r.get("total_latency_s")),
            "avg_latency_ms": _to_float(r.get("avg_latency_ms")),
            "max_latency_ms": _to_float(r.get("max_latency_ms")),
            "rows_examined": _to_int(r.get("rows_examined")),
            "rows_sent": _to_int(r.get("rows_sent")),
            "no_index_used": _to_int(r.get("no_index_used")),
            "tmp_disk_tables": _to_int(r.get("tmp_disk_tables")),
            "digest_text": _redact_query(r.get("digest_text")),
        })

    if not queries:
        return _wrap(status="ok", findings=[],
                     raw_data={"cluster_endpoint": endpoint, "queried_at": _now_iso(), "queries": []},
                     recommendation="performance_schema digest 无业务库慢查询数据(可能刚重置/无负载)。")

    max_avg = max(q["avg_latency_ms"] for q in queries)
    if max_avg >= 5000:
        status = "critical"
        rec = (f"top {n} SQL 中平均耗时最高 {max_avg:.0f}ms ≥ 5s。"
               "建议 EXPLAIN 看执行计划、补索引(关注 no_index_used)、拆 SQL。")
    elif max_avg >= 1000:
        status = "warning"
        rec = f"top {n} SQL 中平均耗时最高 {max_avg:.0f}ms ≥ 1s,关注。"
    else:
        status = "ok"
        rec = f"top {n} SQL 性能正常,最慢平均 {max_avg:.0f}ms。"
    findings = [
        {"severity": "critical" if q["avg_latency_ms"] >= 5000
                     else "warning" if q["avg_latency_ms"] >= 1000 else "info",
         "metric": "slow_query", "schema": q["schema"], "digest": q["digest"],
         "avg_ms": q["avg_latency_ms"], "calls": q["calls"],
         "no_index": q["no_index_used"] > 0}
        for q in queries
    ]
    return _wrap(status=status, findings=findings,
                 raw_data={"cluster_endpoint": endpoint, "top_n": n, "order_by": order_by,
                           "queried_at": _now_iso(), "queries": queries},
                 recommendation=rec)


# ===========================================================================
# Tool 9 — 实时慢查询(PROCESSLIST,不依赖 performance_schema)
# ===========================================================================
@mcp.tool()
def inspect_current_queries(
    cluster_endpoint: str,
    min_seconds: int = 5,
) -> dict[str, Any]:
    """巡检当前正在运行且超阈值的查询(information_schema.PROCESSLIST):现在数据库在跑哪些慢 SQL — 不依赖 performance_schema digest,实时快照。实例级。endpoint 必传。

    Args:
        min_seconds: 只看运行时间 ≥ 该值的查询(默认 5s)
    """
    endpoint = _resolve_endpoint(cluster_endpoint)
    if min_seconds < 0:
        raise ValueError(f"min_seconds 必须 >= 0,收到 {min_seconds}")
    log.info("[current_queries] %s min=%ds", endpoint, min_seconds)

    conn = _connect(endpoint)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT ID, USER, SUBSTRING_INDEX(HOST, ':', 1) AS host_ip, DB,
                       TIME, STATE, INFO
                FROM information_schema.PROCESSLIST
                WHERE COMMAND IN ('Query', 'Execute')
                  AND INFO IS NOT NULL
                  AND TIME >= %s
                ORDER BY TIME DESC
                LIMIT %s
                """,
                (min_seconds, _QUERY_LIMIT),
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    queries = [{
        "thread_id": _to_int(r.get("ID")), "user": str(r.get("USER") or ""),
        "host_ip": str(r.get("host_ip") or ""), "db": str(r.get("DB") or ""),
        "time_seconds": _to_int(r.get("TIME")), "state": str(r.get("STATE") or ""),
        "query": _redact_query(r.get("INFO")),
    } for r in rows]

    raw_data = {"cluster_endpoint": endpoint, "min_seconds": min_seconds,
                "queried_at": _now_iso(), "running_queries": queries}
    if not queries:
        return _wrap(status="ok", findings=[], raw_data=raw_data,
                     recommendation=f"无运行 ≥ {min_seconds}s 的查询。")
    max_t = max(q["time_seconds"] for q in queries)
    status = "critical" if max_t >= 300 else "warning"
    findings = [{"severity": "critical" if q["time_seconds"] >= 300 else "warning",
                 "metric": "running_query", "thread_id": q["thread_id"],
                 "time_s": q["time_seconds"], "state": q["state"], "db": q["db"]}
                for q in queries]
    rec = (f"发现 {len(queries)} 个运行 ≥ {min_seconds}s 的查询(最长 {max_t}s)。"
           "看 query/state 定位,必要时 CALL mysql.rds_kill(thread_id)。")
    return _wrap(status=status, findings=findings, raw_data=raw_data, recommendation=rec)


# ===========================================================================
# Tool 10 — 表级 I/O 热点
# ===========================================================================
@mcp.tool()
def inspect_table_io(
    cluster_endpoint: str,
    top_n: int = _TOP_N_DEFAULT,
) -> dict[str, Any]:
    """巡检表级 I/O 热点(performance_schema.table_io_waits_summary_by_table):哪些表读写等待最多 — 定位热点表 / 全表扫描重灾区。实例级。endpoint 必传。"""
    endpoint = _resolve_endpoint(cluster_endpoint)
    n = max(1, min(top_n, _QUERY_LIMIT))
    log.info("[table_io] %s top=%d", endpoint, n)

    conn = _connect(endpoint)
    try:
        with conn.cursor() as cur:
            try:
                cur.execute(
                    """
                    SELECT OBJECT_SCHEMA AS object_schema, OBJECT_NAME AS object_name,
                           COUNT_READ AS count_read, COUNT_WRITE AS count_write,
                           ROUND(SUM_TIMER_READ/1e9, 2) AS read_latency_ms,
                           ROUND(SUM_TIMER_WRITE/1e9, 2) AS write_latency_ms,
                           ROUND(SUM_TIMER_WAIT/1e9, 2) AS total_latency_ms
                    FROM performance_schema.table_io_waits_summary_by_table
                    WHERE OBJECT_SCHEMA NOT IN ('performance_schema','information_schema','mysql','sys')
                      AND COUNT_STAR > 0
                    ORDER BY SUM_TIMER_WAIT DESC
                    LIMIT %s
                    """,
                    (n,),
                )
                rows = cur.fetchall()
            except pymysql.MySQLError as e:
                return _wrap(status="warning", findings=[],
                             raw_data={"cluster_endpoint": endpoint, "queried_at": _now_iso(),
                                       "error": str(e)[:200]},
                             recommendation="读 table_io_waits_summary 失败:确认 performance_schema 已开启。")
    finally:
        conn.close()

    tables = [{
        "table": f"{r.get('object_schema')}.{r.get('object_name')}",
        "count_read": _to_int(r.get("count_read")), "count_write": _to_int(r.get("count_write")),
        "read_latency_ms": _to_float(r.get("read_latency_ms")),
        "write_latency_ms": _to_float(r.get("write_latency_ms")),
        "total_latency_ms": _to_float(r.get("total_latency_ms")),
    } for r in rows]

    raw_data = {"cluster_endpoint": endpoint, "queried_at": _now_iso(), "top_tables": tables}
    if not tables:
        return _wrap(status="ok", findings=[], raw_data=raw_data, recommendation="无表 I/O 统计数据。")
    findings = [{"severity": "info", "metric": "table_io_hot", "table": t["table"],
                 "reads": t["count_read"], "writes": t["count_write"],
                 "total_latency_ms": t["total_latency_ms"]} for t in tables[:10]]
    rec = (f"I/O 最热表:{tables[0]['table']}(读 {tables[0]['count_read']} / 写 {tables[0]['count_write']})。"
           "结合 inspect_slow_queries / inspect_index_usage 看是否缺索引导致热点表全扫。")
    return _wrap(status="ok", findings=findings, raw_data=raw_data, recommendation=rec)


# ===========================================================================
# Tool 11 — 内部临时表落盘
# ===========================================================================
@mcp.tool()
def inspect_temp_tables(cluster_endpoint: str) -> dict[str, Any]:
    """巡检内部临时表:磁盘临时表占比(Created_tmp_disk_tables / Created_tmp_tables)— 占比高说明 sort/group by/join 超过 tmp_table_size 落盘,严重拖慢查询。实例级。endpoint 必传。"""
    endpoint = _resolve_endpoint(cluster_endpoint)
    log.info("[temp_tables] %s", endpoint)

    conn = _connect(endpoint)
    try:
        with conn.cursor() as cur:
            s = _global_status(cur, [
                "Created_tmp_tables", "Created_tmp_disk_tables", "Created_tmp_files",
                "Sort_merge_passes",
            ])
            v = _global_variables(cur, ["tmp_table_size", "max_heap_table_size"])
    finally:
        conn.close()

    tmp_tables = s.get("Created_tmp_tables", 0)
    tmp_disk = s.get("Created_tmp_disk_tables", 0)
    disk_pct = (tmp_disk * 100.0 / tmp_tables) if tmp_tables > 0 else 0.0

    raw_data = {
        "cluster_endpoint": endpoint, "queried_at": _now_iso(),
        "created_tmp_tables": tmp_tables, "created_tmp_disk_tables": tmp_disk,
        "disk_tmp_pct": round(disk_pct, 2),
        "created_tmp_files": s.get("Created_tmp_files", 0),
        "sort_merge_passes": s.get("Sort_merge_passes", 0),
        "tmp_table_size_bytes": _to_int(v.get("tmp_table_size")),
        "tmp_table_size_pretty": _bytes_to_human(_to_int(v.get("tmp_table_size"))),
        "max_heap_table_size_bytes": _to_int(v.get("max_heap_table_size")),
    }
    findings: list[dict[str, Any]] = []
    status = "ok"
    rec_parts = []
    if tmp_tables > 1000:
        if disk_pct >= 50:
            status = "critical" if disk_pct >= 75 else "warning"
            findings.append({"severity": status, "metric": "disk_tmp_table_pct",
                             "value": f"{disk_pct:.1f}%", "threshold": "50%"})
            rec_parts.append(f"磁盘临时表占比 {disk_pct:.1f}% 高(累计 {tmp_disk}/{tmp_tables})。"
                             "大量 sort/group by/join 落盘。考虑增大 tmp_table_size/max_heap_table_size、"
                             "优化查询减少大结果集排序、补索引避免 filesort。")
        else:
            rec_parts.append(f"磁盘临时表占比 {disk_pct:.1f}%,可接受。")
    if s.get("Sort_merge_passes", 0) > 10000:
        status = "warning" if status == "ok" else status
        findings.append({"severity": "warning", "metric": "sort_merge_passes",
                         "value": str(s["Sort_merge_passes"])})
        rec_parts.append("Sort_merge_passes 高,sort_buffer_size 可能不足。")
    if not rec_parts:
        rec_parts.append(f"临时表健康,磁盘占比 {disk_pct:.1f}%。")
    return _wrap(status=status, findings=findings, raw_data=raw_data,
                 recommendation=" ".join(rec_parts))


# ===========================================================================
# Tool 12 — 关键 GLOBAL STATUS 计数器
# ===========================================================================
@mcp.tool()
def inspect_global_status(cluster_endpoint: str) -> dict[str, Any]:
    """巡检关键 GLOBAL STATUS:QPS / 慢查询数 / 中止连接 / 全表扫描 / 锁等待计数 — 实例整体健康概览。实例级。endpoint 必传。"""
    endpoint = _resolve_endpoint(cluster_endpoint)
    log.info("[global_status] %s", endpoint)

    conn = _connect(endpoint)
    try:
        with conn.cursor() as cur:
            s = _global_status(cur, [
                "Uptime", "Questions", "Queries", "Slow_queries",
                "Aborted_connects", "Aborted_clients",
                "Select_full_join", "Select_scan", "Handler_read_rnd_next",
                "Innodb_row_lock_waits", "Innodb_row_lock_time_avg",
                "Table_locks_waited", "Threads_running",
            ])
    finally:
        conn.close()

    uptime = s.get("Uptime", 0)
    questions = s.get("Questions", 0)
    qps = round(questions / uptime, 2) if uptime > 0 else 0.0
    slow = s.get("Slow_queries", 0)
    slow_pct = (slow * 100.0 / questions) if questions > 0 else 0.0

    raw_data = {
        "cluster_endpoint": endpoint, "queried_at": _now_iso(),
        "uptime_seconds": uptime, "uptime_pretty": f"{uptime // 86400}d {(uptime % 86400) // 3600}h",
        "questions": questions, "avg_qps_since_start": qps,
        "slow_queries": slow, "slow_query_pct": round(slow_pct, 4),
        "aborted_connects": s.get("Aborted_connects", 0),
        "aborted_clients": s.get("Aborted_clients", 0),
        "select_full_join": s.get("Select_full_join", 0),
        "select_scan": s.get("Select_scan", 0),
        "innodb_row_lock_waits": s.get("Innodb_row_lock_waits", 0),
        "innodb_row_lock_time_avg_ms": s.get("Innodb_row_lock_time_avg", 0),
        "table_locks_waited": s.get("Table_locks_waited", 0),
        "threads_running": s.get("Threads_running", 0),
    }
    findings: list[dict[str, Any]] = []
    status = "ok"
    rec_parts = [f"自启动 QPS≈{qps},uptime {raw_data['uptime_pretty']}。"]
    if slow_pct >= 5 and questions > 10000:
        status = "warning"
        findings.append({"severity": "warning", "metric": "slow_query_pct",
                         "value": f"{slow_pct:.2f}%", "threshold": "5%"})
        rec_parts.append(f"慢查询占比 {slow_pct:.2f}%,用 inspect_slow_queries 深挖。")
    if s.get("Select_full_join", 0) > 1000:
        status = "warning" if status == "ok" else status
        findings.append({"severity": "warning", "metric": "select_full_join",
                         "value": str(s["Select_full_join"])})
        rec_parts.append(f"无索引 join 累计 {s['Select_full_join']} 次,排查缺索引的 join。")
    if s.get("Aborted_connects", 0) > 100:
        rec_parts.append(f"中止连接 {s['Aborted_connects']} 次,排查认证失败/网络/超时。")
    return _wrap(status=status, findings=findings, raw_data=raw_data,
                 recommendation=" ".join(rec_parts))


# ===========================================================================
# Tool 13 — 表 / 索引大小 top N
# ===========================================================================
@mcp.tool()
def inspect_table_sizes(
    cluster_endpoint: str,
    top_n: int = _TOP_N_DEFAULT,
    database: str = "",
) -> dict[str, Any]:
    """巡检表 / 索引大小 top N(information_schema.TABLES):数据 + 索引各占多少、行数 — 容量规划、找大表。不传 database 则跨所有业务库,传了只看该库。实例级(可选 database 过滤)。endpoint 必传。"""
    endpoint = _resolve_endpoint(cluster_endpoint)
    n = max(1, min(top_n, _QUERY_LIMIT))
    log.info("[table_sizes] %s top=%d db=%s", endpoint, n, database or "(all)")

    conn = _connect(endpoint)
    try:
        with conn.cursor() as cur:
            params: list[Any] = []
            db_filter = ""
            if database:
                db_filter = "AND TABLE_SCHEMA = %s"
                params.append(database)
            params.append(n)
            cur.execute(
                f"""
                SELECT TABLE_SCHEMA AS table_schema, TABLE_NAME AS table_name,
                       TABLE_ROWS AS table_rows,
                       DATA_LENGTH AS data_length, INDEX_LENGTH AS index_length,
                       DATA_LENGTH + INDEX_LENGTH AS total_length,
                       ENGINE AS engine
                FROM information_schema.TABLES
                WHERE TABLE_TYPE = 'BASE TABLE'
                  AND TABLE_SCHEMA NOT IN ('performance_schema','information_schema','mysql','sys')
                  {db_filter}
                ORDER BY DATA_LENGTH + INDEX_LENGTH DESC
                LIMIT %s
                """,
                tuple(params),
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    tables = [{
        "schema": str(r.get("table_schema") or ""), "table": str(r.get("table_name") or ""),
        "estimated_rows": _to_int(r.get("table_rows")),
        "data_bytes": _to_int(r.get("data_length")), "index_bytes": _to_int(r.get("index_length")),
        "total_bytes": _to_int(r.get("total_length")),
        "total_pretty": _bytes_to_human(_to_int(r.get("total_length"))),
        "index_pretty": _bytes_to_human(_to_int(r.get("index_length"))),
        "engine": str(r.get("engine") or ""),
    } for r in rows]

    raw_data = {"cluster_endpoint": endpoint, "database": database or "(all business dbs)",
                "queried_at": _now_iso(), "top_tables": tables}
    if not tables:
        return _wrap(status="ok", findings=[], raw_data=raw_data, recommendation="未找到业务表。")
    findings = [{"severity": "info", "metric": "table_size", "table": f"{t['schema']}.{t['table']}",
                 "size": t["total_pretty"], "rows": t["estimated_rows"]} for t in tables[:10]]
    non_innodb = [t for t in tables if t["engine"] and t["engine"].upper() != "INNODB"]
    status = "ok"
    rec = f"最大表 {tables[0]['schema']}.{tables[0]['table']} = {tables[0]['total_pretty']}。"
    if non_innodb:
        status = "warning"
        findings.append({"severity": "warning", "metric": "non_innodb_engine",
                         "count": len(non_innodb), "tables": [f"{t['schema']}.{t['table']}" for t in non_innodb[:5]]})
        rec += f" ⚠ 发现 {len(non_innodb)} 张非 InnoDB 表(MyISAM 不 crash-safe/复制不可靠),建议转 InnoDB。"
    return _wrap(status=status, findings=findings, raw_data=raw_data, recommendation=rec)


# ===========================================================================
# Tool 14 — 列出库里有哪些表(发现入口)
# ===========================================================================
@mcp.tool()
def inspect_schema_objects(
    cluster_endpoint: str,
    database: str = "",
) -> dict[str, Any]:
    """巡检库里有哪些表(information_schema.TABLES):表名 / 行数 / 大小 / 引擎概览 — 排障发现入口,不知道库里有什么时先用它。不传 database 则按库汇总;传了列该库所有表。endpoint 必传。"""
    endpoint = _resolve_endpoint(cluster_endpoint)
    log.info("[schema_objects] %s db=%s", endpoint, database or "(summary)")

    conn = _connect(endpoint)
    try:
        with conn.cursor() as cur:
            if not database:
                # 按 schema 汇总
                cur.execute(
                    """
                    SELECT TABLE_SCHEMA AS table_schema,
                           COUNT(*) AS table_count,
                           SUM(TABLE_ROWS) AS total_rows,
                           SUM(DATA_LENGTH + INDEX_LENGTH) AS total_bytes
                    FROM information_schema.TABLES
                    WHERE TABLE_TYPE = 'BASE TABLE'
                      AND TABLE_SCHEMA NOT IN ('performance_schema','information_schema','mysql','sys')
                    GROUP BY TABLE_SCHEMA
                    ORDER BY total_bytes DESC
                    """
                )
                schemas = [{
                    "schema": str(r.get("table_schema") or ""),
                    "table_count": _to_int(r.get("table_count")),
                    "total_rows": _to_int(r.get("total_rows")),
                    "total_pretty": _bytes_to_human(_to_int(r.get("total_bytes"))),
                } for r in cur.fetchall()]
                raw_data = {"cluster_endpoint": endpoint, "mode": "schema_summary",
                            "queried_at": _now_iso(), "schemas": schemas}
                rec = (f"共 {len(schemas)} 个业务库。传 database=库名 可列该库所有表。"
                       if schemas else "无业务库(只有系统库)。")
                return _wrap(status="ok", findings=[], raw_data=raw_data, recommendation=rec)

            cur.execute(
                """
                SELECT TABLE_NAME AS table_name, TABLE_ROWS AS table_rows,
                       DATA_LENGTH + INDEX_LENGTH AS total_bytes, ENGINE AS engine
                FROM information_schema.TABLES
                WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'
                ORDER BY DATA_LENGTH + INDEX_LENGTH DESC
                LIMIT %s
                """,
                (database, _QUERY_LIMIT),
            )
            tables = [{
                "table": str(r.get("table_name") or ""),
                "estimated_rows": _to_int(r.get("table_rows")),
                "total_pretty": _bytes_to_human(_to_int(r.get("total_bytes"))),
                "engine": str(r.get("engine") or ""),
            } for r in cur.fetchall()]
    finally:
        conn.close()

    raw_data = {"cluster_endpoint": endpoint, "database": database, "mode": "table_list",
                "queried_at": _now_iso(), "table_count": len(tables), "tables": tables}
    rec = (f"库 {database} 有 {len(tables)} 张表(最多列 {_QUERY_LIMIT})。"
           if tables else f"库 {database} 无表或不存在。")
    return _wrap(status="ok", findings=[], raw_data=raw_data, recommendation=rec)


# ===========================================================================
# Tool 15 — 未使用索引(sys.schema_unused_indexes)
# ===========================================================================
@mcp.tool()
def inspect_index_usage(cluster_endpoint: str) -> dict[str, Any]:
    """巡检未使用索引(sys.schema_unused_indexes):自上次 server 启动以来从未被用到的索引 — 冗余索引候选,删除可省空间 + 加快写入。需 performance_schema + sys schema。实例级。endpoint 必传。"""
    endpoint = _resolve_endpoint(cluster_endpoint)
    log.info("[index_usage] %s", endpoint)

    conn = _connect(endpoint)
    try:
        with conn.cursor() as cur:
            try:
                cur.execute(
                    """
                    SELECT object_schema, object_name, index_name
                    FROM sys.schema_unused_indexes
                    WHERE object_schema NOT IN ('performance_schema','information_schema','mysql','sys')
                    ORDER BY object_schema, object_name
                    LIMIT %s
                    """,
                    (_QUERY_LIMIT,),
                )
                unused = [{
                    "schema": str(r.get("object_schema") or ""),
                    "table": str(r.get("object_name") or ""),
                    "index": str(r.get("index_name") or ""),
                } for r in cur.fetchall()]
            except pymysql.MySQLError as e:
                return _wrap(status="warning", findings=[],
                             raw_data={"cluster_endpoint": endpoint, "queried_at": _now_iso(),
                                       "error": str(e)[:200]},
                             recommendation=("读 sys.schema_unused_indexes 失败:确认 performance_schema "
                                             "已开启且 table_io_waits instrument 启用。注意:统计自上次重启累计,"
                                             "重启不久的实例可能误报。"))
    finally:
        conn.close()

    raw_data = {"cluster_endpoint": endpoint, "queried_at": _now_iso(),
                "unused_index_count": len(unused), "unused_indexes": unused}
    if not unused:
        return _wrap(status="ok", findings=[], raw_data=raw_data,
                     recommendation="无未使用索引(或实例刚重启统计未积累)。")
    findings = [{"severity": "info", "metric": "unused_index",
                 "index": f"{u['schema']}.{u['table']}.{u['index']}"} for u in unused[:15]]
    return _wrap(status="warning", findings=findings, raw_data=raw_data,
                 recommendation=(f"发现 {len(unused)} 个未使用索引(自上次重启从未命中)。"
                                 "确认实例已稳定运行一段时间后,可考虑删除省空间 + 加快写入。"
                                 "⚠ 注意覆盖低频但关键的查询(如月度报表),删前确认。"))


# ===========================================================================
# Tool 16 — auto_increment 容量耗尽风险
# ===========================================================================
@mcp.tool()
def inspect_auto_increment(
    cluster_endpoint: str,
    usage_threshold_pct: float = 70.0,
) -> dict[str, Any]:
    """巡检 auto_increment 容量:主键自增值接近列类型上限(int 爆 21 亿、smallint 爆 6.5 万)— 耗尽会导致 INSERT 全部失败、业务停摆。跨所有业务库扫描。实例级。endpoint 必传。

    Args:
        usage_threshold_pct: 使用率超此值才报(默认 70%)
    """
    endpoint = _resolve_endpoint(cluster_endpoint)
    log.info("[auto_increment] %s threshold=%.0f%%", endpoint, usage_threshold_pct)

    # 各整型有符号上限(auto_increment 列通常 unsigned,但保守用 signed 上限更安全提醒)
    conn = _connect(endpoint)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT t.TABLE_SCHEMA AS table_schema, t.TABLE_NAME AS table_name,
                       t.AUTO_INCREMENT AS auto_increment,
                       c.COLUMN_NAME AS column_name, c.COLUMN_TYPE AS column_type,
                       c.DATA_TYPE AS data_type
                FROM information_schema.TABLES t
                JOIN information_schema.COLUMNS c
                  ON c.TABLE_SCHEMA = t.TABLE_SCHEMA AND c.TABLE_NAME = t.TABLE_NAME
                 AND c.EXTRA = 'auto_increment'
                WHERE t.AUTO_INCREMENT IS NOT NULL
                  AND t.TABLE_SCHEMA NOT IN ('performance_schema','information_schema','mysql','sys')
                ORDER BY t.AUTO_INCREMENT DESC
                LIMIT %s
                """,
                (_QUERY_LIMIT,),
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    # 列类型上限(unsigned)
    limits = {
        "tinyint": 255, "smallint": 65535, "mediumint": 16777215,
        "int": 4294967295, "integer": 4294967295, "bigint": 18446744073709551615,
    }
    signed_limits = {
        "tinyint": 127, "smallint": 32767, "mediumint": 8388607,
        "int": 2147483647, "integer": 2147483647, "bigint": 9223372036854775807,
    }
    items = []
    for r in rows:
        data_type = str(r.get("data_type") or "").lower()
        col_type = str(r.get("column_type") or "").lower()
        is_unsigned = "unsigned" in col_type
        limit = (limits if is_unsigned else signed_limits).get(data_type)
        if not limit:
            continue
        cur_val = _to_int(r.get("auto_increment"))
        pct = cur_val * 100.0 / limit
        items.append({
            "schema": str(r.get("table_schema") or ""), "table": str(r.get("table_name") or ""),
            "column": str(r.get("column_name") or ""), "column_type": col_type,
            "current_value": cur_val, "max_value": limit,
            "usage_pct": round(pct, 4),
        })
    items = [i for i in items if i["usage_pct"] >= usage_threshold_pct]
    items.sort(key=lambda x: x["usage_pct"], reverse=True)

    raw_data = {"cluster_endpoint": endpoint, "usage_threshold_pct": usage_threshold_pct,
                "queried_at": _now_iso(), "at_risk_tables": items}
    if not items:
        return _wrap(status="ok", findings=[], raw_data=raw_data,
                     recommendation=f"无 auto_increment 使用率 ≥ {usage_threshold_pct}% 的表,容量健康。")
    max_pct = items[0]["usage_pct"]
    status = "critical" if max_pct >= 90 else "warning"
    findings = [{"severity": "critical" if i["usage_pct"] >= 90 else "warning",
                 "metric": "auto_increment_capacity", "table": f"{i['schema']}.{i['table']}",
                 "column": i["column"], "usage_pct": f"{i['usage_pct']:.2f}%",
                 "type": i["column_type"]} for i in items]
    rec = (f"发现 {len(items)} 张表 auto_increment 使用率 ≥ {usage_threshold_pct}%,"
           f"最高 {items[0]['schema']}.{items[0]['table']} = {max_pct:.2f}%。"
           "耗尽后 INSERT 报 duplicate key 全失败。建议:改列类型 int→bigint(需 DDL,提前规划),"
           "或检查是否有大量删除留下空洞(可重置 auto_increment)。")
    return _wrap(status=status, findings=findings, raw_data=raw_data, recommendation=rec)


# ===========================================================================
# Tool 17 — 关键 GLOBAL VARIABLES
# ===========================================================================
@mcp.tool()
def inspect_variables(cluster_endpoint: str) -> dict[str, Any]:
    """巡检关键 GLOBAL VARIABLES:buffer pool / 连接 / 超时 / binlog / 临时表等核心参数 — 参数配置审阅,排查"为什么这么配"。实例级。endpoint 必传。"""
    endpoint = _resolve_endpoint(cluster_endpoint)
    log.info("[variables] %s", endpoint)

    keys = [
        "version", "innodb_buffer_pool_size", "innodb_buffer_pool_instances",
        "max_connections", "wait_timeout", "interactive_timeout",
        "innodb_lock_wait_timeout", "innodb_flush_log_at_trx_commit",
        "sync_binlog", "binlog_format", "tmp_table_size", "max_heap_table_size",
        "long_query_time", "slow_query_log", "innodb_io_capacity",
        "innodb_flush_method", "default_storage_engine", "transaction_isolation",
        "max_allowed_packet", "read_only", "super_read_only",
    ]
    conn = _connect(endpoint)
    try:
        with conn.cursor() as cur:
            v = _global_variables(cur, keys)
    finally:
        conn.close()

    raw_data = {"cluster_endpoint": endpoint, "queried_at": _now_iso(), "variables": v}
    findings: list[dict[str, Any]] = []
    rec_parts = []

    if v.get("binlog_format") and v["binlog_format"].upper() != "ROW":
        findings.append({"severity": "warning", "metric": "binlog_format",
                         "value": v["binlog_format"]})
        rec_parts.append(f"binlog_format={v['binlog_format']}(非 ROW),"
                         "STATEMENT/MIXED 可能导致复制不一致,建议 ROW。")
    if v.get("innodb_flush_log_at_trx_commit") and v["innodb_flush_log_at_trx_commit"] != "1":
        findings.append({"severity": "info", "metric": "innodb_flush_log_at_trx_commit",
                         "value": v["innodb_flush_log_at_trx_commit"]})
        rec_parts.append(f"innodb_flush_log_at_trx_commit={v['innodb_flush_log_at_trx_commit']}"
                         "(非 1),牺牲持久性换性能,确认可接受崩溃丢 1s 事务。")
    if v.get("slow_query_log") and v["slow_query_log"] in ("OFF", "0"):
        rec_parts.append("slow_query_log 关闭,建议开启便于慢查询分析。")
    status = "warning" if findings else "ok"
    if not rec_parts:
        rec_parts.append(f"核心参数审阅完成,MySQL {v.get('version', '?')},无明显配置风险。")
    return _wrap(status=status, findings=findings, raw_data=raw_data,
                 recommendation=" ".join(rec_parts))


# ===========================================================================
# Tool 18 — 复制延迟 / 链路状态
# ===========================================================================
@mcp.tool()
def inspect_replica_status(cluster_endpoint: str) -> dict[str, Any]:
    """巡检复制状态:RDS MySQL read replica 走 SHOW REPLICA STATUS(延迟秒数 + IO/SQL 线程状态 + 错误);Aurora 走 information_schema.REPLICA_HOST_STATUS(各 reader 延迟)。自动适配。连 replica/reader 看更准。endpoint 必传。"""
    endpoint = _resolve_endpoint(cluster_endpoint)
    log.info("[replica_status] %s", endpoint)

    conn = _connect(endpoint)
    try:
        with conn.cursor() as cur:
            # 先试 Aurora REPLICA_HOST_STATUS
            aurora_rows = None
            try:
                cur.execute(
                    """
                    SELECT SERVER_ID AS server_id, SESSION_ID AS session_id,
                           IF_REPLICA AS is_replica,
                           REPLICA_LAG_IN_MILLISECONDS AS lag_ms,
                           CPU AS cpu
                    FROM information_schema.REPLICA_HOST_STATUS
                    """
                )
                aurora_rows = cur.fetchall()
            except pymysql.MySQLError:
                aurora_rows = None

            replica_status = None
            if not aurora_rows:
                try:
                    cur.execute("SHOW REPLICA STATUS")
                    replica_status = cur.fetchone()
                except pymysql.MySQLError:
                    try:
                        cur.execute("SHOW SLAVE STATUS")  # 5.7 兼容
                        replica_status = cur.fetchone()
                    except pymysql.MySQLError:
                        replica_status = None
    finally:
        conn.close()

    # Aurora 路径
    if aurora_rows:
        replicas = [{
            "server_id": str(r.get("server_id") or ""),
            "is_replica": bool(r.get("is_replica")),
            "lag_ms": _to_int(r.get("lag_ms")),
            "cpu": _to_float(r.get("cpu")),
        } for r in aurora_rows]
        reader_lags = [r["lag_ms"] for r in replicas if r["is_replica"]]
        max_lag = max(reader_lags) if reader_lags else 0
        raw_data = {"cluster_endpoint": endpoint, "engine_type": "aurora",
                    "queried_at": _now_iso(), "nodes": replicas, "max_reader_lag_ms": max_lag}
        if not reader_lags:
            return _wrap(status="warning", findings=[], raw_data=raw_data,
                         recommendation="Aurora 集群当前无 reader 节点(单实例),无复制延迟数据。")
        status = "critical" if max_lag >= 10000 else "warning" if max_lag >= 1000 else "ok"
        findings = [{"severity": "critical" if r["lag_ms"] >= 10000 else "warning" if r["lag_ms"] >= 1000 else "info",
                     "metric": "aurora_reader_lag", "server_id": r["server_id"], "lag_ms": r["lag_ms"]}
                    for r in replicas if r["is_replica"]]
        rec = (f"Aurora {len(reader_lags)} 个 reader,最大延迟 {max_lag}ms。"
               + (" 延迟偏高,关注写负载/reader 规格。" if max_lag >= 1000 else " 复制健康。"))
        return _wrap(status=status, findings=findings, raw_data=raw_data, recommendation=rec)

    # RDS MySQL read replica 路径
    if not replica_status:
        return _wrap(status="ok", findings=[],
                     raw_data={"cluster_endpoint": endpoint, "engine_type": "standalone_or_source",
                               "queried_at": _now_iso()},
                     recommendation=("此节点非 read replica(SHOW REPLICA STATUS 为空)。"
                                     "这是 source/单实例,或需连 replica endpoint 查复制延迟。"))

    def g(*names):
        for nm in names:
            if nm in replica_status and replica_status[nm] is not None:
                return replica_status[nm]
        return None

    io_running = str(g("Replica_IO_Running", "Slave_IO_Running") or "")
    sql_running = str(g("Replica_SQL_Running", "Slave_SQL_Running") or "")
    lag = g("Seconds_Behind_Source", "Seconds_Behind_Master")
    last_error = str(g("Last_Error") or "")
    last_io_error = str(g("Last_IO_Error") or "")
    lag_val = _to_int(lag) if lag is not None else None

    raw_data = {
        "cluster_endpoint": endpoint, "engine_type": "rds_mysql_replica",
        "queried_at": _now_iso(),
        "io_thread_running": io_running, "sql_thread_running": sql_running,
        "seconds_behind_source": lag_val,
        "last_error": _redact_query(last_error) if last_error else "",
        "last_io_error": _redact_query(last_io_error) if last_io_error else "",
    }
    findings: list[dict[str, Any]] = []
    status = "ok"
    rec_parts = []
    if io_running != "Yes" or sql_running != "Yes":
        status = "critical"
        findings.append({"severity": "critical", "metric": "replication_thread_down",
                         "io_running": io_running, "sql_running": sql_running})
        rec_parts.append(f"⚠ 复制线程异常(IO={io_running}, SQL={sql_running})。"
                         "检查 Last_Error/Last_IO_Error,复制已中断。")
    elif lag_val is None:
        status = "warning"
        rec_parts.append("Seconds_Behind_Source 为 NULL,复制可能未运行或正在追赶。")
    elif lag_val >= 30:
        status = "critical" if lag_val >= 300 else "warning"
        findings.append({"severity": status, "metric": "replica_lag",
                         "value": f"{lag_val}s", "threshold": "30s"})
        rec_parts.append(f"复制延迟 {lag_val}s。写负载过重/replica 规格不足/单线程复制瓶颈。")
    else:
        rec_parts.append(f"复制健康,延迟 {lag_val}s,IO/SQL 线程正常。")
    return _wrap(status=status, findings=findings, raw_data=raw_data,
                 recommendation=" ".join(rec_parts))


if __name__ == "__main__":
    log.info("starting RDS/Aurora MySQL inspect MCP server on :8000 with 18 tools")
    mcp.run(transport="streamable-http")
