"""ElastiCache (Redis / Valkey 引擎) 巡检 MCP server(13 个语义化巡检 tool,IAM 形态)。

关键设计:**endpoint 不注入容器**。本 server 是通用 ElastiCache 巡检工具,不绑定任何
实例。每个 tool 的 `endpoint` 是**必传参数**,由 DevOps Agent 在调用时按用户上下文给出
(如"巡检 my-cache 的内存" → endpoint=my-cache.xxx.cache.amazonaws.com)。
一套 server 天然巡检 N 个同类 ElastiCache 实例。

适用引擎:ElastiCache for Redis / ElastiCache for Valkey / 自建 Redis(协议兼容)。
**不支持 Memcached**(无 INFO replication / SLOWLOG / persistence,协议完全不同)。

暴露的 tool 全集:

【核心 11 个(数据面巡检)】
  inspect_overview        总览(版本 / uptime / role / 客户端数)
  inspect_memory          内存使用 + 碎片化 + maxmemory 占比
  inspect_slow_queries    慢命令 top N(SLOWLOG GET)
  inspect_clients         客户端连接分布(CLIENT LIST)
  inspect_keyspace        各 db 的 key 数 / expire 占比 / avg TTL
  inspect_stats           命中率 + OPS + 连接拒绝 + 命令统计 top
  inspect_replication     主从复制状态 + 延迟
  inspect_persistence     RDB / AOF 状态 + last save / fsync 错误
  inspect_big_keys        SCAN 全库找 top N 内存占用大 key(MEMORY USAGE)
  inspect_hot_keys        SCAN 全库找 top N 访问频繁 key(OBJECT FREQ,LFU 模式)
  inspect_eviction        eviction 速率 + maxmemory 占比 + policy 健康

【ElastiCache 特性补充 2 个(基于 elasticache skill)】
  inspect_cluster_mode    cluster mode(分片 / hash slot 分布 / 各 shard 状态)
  inspect_pubsub          pub/sub channel 数 + client output buffer(fan-out 内存风险)

容器外部注入(由 target stack 在 Runtime 上设环境变量,**不含 endpoint**):
  REDIS_PORT                默认 6379
  REDIS_USE_TLS             "auto"(默认,按 endpoint 探测) / "true"(强制 TLS) / "false"(强制明文)
  REDIS_AUTH_SECRET_NAME    ACL/AUTH 凭据 Secrets Manager 路径
                              (Secret JSON: {"username":"...", "password":"..."})
  REDIS_AUTH_SECRET_REGION  Secret 所在 region
  AWS_REGION                Runtime 所在 region
  LOG_LEVEL                 可选,默认 INFO

设计契约:
  - FastMCP host=0.0.0.0 port=8000 stateless_http=True(SHALL NOT #6 / #17)
  - **Secret 5 分钟 TTL 缓存**(P7)
  - 用 redis-py(redis ~= 5.2,Valkey 8 / Redis 7 协议兼容)
  - **TLS 按 endpoint 自适应**(REDIS_USE_TLS=auto):TLS 优先,失败回退明文,决策缓存
    (TLS 是实例属性,一套 server 巡检 N 个集群时有的开 in-transit encryption 有的没开)
  - **SCAN 类 tool(big_keys/hot_keys)建议传 replica endpoint**(P4,减 master CPU)
  - 不返回 key value,只返回 key name / size / type / TTL(SHALL NOT #3)
  - 返回结构严格 conventions A8
  - ElastiCache managed service 禁用 CONFIG 命令,相关读取优雅降级到 INFO 字段 / 默认值
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
import redis
from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import PlainTextResponse

# ---------------------------------------------------------------------------
# 模块级常量
# ---------------------------------------------------------------------------
_TOP_N_DEFAULT: int = 20
_QUERY_LIMIT: int = 100
_CONNECT_TIMEOUT_SECONDS: int = 10
_SOCKET_TIMEOUT_SECONDS: int = 10
# big_keys / hot_keys 扫描上限(防止数百万 key 库扫挂)
_SCAN_DEFAULT_MAX: int = 5000
_SCAN_HARD_LIMIT: int = 50000
# pipeline 批大小(MEMORY USAGE 批量调用)
_PIPELINE_BATCH: int = 200
# Secret 缓存 TTL(P7)
_SECRET_TTL_SECONDS: int = 300

# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
_LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=_LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("elasticache_inspect")

# ---------------------------------------------------------------------------
# 容器启动时一次性读取的环境变量(不含 endpoint —— endpoint 是 tool 调用必传)
# ---------------------------------------------------------------------------
_AWS_REGION: str = os.environ.get("AWS_REGION", "us-east-1")
_REDIS_PORT: int = int(os.environ.get("REDIS_PORT", "6379"))
# TLS 模式三档:
#   "auto"(默认)— 按 endpoint 探测:TLS 优先,握手失败回退明文,决策缓存(见 _resolve_tls)
#   "true"        — 强制 TLS(客户全集群都开 in-transit encryption 时省一次探测握手)
#   "false"       — 强制明文(客户全集群都不开 TLS)
# TLS 是实例属性,不是容器属性:一套 server 巡检 N 个集群,有的开有的不开,所以默认 auto。
_REDIS_TLS_MODE: str = os.environ.get("REDIS_USE_TLS", "auto").strip().lower()
if _REDIS_TLS_MODE in ("1", "yes", "on"):
    _REDIS_TLS_MODE = "true"
elif _REDIS_TLS_MODE in ("0", "no", "off"):
    _REDIS_TLS_MODE = "false"
if _REDIS_TLS_MODE not in ("auto", "true", "false"):
    log.warning("REDIS_USE_TLS=%r 非法,回退 auto", _REDIS_TLS_MODE)
    _REDIS_TLS_MODE = "auto"
# 凭据 Secret 名:优先 REDIS_AUTH_SECRET_NAME,fallback 到 target_stack 通用注入的 DB_SECRET_NAME。
# (target_stack 统一注入 DB_SECRET_NAME,这里兼容它,无需为 elasticache 特例化 stack)
_REDIS_AUTH_SECRET_NAME: str = (
    os.environ.get("REDIS_AUTH_SECRET_NAME")
    or os.environ.get("DB_SECRET_NAME", "")
)
_REDIS_AUTH_SECRET_REGION: str = (
    os.environ.get("REDIS_AUTH_SECRET_REGION") or _AWS_REGION
)

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
_secret_cache: dict[str, tuple[float, tuple[str | None, str | None]]] = {}
_secret_cache_lock = Lock()

# TLS 自适应决策缓存:endpoint -> bool(True=用 TLS)。
# auto 模式下首次连接时探测一次,后续复用,避免每次 tool call 都多一次握手。
_tls_cache: dict[str, bool] = {}
_tls_cache_lock = Lock()


def _fetch_auth() -> tuple[str | None, str | None]:
    """如果配置了 ACL/AUTH Secret,返回 (username, password);否则 (None, None)。

    带 5 分钟 TTL 缓存(P7)。Secret 格式:JSON `{"username":"...","password":"..."}`。
    """
    if not _REDIS_AUTH_SECRET_NAME:
        return None, None
    now = time.monotonic()
    with _secret_cache_lock:
        cached = _secret_cache.get(_REDIS_AUTH_SECRET_NAME)
        if cached is not None:
            cached_at, value = cached
            if now - cached_at < _SECRET_TTL_SECONDS:
                return value
        sm = boto3.client("secretsmanager", region_name=_REDIS_AUTH_SECRET_REGION)
        raw = sm.get_secret_value(SecretId=_REDIS_AUTH_SECRET_NAME)["SecretString"]
        payload = json.loads(raw) if raw.lstrip().startswith("{") else {}
        username = payload.get("username")
        password = payload.get("password") or (raw if not payload else None)
        result: tuple[str | None, str | None] = (
            (username, password) if password else (None, None)
        )
        _secret_cache[_REDIS_AUTH_SECRET_NAME] = (now, result)
        return result


def _resolve_endpoint(endpoint: str) -> str:
    """校验 endpoint 非空(必传)。

    本 server 是通用 ElastiCache 巡检工具,不绑定任何实例。endpoint 必须由调用方
    (DevOps Agent)在 tool 调用时传入,容器内不注入任何默认 endpoint。
    big_keys / hot_keys 这类 SCAN 工具建议传 **replica** endpoint 减 master CPU 压力。
    """
    ep = (endpoint or "").strip()
    if not ep:
        raise ValueError(
            "endpoint 必传:请传入要巡检的 ElastiCache 节点 endpoint"
            "(如 my-cache.xxxx.ng.0001.use1.cache.amazonaws.com 或 primary/replica endpoint)。"
            "本工具是通用巡检工具,不绑定特定实例。"
        )
    return ep


def _build_redis(endpoint: str, *, use_tls: bool) -> redis.Redis:
    """按指定 TLS 开关建一个 redis.Redis 客户端(不触发连接,lazy)。

    身份模式自动适配:
      - 无身份(REDIS_AUTH_SECRET_NAME 为空):username=None password=None
      - Legacy AUTH(secret 只有 password):password 模式
      - ACL(secret 含 username + password):ACL 模式
    decode_responses=True 把 bytes 转 str(便于 JSON 序列化)。
    """
    username, password = _fetch_auth()
    return redis.Redis(
        host=endpoint,
        port=_REDIS_PORT,
        username=username,
        password=password,
        ssl=use_tls,
        ssl_cert_reqs=None,  # ElastiCache TLS 自签证书,跳过 client 端 hostname 校验
        socket_connect_timeout=_CONNECT_TIMEOUT_SECONDS,
        socket_timeout=_SOCKET_TIMEOUT_SECONDS,
        decode_responses=True,
    )


def _resolve_tls(endpoint: str) -> bool:
    """决定某 endpoint 是否用 TLS。

    - 强制模式(true/false):直接返回,不探测。
    - auto 模式:首次对该 endpoint 探测——先试 TLS(ElastiCache 最佳实践默认),
      握手/连接失败则回退明文 PING 验证;决策缓存到 _tls_cache,后续 tool call 复用。

    TLS 是实例属性:同一套 server 巡检的不同集群可能配置不同,所以按 endpoint 缓存而非全局。
    """
    if _REDIS_TLS_MODE == "true":
        return True
    if _REDIS_TLS_MODE == "false":
        return False

    # auto:查缓存
    with _tls_cache_lock:
        cached = _tls_cache.get(endpoint)
    if cached is not None:
        return cached

    # 探测:TLS 优先
    decided: bool | None = None
    for candidate in (True, False):
        probe = _build_redis(endpoint, use_tls=candidate)
        try:
            probe.ping()
            decided = candidate
            log.info("[tls-probe] %s -> %s", endpoint, "TLS" if candidate else "plaintext")
            break
        except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError, OSError) as e:
            log.info("[tls-probe] %s ssl=%s 失败:%s", endpoint, candidate, str(e)[:120])
        except redis.exceptions.AuthenticationError:
            # 能握手到要鉴权 = 传输层通了,就是这个 TLS 档位
            decided = candidate
            log.info("[tls-probe] %s -> %s (auth required, transport ok)",
                     endpoint, "TLS" if candidate else "plaintext")
            break
        finally:
            try:
                probe.close()
            except Exception:  # noqa: BLE001 — close 失败不影响探测结论
                pass

    if decided is None:
        # 两档都连不上:不是 TLS 问题(可能网络/SG/endpoint 错)。
        # 缓存不写,默认按 TLS 走,让真正的 tool call 抛出可诊断的连接错误。
        log.warning("[tls-probe] %s TLS 与明文均失败,暂按 TLS;请检查网络/SG/endpoint", endpoint)
        return True

    with _tls_cache_lock:
        _tls_cache[endpoint] = decided
    return decided


def _connect(endpoint: str) -> redis.Redis:
    """建 ElastiCache (Redis/Valkey) 连接 — TLS 按 REDIS_USE_TLS 模式自适应,timeout 各 10s。

    auto 模式下首次对 endpoint 探测 TLS 可用性并缓存(见 _resolve_tls);
    强制模式跳过探测。返回的客户端 lazy 连接,首条命令时才真正建链。
    """
    use_tls = _resolve_tls(endpoint)
    return _build_redis(endpoint, use_tls=use_tls)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _wrap(
    *,
    status: str,
    findings: list[dict[str, Any]],
    raw_data: dict[str, Any],
    recommendation: str,
) -> dict[str, Any]:
    """conventions A8 包装。"""
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
    """简易字节转人类可读(KB / MB / GB)。"""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.2f} {unit}"
        n /= 1024  # type: ignore
    return f"{n:.2f} PB"


# ===========================================================================
# Tool 1 — 总览
# ===========================================================================
@mcp.tool()
def inspect_overview(endpoint: str) -> dict[str, Any]:
    """巡检总览:引擎版本 / uptime / 角色(master/replica)/ 客户端数 — 排障第一步,确认连的是哪个节点、什么角色。endpoint 必传。"""
    ep = _resolve_endpoint(endpoint)
    log.info("[overview] %s", ep)

    r = _connect(ep)
    try:
        info_server = r.info("server")
        info_clients = r.info("clients")
        role_info = r.info("replication")
    finally:
        r.close()

    role = role_info.get("role", "unknown")
    uptime = _to_int(info_server.get("uptime_in_seconds"))
    connected_clients = _to_int(info_clients.get("connected_clients"))
    blocked_clients = _to_int(info_clients.get("blocked_clients"))

    raw_data = {
        "endpoint": ep,
        "queried_at": _now_iso(),
        "tls": "enabled" if _resolve_tls(ep) else "disabled",
        "version": str(info_server.get("redis_version") or info_server.get("valkey_version") or ""),
        "engine": str(info_server.get("server_name") or info_server.get("redis_mode") or "redis"),
        "uptime_seconds": uptime,
        "uptime_pretty": f"{uptime // 86400}d {(uptime % 86400) // 3600}h",
        "role": str(role),
        "connected_clients": connected_clients,
        "blocked_clients": blocked_clients,
        "maxclients": _to_int(info_clients.get("maxclients")),
        "tcp_port": _to_int(info_server.get("tcp_port"), _REDIS_PORT),
        "process_id": _to_int(info_server.get("process_id")),
    }

    findings: list[dict[str, Any]] = []
    if blocked_clients > 0:
        findings.append({
            "severity": "warning", "metric": "blocked_clients",
            "value": str(blocked_clients),
            "note": "有 client 在 BLPOP / BRPOP / WAIT 等待",
        })

    return _wrap(
        status="ok", findings=findings, raw_data=raw_data,
        recommendation=(
            f"{raw_data['engine']} {raw_data['version']}({role}),uptime {raw_data['uptime_pretty']},"
            f"{connected_clients} 连接 / {raw_data['maxclients']} 上限。"
        ),
    )


# ===========================================================================
# Tool 2 — 内存
# ===========================================================================
@mcp.tool()
def inspect_memory(endpoint: str) -> dict[str, Any]:
    """巡检内存:使用量 / maxmemory 占比 / 碎片化率 / 累计 eviction — 排查内存压力、OOM 风险、碎片浪费。endpoint 必传。"""
    ep = _resolve_endpoint(endpoint)
    log.info("[memory] %s", ep)

    r = _connect(ep)
    try:
        info_mem = r.info("memory")
        info_stats = r.info("stats")
    finally:
        r.close()

    used = _to_int(info_mem.get("used_memory"))
    used_rss = _to_int(info_mem.get("used_memory_rss"))
    used_peak = _to_int(info_mem.get("used_memory_peak"))
    maxmemory = _to_int(info_mem.get("maxmemory"))
    frag_ratio = _to_float(info_mem.get("mem_fragmentation_ratio"))
    evicted_keys = _to_int(info_stats.get("evicted_keys"))
    expired_keys = _to_int(info_stats.get("expired_keys"))
    used_pct = (used * 100.0 / maxmemory) if maxmemory > 0 else 0.0

    raw_data = {
        "endpoint": ep, "queried_at": _now_iso(),
        "used_memory_bytes": used, "used_memory_pretty": _bytes_to_human(used),
        "used_memory_rss_bytes": used_rss, "used_memory_rss_pretty": _bytes_to_human(used_rss),
        "used_memory_peak_bytes": used_peak, "used_memory_peak_pretty": _bytes_to_human(used_peak),
        "maxmemory_bytes": maxmemory,
        "maxmemory_pretty": _bytes_to_human(maxmemory) if maxmemory else "(no limit)",
        "used_pct": round(used_pct, 2),
        "mem_fragmentation_ratio": frag_ratio,
        "maxmemory_policy": str(info_mem.get("maxmemory_policy") or ""),
        "evicted_keys_total": evicted_keys,
        "expired_keys_total": expired_keys,
    }

    findings: list[dict[str, Any]] = []
    status = "ok"
    rec_parts = []

    if used_pct >= 90:
        status = "critical"
        findings.append({"severity": "critical", "metric": "memory_used_pct",
                         "value": f"{used_pct:.1f}%", "threshold": "90%"})
        rec_parts.append(f"内存使用 {used_pct:.1f}% 极高,即将 OOM evict。")
    elif used_pct >= 75:
        status = "warning"
        findings.append({"severity": "warning", "metric": "memory_used_pct",
                         "value": f"{used_pct:.1f}%", "threshold": "75%"})
        rec_parts.append(f"内存使用 {used_pct:.1f}% 偏高。")

    if frag_ratio >= 1.5 and used > 100 * 1024 * 1024:
        if status != "critical":
            status = "warning"
        findings.append({"severity": "warning" if frag_ratio < 2.0 else "critical",
                         "metric": "fragmentation_ratio",
                         "value": f"{frag_ratio:.2f}", "threshold": "1.5"})
        rec_parts.append(f"内存碎片化 {frag_ratio:.2f}({_bytes_to_human(used_rss - used)} 浪费),"
                         "可设 activedefrag yes 自动整理,或重启 reset。")

    if evicted_keys > 0:
        if status != "critical":
            status = "warning"
        findings.append({"severity": "warning", "metric": "evicted_keys_total",
                         "value": str(evicted_keys),
                         "note": f"policy={raw_data['maxmemory_policy']}"})
        rec_parts.append(f"曾 evict {evicted_keys} 个 key(总累计),关注是否频繁。")

    if not rec_parts:
        rec_parts.append(
            f"内存健康,used {raw_data['used_memory_pretty']} / "
            f"max {raw_data['maxmemory_pretty']} ({used_pct:.1f}%),frag={frag_ratio:.2f}。"
        )

    return _wrap(status=status, findings=findings, raw_data=raw_data,
                 recommendation=" ".join(rec_parts))


# ===========================================================================
# Tool 3 — 慢命令 top N
# ===========================================================================
@mcp.tool()
def inspect_slow_queries(endpoint: str, top_n: int = _TOP_N_DEFAULT) -> dict[str, Any]:
    """巡检慢命令(SLOWLOG GET)— 找耗时长的命令(KEYS/SORT/大集合 HGETALL 等)。默认阈值 10ms。endpoint 必传。"""
    ep = _resolve_endpoint(endpoint)
    n = max(1, min(top_n, _QUERY_LIMIT))
    log.info("[slow_queries] %s top=%d", ep, n)

    r = _connect(ep)
    try:
        slow_entries = r.slowlog_get(n)
        try:
            cfg = r.config_get("slowlog-log-slower-than") or {}
            threshold_us = _to_int(cfg.get("slowlog-log-slower-than"), 10000)
        except redis.exceptions.ResponseError:
            threshold_us = 10000  # ElastiCache 禁 CONFIG,降级默认 10ms
    finally:
        r.close()

    queries = []
    for e in slow_entries:
        cmd = e.get("command")
        cmd_str = " ".join(str(c) for c in cmd) if isinstance(cmd, (list, tuple)) else str(cmd or "")
        queries.append({
            "id": _to_int(e.get("id")),
            "start_time_unix": _to_int(e.get("start_time")),
            "duration_us": _to_int(e.get("duration")),
            "duration_ms": round(_to_int(e.get("duration")) / 1000.0, 3),
            "command_snippet": cmd_str[:200],
            "client_addr": str(e.get("client_address") or e.get("client_addr") or ""),
            "client_name": str(e.get("client_name") or ""),
        })

    raw_data = {
        "endpoint": ep, "queried_at": _now_iso(),
        "threshold_microseconds": threshold_us,
        "threshold_ms": round(threshold_us / 1000.0, 3),
        "slow_queries": queries,
    }

    if not queries:
        return _wrap(status="ok", findings=[], raw_data=raw_data,
                     recommendation=f"slowlog 为空(阈值 {threshold_us}μs),无慢命令。")

    max_dur = max(q["duration_ms"] for q in queries)
    if max_dur >= 100:
        status = "critical"
        rec = (f"slowlog top {len(queries)} 中最慢 {max_dur:.1f}ms ≥ 100ms。"
               "建议检查命令是否扫描大 key / 大集合,用 SCAN 替代 KEYS、HSCAN 替代 HGETALL。")
    elif max_dur >= 50:
        status = "warning"
        rec = f"slowlog top {len(queries)} 中最慢 {max_dur:.1f}ms ≥ 50ms,关注趋势。"
    else:
        status = "ok"
        rec = f"slowlog 有 {len(queries)} 条,最慢 {max_dur:.1f}ms,可控。"

    findings = [
        {"severity": "critical" if q["duration_ms"] >= 100
                    else "warning" if q["duration_ms"] >= 50 else "info",
         "metric": "slow_command", "duration_ms": q["duration_ms"],
         "command": q["command_snippet"][:80], "client": q["client_addr"]}
        for q in queries
    ]
    return _wrap(status=status, findings=findings, raw_data=raw_data, recommendation=rec)


# ===========================================================================
# Tool 4 — 客户端连接分布
# ===========================================================================
@mcp.tool()
def inspect_clients(endpoint: str) -> dict[str, Any]:
    """巡检客户端连接(CLIENT LIST):按 IP+name 聚合 + idle 时长分布 — 排查连接数异常、连接池泄漏(长 idle)。endpoint 必传。"""
    ep = _resolve_endpoint(endpoint)
    log.info("[clients] %s", ep)

    r = _connect(ep)
    try:
        clients = r.client_list()
    finally:
        r.close()

    by_addr: dict[str, dict[str, Any]] = {}
    long_idle: list[dict[str, Any]] = []
    for c in clients:
        addr_full = str(c.get("addr") or "")
        ip = addr_full.split(":")[0] if ":" in addr_full else addr_full
        name = str(c.get("name") or "")
        key = f"{ip}|{name}"
        idle = _to_int(c.get("idle"))
        if key not in by_addr:
            by_addr[key] = {"ip": ip, "name": name, "count": 0, "max_idle": 0, "max_age": 0, "cmds": set()}
        by_addr[key]["count"] += 1
        by_addr[key]["max_idle"] = max(by_addr[key]["max_idle"], idle)
        by_addr[key]["max_age"] = max(by_addr[key]["max_age"], _to_int(c.get("age")))
        cmd = c.get("cmd")
        if cmd:
            by_addr[key]["cmds"].add(str(cmd))
        if idle >= 3600:
            long_idle.append({"addr": addr_full, "name": name,
                              "idle_seconds": idle, "age_seconds": _to_int(c.get("age"))})

    groups = sorted(
        [{**v, "cmds": sorted(v["cmds"])[:10]} for v in by_addr.values()],
        key=lambda x: x["count"], reverse=True,
    )
    total = len(clients)
    raw_data = {
        "endpoint": ep, "queried_at": _now_iso(),
        "total_connections": total, "distinct_clients": len(groups),
        "groups": groups[:_TOP_N_DEFAULT], "long_idle_clients": long_idle[:_TOP_N_DEFAULT],
    }

    findings: list[dict[str, Any]] = []
    if long_idle:
        findings.append({"severity": "warning", "metric": "long_idle_clients",
                         "count": len(long_idle),
                         "max_idle_seconds": max(c["idle_seconds"] for c in long_idle)})

    if total >= 4000:
        status, rec = "critical", f"{total} 连接接近 maxclients,关注连接 leak / 池配置。"
    elif total >= 2000:
        status, rec = "warning", f"{total} 连接较多,审阅是否有非预期来源。"
    else:
        status = "ok"
        rec = f"{total} 连接 / {len(groups)} 独立 (ip, name),正常。"
        if long_idle:
            rec += f" 但有 {len(long_idle)} 个 idle ≥ 1h,可能是连接池忘释放。"

    return _wrap(status=status, findings=findings, raw_data=raw_data, recommendation=rec)


# ===========================================================================
# Tool 5 — Keyspace
# ===========================================================================
@mcp.tool()
def inspect_keyspace(endpoint: str) -> dict[str, Any]:
    """巡检 keyspace:各 db 的 key 数 / 设了 TTL 的占比 / 平均 TTL — 排查永不过期 key 堆积导致的内存膨胀。endpoint 必传。"""
    ep = _resolve_endpoint(endpoint)
    log.info("[keyspace] %s", ep)

    r = _connect(ep)
    try:
        info_ks = r.info("keyspace")
    finally:
        r.close()

    dbs = []
    total_keys = 0
    total_expires = 0
    for dbname, stats in (info_ks or {}).items():
        if not isinstance(stats, dict):
            continue
        keys = _to_int(stats.get("keys"))
        expires = _to_int(stats.get("expires"))
        avg_ttl = _to_int(stats.get("avg_ttl"))
        dbs.append({
            "db": dbname, "keys": keys, "expires": expires,
            "expires_pct": round(expires * 100.0 / max(keys, 1), 2),
            "avg_ttl_ms": avg_ttl,
            "avg_ttl_seconds": round(avg_ttl / 1000.0, 1) if avg_ttl else None,
        })
        total_keys += keys
        total_expires += expires
    dbs.sort(key=lambda x: x["keys"], reverse=True)

    raw_data = {
        "endpoint": ep, "queried_at": _now_iso(),
        "total_keys": total_keys, "total_expires": total_expires,
        "expires_pct_overall": round(total_expires * 100.0 / max(total_keys, 1), 2),
        "databases": dbs,
    }
    findings = [{"severity": "info", "metric": "db_keys", "db": d["db"],
                 "keys": d["keys"], "expires_pct": d["expires_pct"]} for d in dbs[:10]]

    if total_keys == 0:
        rec = "keyspace 为空,无 key。"
    else:
        rec = (f"共 {total_keys} key 分布在 {len(dbs)} 个 db,"
               f"{raw_data['expires_pct_overall']}% 设了 TTL。")
        if raw_data["expires_pct_overall"] < 50 and total_keys > 1000:
            rec += " ⚠ 设 TTL 的 key 占比 < 50%,关注永不过期 key 堆积导致内存膨胀。"

    return _wrap(status="ok", findings=findings, raw_data=raw_data, recommendation=rec)


# ===========================================================================
# Tool 6 — 命中率 + OPS + 命令统计
# ===========================================================================
@mcp.tool()
def inspect_stats(endpoint: str, top_n_commands: int = 10) -> dict[str, Any]:
    """巡检运行统计:缓存命中率 / 实时 OPS / 拒绝连接数 / 命令调用 top — 命中率低或拒绝连接是缓存效果差/容量不足的信号。endpoint 必传。"""
    ep = _resolve_endpoint(endpoint)
    n = max(1, min(top_n_commands, 50))
    log.info("[stats] %s top=%d", ep, n)

    r = _connect(ep)
    try:
        info_stats = r.info("stats")
        info_cmd = r.info("commandstats")
    finally:
        r.close()

    hits = _to_int(info_stats.get("keyspace_hits"))
    misses = _to_int(info_stats.get("keyspace_misses"))
    hit_rate = round(hits * 100.0 / max(hits + misses, 1), 2) if (hits + misses) > 0 else None
    rejected = _to_int(info_stats.get("rejected_connections"))
    total_conn = _to_int(info_stats.get("total_connections_received"))
    total_cmd = _to_int(info_stats.get("total_commands_processed"))
    ops = _to_int(info_stats.get("instantaneous_ops_per_sec"))

    commands = []
    for key, val in info_cmd.items():
        if not isinstance(val, dict):
            continue
        commands.append({
            "command": key.replace("cmdstat_", ""),
            "calls": _to_int(val.get("calls")),
            "usec_total": _to_int(val.get("usec")),
            "usec_per_call": _to_float(val.get("usec_per_call")),
        })
    commands.sort(key=lambda x: x["calls"], reverse=True)

    raw_data = {
        "endpoint": ep, "queried_at": _now_iso(),
        "keyspace_hits": hits, "keyspace_misses": misses, "hit_rate_pct": hit_rate,
        "instantaneous_ops_per_sec": ops,
        "total_connections_received": total_conn,
        "total_commands_processed": total_cmd,
        "rejected_connections": rejected,
        "top_commands": commands[:n],
    }

    findings: list[dict[str, Any]] = []
    status = "ok"
    rec_parts = []
    if hit_rate is not None:
        if hit_rate < 80 and (hits + misses) > 1000:
            status = "warning"
            findings.append({"severity": "warning", "metric": "hit_rate",
                             "value": f"{hit_rate}%", "threshold": "80%"})
            rec_parts.append(f"命中率 {hit_rate}% 偏低(总查询 > 1k),考虑预热 / 缓存策略 / TTL 调整。")
        elif hit_rate >= 95:
            rec_parts.append(f"命中率 {hit_rate}% 优秀。")
        else:
            rec_parts.append(f"命中率 {hit_rate}%。")
    if rejected > 0:
        status = "warning" if status != "critical" else status
        findings.append({"severity": "warning", "metric": "rejected_connections",
                         "value": str(rejected), "note": "连接被拒,接近 maxclients 或网络层 reject"})
        rec_parts.append(f"⚠ 累计拒绝连接 {rejected} 次,审阅 maxclients / TLS handshake。")
    if ops > 50000:
        rec_parts.append(f"当前 OPS {ops}/s 极高,关注 CPU 是否瓶颈。")

    return _wrap(status=status, findings=findings, raw_data=raw_data,
                 recommendation=" ".join(rec_parts) if rec_parts else f"OPS {ops}/s,运行平稳。")


# ===========================================================================
# Tool 7 — 复制状态
# ===========================================================================
@mcp.tool()
def inspect_replication(endpoint: str) -> dict[str, Any]:
    """巡检主从复制(INFO replication):master 看 replica 数 + lag;replica 看与 master 链路状态 + lag — 排查复制延迟、失去高可用、链路断开。endpoint 必传。"""
    ep = _resolve_endpoint(endpoint)
    log.info("[replication] %s", ep)

    r = _connect(ep)
    try:
        info_rep = r.info("replication")
    finally:
        r.close()

    role = str(info_rep.get("role", "unknown"))
    raw_data: dict[str, Any] = {
        "endpoint": ep, "queried_at": _now_iso(), "role": role,
        "connected_slaves": _to_int(info_rep.get("connected_slaves")),
        "master_replid": str(info_rep.get("master_replid") or ""),
        "master_repl_offset": _to_int(info_rep.get("master_repl_offset")),
    }
    findings: list[dict[str, Any]] = []
    status = "ok"

    if role == "master":
        slaves = []
        for k, v in info_rep.items():
            if not str(k).startswith("slave"):
                continue
            if not isinstance(v, dict):
                if isinstance(v, str):
                    fields = dict(p.split("=", 1) for p in v.split(",") if "=" in p)
                else:
                    continue
            else:
                fields = v
            slaves.append({
                "id": k, "ip": str(fields.get("ip") or ""), "port": _to_int(fields.get("port")),
                "state": str(fields.get("state") or ""), "offset": _to_int(fields.get("offset")),
                "lag_seconds": _to_int(fields.get("lag")),
            })
        raw_data["slaves"] = slaves
        if not slaves:
            status = "warning"
            findings.append({"severity": "warning", "metric": "no_replicas",
                             "note": "master 上无 slave,失去高可用"})
            rec = "⚠ master 当前无 connected slave,失去自动 failover 能力。"
        else:
            max_lag = max(s["lag_seconds"] for s in slaves)
            if max_lag >= 30:
                status, rec = "critical", f"replica 最大 lag {max_lag}s ≥ 30s,严重落后。"
            elif max_lag >= 5:
                status, rec = "warning", f"replica 最大 lag {max_lag}s ≥ 5s,关注。"
            else:
                rec = f"复制健康,{len(slaves)} 个 replica,最大 lag {max_lag}s。"
            for s in slaves:
                if s["lag_seconds"] >= 5:
                    findings.append({"severity": "critical" if s["lag_seconds"] >= 30 else "warning",
                                     "metric": "replica_lag", "ip": s["ip"], "lag_s": s["lag_seconds"]})
    else:
        master_link = str(info_rep.get("master_link_status") or "")
        master_lag = _to_int(info_rep.get("master_last_io_seconds_ago"))
        raw_data["master_host"] = str(info_rep.get("master_host") or "")
        raw_data["master_port"] = _to_int(info_rep.get("master_port"))
        raw_data["master_link_status"] = master_link
        raw_data["master_last_io_seconds_ago"] = master_lag
        raw_data["slave_repl_offset"] = _to_int(info_rep.get("slave_repl_offset"))
        raw_data["slave_priority"] = _to_int(info_rep.get("slave_priority"), 100)
        if master_link != "up":
            status = "critical"
            findings.append({"severity": "critical", "metric": "master_link_down", "value": master_link})
            rec = f"⚠ slave 与 master 链路状态 = {master_link!r},非 up。"
        elif master_lag >= 30:
            status, rec = "warning", f"slave 距 master 最近 IO {master_lag}s 前,可能 lag。"
        else:
            rec = f"slave 健康,master_link=up,last_io={master_lag}s ago。"

    return _wrap(status=status, findings=findings, raw_data=raw_data, recommendation=rec)


# ===========================================================================
# Tool 8 — 持久化
# ===========================================================================
@mcp.tool()
def inspect_persistence(endpoint: str) -> dict[str, Any]:
    """巡检持久化(RDB / AOF 状态):上次保存时间 / bgsave 是否失败 / AOF 写状态 — 排查备份失败、快照过期(Redis/Valkey 引擎;Memcached 无持久化)。endpoint 必传。"""
    ep = _resolve_endpoint(endpoint)
    log.info("[persistence] %s", ep)

    r = _connect(ep)
    try:
        info_p = r.info("persistence")
    finally:
        r.close()

    rdb_last_save_unix = _to_int(info_p.get("rdb_last_save_time"))
    rdb_last_bgsave_status = str(info_p.get("rdb_last_bgsave_status") or "")
    aof_enabled = _to_int(info_p.get("aof_enabled")) == 1
    aof_last_write_status = str(info_p.get("aof_last_write_status") or "")
    aof_last_bgrewrite_status = str(info_p.get("aof_last_bgrewrite_status") or "")
    now_unix = int(datetime.now(timezone.utc).timestamp())
    rdb_age_seconds = now_unix - rdb_last_save_unix if rdb_last_save_unix > 0 else None

    raw_data = {
        "endpoint": ep, "queried_at": _now_iso(),
        "rdb_changes_since_last_save": _to_int(info_p.get("rdb_changes_since_last_save")),
        "rdb_last_save_unix": rdb_last_save_unix,
        "rdb_last_save_age_seconds": rdb_age_seconds,
        "rdb_last_bgsave_status": rdb_last_bgsave_status,
        "aof_enabled": aof_enabled,
        "aof_last_write_status": aof_last_write_status if aof_enabled else None,
        "aof_last_bgrewrite_status": aof_last_bgrewrite_status if aof_enabled else None,
    }
    findings: list[dict[str, Any]] = []
    status = "ok"
    rec_parts = []

    if rdb_last_bgsave_status not in ("ok", ""):
        status = "critical"
        findings.append({"severity": "critical", "metric": "rdb_bgsave_failed", "value": rdb_last_bgsave_status})
        rec_parts.append(f"⚠ 上次 RDB bgsave 状态 = {rdb_last_bgsave_status!r}。")
    elif rdb_age_seconds is not None and rdb_age_seconds > 24 * 3600:
        status = "warning"
        findings.append({"severity": "warning", "metric": "rdb_stale", "age_seconds": rdb_age_seconds})
        rec_parts.append(f"上次 RDB 已 {rdb_age_seconds // 3600}h 前,关注 SnapshotWindow 是否生效。")

    if aof_enabled:
        if aof_last_write_status != "ok":
            status = "critical"
            findings.append({"severity": "critical", "metric": "aof_write_failed", "value": aof_last_write_status})
            rec_parts.append(f"⚠ AOF 上次写状态 = {aof_last_write_status!r}。")
        if aof_last_bgrewrite_status not in ("ok", ""):
            status = "warning" if status != "critical" else status
            findings.append({"severity": "warning", "metric": "aof_bgrewrite_failed", "value": aof_last_bgrewrite_status})
            rec_parts.append(f"AOF bgrewrite 状态 = {aof_last_bgrewrite_status!r}。")

    if not rec_parts:
        rec_parts.append(
            f"持久化健康,RDB 上次保存 {rdb_age_seconds // 60}min 前。"
            if rdb_age_seconds is not None else "持久化健康。"
        )

    return _wrap(status=status, findings=findings, raw_data=raw_data,
                 recommendation=" ".join(rec_parts))


# ===========================================================================
# Tool 9 — Big keys
# ===========================================================================
@mcp.tool()
def inspect_big_keys(
    endpoint: str,
    top_n: int = _TOP_N_DEFAULT,
    max_scan_count: int = _SCAN_DEFAULT_MAX,
    db: int = 0,
    pattern: str = "*",
) -> dict[str, Any]:
    """找大 key:SCAN 全库 + MEMORY USAGE,返回 top N 内存占用最大的 key — 排查内存被少数大 key 占满、大 key 拖慢命令。⚠ 建议传 replica endpoint 减 master CPU。只返回 key 名/大小/类型/TTL,不返回 value。endpoint 必传。

    Args:
        top_n: 返回 top N(默认 20,上限 100)
        max_scan_count: 最多扫多少 key 后停(默认 5000,上限 50000),防大库扫挂
        db: logical database(默认 0)
        pattern: SCAN match 通配符(默认 "*",可传 "user:*")
    """
    ep = _resolve_endpoint(endpoint)
    n = max(1, min(top_n, _QUERY_LIMIT))
    scan_max = max(100, min(max_scan_count, _SCAN_HARD_LIMIT))
    if db < 0 or db > 15:
        raise ValueError(f"db 必须 ∈ [0, 15],收到 {db}")
    log.info("[big_keys] %s db=%d pattern=%r top=%d max_scan=%d", ep, db, pattern, n, scan_max)

    r = _connect(ep)
    try:
        if db != 0:
            r.execute_command("SELECT", db)
        keys_collected: list[str] = []
        cursor = 0
        scan_iterations = 0
        while True:
            cursor, batch = r.scan(cursor=cursor, match=pattern, count=200)
            keys_collected.extend(batch)
            scan_iterations += 1
            if len(keys_collected) >= scan_max:
                keys_collected = keys_collected[:scan_max]
                break
            if cursor == 0 or scan_iterations > 1000:
                break

        sized: list[dict[str, Any]] = []
        for i in range(0, len(keys_collected), _PIPELINE_BATCH):
            chunk = keys_collected[i:i + _PIPELINE_BATCH]
            pipe = r.pipeline(transaction=False)
            for k in chunk:
                pipe.memory_usage(k)
            sizes = pipe.execute()
            pipe2 = r.pipeline(transaction=False)
            for k in chunk:
                pipe2.type(k)
            types = pipe2.execute()
            pipe3 = r.pipeline(transaction=False)
            for k in chunk:
                pipe3.ttl(k)
            ttls = pipe3.execute()
            for k, sz, t, ttl in zip(chunk, sizes, types, ttls):
                if sz is None:
                    continue
                sized.append({
                    "key": k, "size_bytes": int(sz), "size_pretty": _bytes_to_human(int(sz)),
                    "type": str(t) if t else "unknown",
                    "ttl_seconds": int(ttl) if ttl is not None and ttl >= 0 else None,
                    "ttl_state": "no_expire" if ttl == -1 else "expired" if ttl == -2 else "ok",
                })
        sized.sort(key=lambda x: x["size_bytes"], reverse=True)
        top_keys = sized[:n]
    finally:
        r.close()

    truncated = len(keys_collected) >= scan_max and cursor != 0
    raw_data = {
        "endpoint": ep, "db": db, "pattern": pattern, "queried_at": _now_iso(),
        "scanned_count": len(keys_collected), "scan_iterations": scan_iterations,
        "max_scan_count": scan_max, "scan_truncated": truncated,
        "measured_count": len(sized), "top_keys": top_keys,
    }
    if not top_keys:
        return _wrap(status="ok", findings=[], raw_data=raw_data,
                     recommendation=f"db {db} pattern {pattern!r} 范围内未找到 key。")

    max_size = top_keys[0]["size_bytes"]
    if max_size >= 100 * 1024 * 1024:
        status = "critical"
        rec = (f"发现 big key 最大 {top_keys[0]['size_pretty']} ≥ 100MB,key={top_keys[0]['key'][:80]} "
               f"type={top_keys[0]['type']}。建议拆分(hash 拆桶/list 分页)+ 设 TTL。")
    elif max_size >= 10 * 1024 * 1024:
        status = "warning"
        rec = f"top1 大 key {top_keys[0]['size_pretty']} ≥ 10MB,key={top_keys[0]['key'][:80]},关注是否需拆分。"
    else:
        status = "ok"
        rec = f"扫描 {len(keys_collected)} key,top1 {top_keys[0]['size_pretty']},无明显 big key。"
    if truncated:
        rec += f" ⚠ 扫描达上限 {scan_max} 即停(库可能更大,建议分多次扫或调高 max_scan_count)。"

    findings = [
        {"severity": ("critical" if k["size_bytes"] >= 100 * 1024 * 1024
                     else "warning" if k["size_bytes"] >= 10 * 1024 * 1024 else "info"),
         "metric": "big_key", "key": k["key"][:120], "size": k["size_pretty"],
         "type": k["type"], "ttl": k["ttl_state"] if k["ttl_state"] != "ok" else f"{k['ttl_seconds']}s"}
        for k in top_keys
    ]
    return _wrap(status=status, findings=findings, raw_data=raw_data, recommendation=rec)


# ===========================================================================
# Tool 10 — Hot keys
# ===========================================================================
@mcp.tool()
def inspect_hot_keys(
    endpoint: str,
    top_n: int = _TOP_N_DEFAULT,
    max_scan_count: int = _SCAN_DEFAULT_MAX,
    db: int = 0,
    pattern: str = "*",
) -> dict[str, Any]:
    """找热点 key:SCAN 全库 + OBJECT FREQ(LFU 模式)/ OBJECT IDLETIME(其它模式)— 找访问最频繁的 key,定位热点导致的单 shard 压力。⚠ 建议传 replica endpoint 减 master CPU。只返回 key 名/类型/频次,不返回 value。endpoint 必传。

    Args 同 big_keys。LFU 模式(maxmemory-policy=*-lfu)返回真实访问频次,否则用 idletime 兜底。
    """
    ep = _resolve_endpoint(endpoint)
    n = max(1, min(top_n, _QUERY_LIMIT))
    scan_max = max(100, min(max_scan_count, _SCAN_HARD_LIMIT))
    if db < 0 or db > 15:
        raise ValueError(f"db 必须 ∈ [0, 15],收到 {db}")
    log.info("[hot_keys] %s db=%d pattern=%r top=%d max_scan=%d", ep, db, pattern, n, scan_max)

    r = _connect(ep)
    try:
        if db != 0:
            r.execute_command("SELECT", db)
        info_mem = r.info("memory")
        policy = str(info_mem.get("maxmemory_policy") or "noeviction")
        is_lfu_mode = policy.endswith("-lfu")

        keys_collected: list[str] = []
        cursor = 0
        scan_iterations = 0
        while True:
            cursor, batch = r.scan(cursor=cursor, match=pattern, count=200)
            keys_collected.extend(batch)
            scan_iterations += 1
            if len(keys_collected) >= scan_max:
                keys_collected = keys_collected[:scan_max]
                break
            if cursor == 0 or scan_iterations > 1000:
                break

        sized: list[dict[str, Any]] = []
        for i in range(0, len(keys_collected), _PIPELINE_BATCH):
            chunk = keys_collected[i:i + _PIPELINE_BATCH]
            pipe = r.pipeline(transaction=False)
            for k in chunk:
                if is_lfu_mode:
                    pipe.object("FREQ", k)
                else:
                    pipe.object("IDLETIME", k)
            results = pipe.execute()
            pipe2 = r.pipeline(transaction=False)
            for k in chunk:
                pipe2.type(k)
            types = pipe2.execute()
            for k, val, t in zip(chunk, results, types):
                if val is None or isinstance(val, Exception):
                    continue
                entry = {"key": k, "type": str(t) if t else "unknown"}
                if is_lfu_mode:
                    entry["freq"] = int(val)
                else:
                    entry["idle_seconds"] = int(val)
                sized.append(entry)
    finally:
        r.close()

    if is_lfu_mode:
        sized.sort(key=lambda x: x.get("freq", 0), reverse=True)
    else:
        sized.sort(key=lambda x: x.get("idle_seconds", 999_999_999))
    top_keys = sized[:n]
    truncated = len(keys_collected) >= scan_max and cursor != 0

    raw_data = {
        "endpoint": ep, "db": db, "pattern": pattern, "queried_at": _now_iso(),
        "maxmemory_policy": policy,
        "mode": "LFU (OBJECT FREQ)" if is_lfu_mode else "LRU (OBJECT IDLETIME)",
        "scanned_count": len(keys_collected), "scan_truncated": truncated,
        "top_keys": top_keys,
    }
    if not top_keys:
        return _wrap(status="ok", findings=[], raw_data=raw_data,
                     recommendation=f"db {db} pattern {pattern!r} 范围内未找到 key。")

    if is_lfu_mode:
        max_freq = top_keys[0].get("freq", 0)
        if max_freq >= 200:
            status = "warning"
            rec = (f"top1 hot key freq={max_freq}(LFU counter,接近 255 极热),"
                   f"key={top_keys[0]['key'][:80]}。建议加本地缓存层 / 看是否需要拆 key。")
        else:
            status = "ok"
            rec = f"top1 hot key freq={max_freq},热度可控。"
    else:
        min_idle = top_keys[0].get("idle_seconds", 0)
        rec = (f"非 LFU 模式(policy={policy!r}),用 OBJECT IDLETIME 兜底。最近访问 key idle={min_idle}s。"
               "建议改 maxmemory-policy 为 allkeys-lfu / volatile-lfu 才能拿真实访问频次。")
        status = "ok"
    if truncated:
        rec += f" ⚠ 扫描达上限 {scan_max} 即停。"

    findings = [
        {"severity": "warning" if (is_lfu_mode and k.get("freq", 0) >= 200) else "info",
         "metric": "hot_key" if is_lfu_mode else "recently_accessed_key",
         "key": k["key"][:120], "type": k["type"],
         **({"freq": k["freq"]} if is_lfu_mode else {"idle_s": k["idle_seconds"]})}
        for k in top_keys
    ]
    return _wrap(status=status, findings=findings, raw_data=raw_data, recommendation=rec)


# ===========================================================================
# Tool 11 — Eviction
# ===========================================================================
@mcp.tool()
def inspect_eviction(endpoint: str) -> dict[str, Any]:
    """巡检 eviction(内存满淘汰):maxmemory 占比 + 累计 evicted_keys + maxmemory-policy 健康 — 排查 eviction 风暴、noeviction 模式写失败(OOM)。endpoint 必传。"""
    ep = _resolve_endpoint(endpoint)
    log.info("[eviction] %s", ep)

    r = _connect(ep)
    try:
        info_mem = r.info("memory")
        info_stats = r.info("stats")
    finally:
        r.close()

    used_memory = _to_int(info_mem.get("used_memory"))
    used_memory_dataset = _to_int(info_mem.get("used_memory_dataset"))
    maxmemory = _to_int(info_mem.get("maxmemory"))
    policy = str(info_mem.get("maxmemory_policy") or "noeviction")
    used_pct = (used_memory / maxmemory * 100) if maxmemory > 0 else None
    evicted_keys = _to_int(info_stats.get("evicted_keys"))
    expired_keys = _to_int(info_stats.get("expired_keys"))
    keyspace_misses = _to_int(info_stats.get("keyspace_misses"))

    raw_data = {
        "endpoint": ep, "queried_at": _now_iso(),
        "maxmemory_bytes": maxmemory, "maxmemory_pretty": _bytes_to_human(maxmemory),
        "used_memory_bytes": used_memory, "used_memory_pretty": _bytes_to_human(used_memory),
        "used_memory_dataset_bytes": used_memory_dataset,
        "used_pct": round(used_pct, 2) if used_pct is not None else None,
        "maxmemory_policy": policy,
        "evicted_keys_total": evicted_keys, "expired_keys_total": expired_keys,
        "keyspace_misses_total": keyspace_misses,
    }
    findings: list[dict[str, Any]] = []
    rec_parts: list[str] = []
    status = "ok"

    if maxmemory == 0:
        rec_parts.append("maxmemory=0(无上限),OOM 时直接被 OS kill,建议显式设 maxmemory + policy。")
        status = "warning"
    elif used_pct is not None:
        if used_pct >= 90:
            status = "critical"
            findings.append({"severity": "critical", "metric": "memory_used_pct",
                             "value": f"{used_pct:.1f}%", "threshold": "90%"})
            rec_parts.append(f"⚠ 内存用量 {used_pct:.1f}% ≥ 90%,policy={policy};"
                             "noeviction → 写命令开始失败,allkeys-* → eviction 飙升。建议扩容/迁数据。")
        elif used_pct >= 75:
            status = "warning"
            findings.append({"severity": "warning", "metric": "memory_used_pct",
                             "value": f"{used_pct:.1f}%", "threshold": "75%"})
            rec_parts.append(f"内存用量 {used_pct:.1f}% ≥ 75%,关注趋势。")

    if policy == "noeviction" and used_pct and used_pct >= 80:
        findings.append({"severity": "warning", "metric": "noeviction_high_mem",
                         "note": "noeviction + 高用量,内存满时写命令返回 OOM"})
        rec_parts.append("noeviction 模式内存满时写报 OOM,考虑 allkeys-lfu / volatile-lfu。")

    if evicted_keys > 0:
        findings.append({"severity": "warning", "metric": "evicted_keys",
                         "value": str(evicted_keys), "note": "已发生过 eviction(累计)"})
        rec_parts.append(f"累计已 evict {evicted_keys} 个 key — 监控 trend 判断速率是否加速。")

    if not rec_parts:
        rec_parts.append(
            f"内存健康:{used_pct:.1f}% used,policy={policy},无 eviction。"
            if used_pct is not None else
            f"policy={policy},无 maxmemory 上限,内存用量 {_bytes_to_human(used_memory)}。"
        )

    return _wrap(status=status, findings=findings, raw_data=raw_data,
                 recommendation=" ".join(rec_parts))


# ===========================================================================
# Tool 12 — Cluster mode(分片 / hash slot / 各 shard 状态)
# ===========================================================================
@mcp.tool()
def inspect_cluster_mode(endpoint: str) -> dict[str, Any]:
    """巡检 cluster mode(分片模式):是否启用、分片数、hash slot 覆盖是否完整、各 shard 主从状态 — 排查 resharding 后 slot 缺失、cross-slot 错误、shard 不均。cluster mode disabled 会返回提示。endpoint 必传。

    cluster mode enabled (CME):数据按 16384 个 hash slot 分散到多个 shard。
    slot 未全覆盖(< 16384)→ 部分 key 不可访问;shard 数据/连接不均 → 热点 shard。
    """
    ep = _resolve_endpoint(endpoint)
    log.info("[cluster_mode] %s", ep)

    r = _connect(ep)
    try:
        info_cluster = r.info("cluster")
        cluster_enabled = _to_int(info_cluster.get("cluster_enabled")) == 1
        cluster_info: dict[str, Any] = {}
        nodes: list[dict[str, Any]] = []
        shards_summary: dict[str, Any] = {}
        if cluster_enabled:
            # CLUSTER INFO:state / slots_assigned / known_nodes / size(shard 数)
            try:
                ci_raw = r.execute_command("CLUSTER", "INFO")
                if isinstance(ci_raw, bytes):
                    ci_raw = ci_raw.decode("utf-8")
                if isinstance(ci_raw, str):
                    for line in ci_raw.splitlines():
                        if ":" in line:
                            k, v = line.split(":", 1)
                            cluster_info[k.strip()] = v.strip()
            except redis.exceptions.ResponseError as e:
                cluster_info["_error"] = str(e)[:100]
            # CLUSTER NODES:每个节点 id / addr / flags(master/slave)/ slots
            try:
                nodes_raw = r.execute_command("CLUSTER", "NODES")
                if isinstance(nodes_raw, bytes):
                    nodes_raw = nodes_raw.decode("utf-8")
                for line in str(nodes_raw).splitlines():
                    parts = line.split()
                    if len(parts) < 8:
                        continue
                    flags = parts[2]
                    role = "master" if "master" in flags else "replica" if "slave" in flags else "?"
                    slot_ranges = parts[8:] if len(parts) > 8 else []
                    nodes.append({
                        "node_id": parts[0][:12],
                        "addr": parts[1].split("@")[0],
                        "role": role,
                        "link_state": parts[7],
                        "slot_ranges": slot_ranges,
                    })
            except redis.exceptions.ResponseError as e:
                shards_summary["_nodes_error"] = str(e)[:100]
    finally:
        r.close()

    if not cluster_enabled:
        return _wrap(
            status="ok", findings=[],
            raw_data={"endpoint": ep, "queried_at": _now_iso(), "cluster_enabled": False},
            recommendation="cluster mode disabled(单 shard,1 primary + 最多 5 replica)。"
                           "无分片 / hash slot 概念,resharding / cross-slot 不适用。",
        )

    masters = [n for n in nodes if n["role"] == "master"]
    replicas = [n for n in nodes if n["role"] == "replica"]
    slots_assigned = _to_int(cluster_info.get("cluster_slots_assigned"))
    state = str(cluster_info.get("cluster_state") or "")

    raw_data = {
        "endpoint": ep, "queried_at": _now_iso(), "cluster_enabled": True,
        "cluster_state": state,
        "slots_assigned": slots_assigned,
        "slots_ok": _to_int(cluster_info.get("cluster_slots_ok")),
        "known_nodes": _to_int(cluster_info.get("cluster_known_nodes")),
        "cluster_size_shards": _to_int(cluster_info.get("cluster_size")),
        "master_count": len(masters), "replica_count": len(replicas),
        "nodes": nodes,
        "cluster_info": cluster_info,
    }
    findings: list[dict[str, Any]] = []
    status = "ok"
    rec_parts = []

    if state and state != "ok":
        status = "critical"
        findings.append({"severity": "critical", "metric": "cluster_state", "value": state})
        rec_parts.append(f"⚠ cluster_state={state!r}(非 ok),集群不健康,部分操作会失败。")
    if slots_assigned and slots_assigned < 16384:
        status = "critical"
        findings.append({"severity": "critical", "metric": "slots_incomplete",
                         "value": f"{slots_assigned}/16384"})
        rec_parts.append(f"⚠ 只有 {slots_assigned}/16384 个 slot 被分配,部分 key 不可访问"
                         "(resharding 未完成 / 节点失联)。")
    down_nodes = [n for n in nodes if n["link_state"] != "connected"]
    if down_nodes:
        status = "critical" if status != "critical" else status
        findings.append({"severity": "critical", "metric": "node_disconnected",
                         "count": len(down_nodes)})
        rec_parts.append(f"⚠ {len(down_nodes)} 个节点 link_state != connected。")
    if masters and not replicas:
        if status == "ok":
            status = "warning"
        rec_parts.append("⚠ 所有 shard 均无 replica,任一 master 故障会丢该 shard 数据 + 失去 failover。")

    if not rec_parts:
        rec_parts.append(f"cluster 健康:{len(masters)} 个 shard / {len(replicas)} 个 replica,"
                         f"state=ok,16384 slot 全覆盖。")

    for n in nodes[:20]:
        findings.append({"severity": "info", "metric": "cluster_node",
                         "addr": n["addr"], "role": n["role"], "link": n["link_state"]})

    return _wrap(status=status, findings=findings, raw_data=raw_data,
                 recommendation=" ".join(rec_parts))


# ===========================================================================
# Tool 13 — Pub/Sub(channel 数 + output buffer fan-out 内存风险)
# ===========================================================================
@mcp.tool()
def inspect_pubsub(endpoint: str) -> dict[str, Any]:
    """巡检 pub/sub:活跃 channel / pattern 订阅数 + 慢订阅者的 output buffer 堆积 — 排查 fan-out 到大量慢订阅者导致的 output buffer 内存膨胀(client-output-buffer-limit)。endpoint 必传。

    pub/sub 输出缓冲会因订阅者消费慢而无限增长;fan-out 到很多订阅者会成倍放大内存。
    """
    ep = _resolve_endpoint(endpoint)
    log.info("[pubsub] %s", ep)

    r = _connect(ep)
    try:
        # PUBSUB CHANNELS / NUMPAT —— 活跃 channel 与 pattern 订阅
        try:
            channels = r.execute_command("PUBSUB", "CHANNELS")
            channels = [c.decode("utf-8") if isinstance(c, bytes) else str(c) for c in (channels or [])]
        except redis.exceptions.ResponseError:
            channels = []
        try:
            numpat = _to_int(r.execute_command("PUBSUB", "NUMPAT"))
        except redis.exceptions.ResponseError:
            numpat = 0

        # CLIENT LIST 里看 pub/sub 订阅者的 output buffer(omem / obl / oll)
        clients = r.client_list()
        info_clients = r.info("clients")
    finally:
        r.close()

    # 找 output buffer 大的 client(omem = output buffer 内存字节)
    big_obuf = []
    pubsub_clients = 0
    for c in clients:
        flags = str(c.get("flags") or "")
        sub = _to_int(c.get("sub")) + _to_int(c.get("psub"))
        if sub > 0 or "P" in flags:  # P flag = pubsub
            pubsub_clients += 1
        omem = _to_int(c.get("omem"))
        oll = _to_int(c.get("oll"))  # output list length
        if omem > 1024 * 1024:  # > 1MB output buffer
            big_obuf.append({
                "addr": str(c.get("addr") or ""), "name": str(c.get("name") or ""),
                "omem_bytes": omem, "omem_pretty": _bytes_to_human(omem),
                "output_list_len": oll, "sub": _to_int(c.get("sub")), "psub": _to_int(c.get("psub")),
            })
    big_obuf.sort(key=lambda x: x["omem_bytes"], reverse=True)

    raw_data = {
        "endpoint": ep, "queried_at": _now_iso(),
        "active_channels": len(channels),
        "channels_sample": channels[:50],
        "pattern_subscriptions": numpat,
        "pubsub_client_count": pubsub_clients,
        "clients_with_big_output_buffer": big_obuf[:_TOP_N_DEFAULT],
        "total_blocked_clients": _to_int(info_clients.get("blocked_clients")),
    }

    findings: list[dict[str, Any]] = []
    status = "ok"
    rec_parts = []

    if big_obuf:
        max_omem = big_obuf[0]["omem_bytes"]
        if max_omem >= 100 * 1024 * 1024:
            status = "critical"
            rec_parts.append(f"⚠ 有订阅者 output buffer {big_obuf[0]['omem_pretty']} ≥ 100MB,"
                             "消费太慢导致缓冲堆积,可能触发 client-output-buffer-limit 断连或 OOM。")
        else:
            status = "warning"
            rec_parts.append(f"有 {len(big_obuf)} 个订阅者 output buffer > 1MB(最大 {big_obuf[0]['omem_pretty']}),"
                             "关注慢订阅者。")
        for b in big_obuf[:10]:
            findings.append({"severity": "warning", "metric": "pubsub_output_buffer",
                             "addr": b["addr"], "omem": b["omem_pretty"]})

    if not rec_parts:
        rec_parts.append(f"pub/sub:{len(channels)} 个活跃 channel,{numpat} 个 pattern 订阅,"
                         f"{pubsub_clients} 个订阅者,无 output buffer 堆积。")

    return _wrap(status=status, findings=findings, raw_data=raw_data,
                 recommendation=" ".join(rec_parts))


if __name__ == "__main__":
    log.info("starting ElastiCache (Redis/Valkey) inspect MCP server on :8000 with 13 tools")
    mcp.run(transport="streamable-http")
