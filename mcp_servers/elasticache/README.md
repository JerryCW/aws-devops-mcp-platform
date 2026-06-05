# ElastiCache (Redis / Valkey) 巡检 MCP Server

一个通用的 ElastiCache for Redis / ElastiCache for Valkey **只读巡检** MCP server,
通过 13 个语义化工具,让 DevOps Agent 能安全地排查线上缓存问题(内存压力、慢命令、
big key / hot key、复制延迟、eviction、cluster 分片健康、pub/sub fan-out 等)。

> 它是「ElastiCache 巡检能力」,不是「某个实例的巡检器」—— 不绑定任何缓存实例。
> 每个工具的 `endpoint` 是**调用时必传**的,由 Agent 按用户上下文给出。
> 一套 server 天然巡检 N 个同类 ElastiCache 实例。

**适用引擎**:ElastiCache for Redis / ElastiCache for Valkey / 自建 Redis(协议兼容)。
**不支持 Memcached**(无 INFO replication / SLOWLOG / persistence,协议完全不同)。

## 安全模型(先看这个)

| 设计 | 说明 |
|---|---|
| **只读账号** | 容器用专门的 ACL 只读用户 `mcp_devops_ro` 连库,权限只给 `INFO` / `SLOWLOG` / `CLIENT` / `SCAN` / `MEMORY` 等只读命令,**绝不用 default 全权用户** |
| **命令全固化** | 13 个工具调用的 Redis 命令都写死在代码里,只发 `INFO` / `SLOWLOG GET` / `CLIENT LIST` / `SCAN` / `MEMORY USAGE` / `OBJECT` / `CLUSTER` / `PUBSUB` 等**只读/元数据命令**,**没有任何"传命令进来执行"的口子** |
| **看不到业务数据** | 工具能看到"key 多大、什么类型、TTL、访问频次",但**永远不返回 key 的 value**(big_keys / hot_keys 只返回 key 名 / size / type / TTL) |
| **不卡业务库** | connect / socket timeout 各 10s;SCAN 类工具有扫描上限(默认 5000,上限 50000)防止数百万 key 库扫挂 |
| **endpoint 不注入** | 容器不预设任何实例地址,连哪个节点由 Agent 调用时传入 |
| **TLS 自适应** | 默认 `auto`,按 endpoint 探测 TLS 可用性,客户开不开 in-transit encryption 都能连(见下文) |
| **凭据走 Secrets Manager** | 密码不进代码、不进镜像、不进日志,容器运行时按需拉取(5 分钟 TTL 缓存) |

---

## ⭐ 只读用户 `mcp_devops_ro` 配置(最重要)

巡检容器用这个 ACL 只读用户连库。**框架不会自动创建它**——需要管理员手工创建一次。
所有同类型 ElastiCache 实例**共用同一套** `mcp_devops_ro` 凭据(单 secret 多库策略)。

> ⚠ ElastiCache 是 **managed service**:`CONFIG SET` / `CONFIG GET` 被禁用,
> ACL 的管理方式与自建 Redis 不同 —— 通过 **AWS 控制台 / CLI 的 User & User Group**
> 管理,**不是** `redis-cli ACL SETUSER`。

### 方式 A:ElastiCache User Group(推荐,RBAC)

ElastiCache for Redis 6+ / Valkey 支持 RBAC。用 AWS CLI 创建只读 User 并绑到集群:

```bash
# 1) 创建只读 ACL user(只允许只读命令 + 巡检命令,所有 key 只读)
#    ElastiCache ACL 语法:命令用 +@category / +command,key 用 ~pattern
aws elasticache create-user \
  --user-id mcp-devops-ro \
  --user-name mcp_devops_ro \
  --engine redis \
  --passwords "<32+ 字符强随机密码>" \
  --access-string "on ~* +@read +@connection +@keyspace +info +slowlog +client|list +memory|usage +memory|doctor +object|freq +object|idletime +object|encoding +cluster|info +cluster|nodes +pubsub|channels +pubsub|numpat +scan +ttl +type -@dangerous" \
  --region us-east-1

# 2) 把 user 加进一个 user group
aws elasticache create-user-group \
  --user-group-id mcp-devops-ro-group \
  --engine redis \
  --user-ids default mcp-devops-ro \
  --region us-east-1

# 3) 把 user group 关联到要巡检的集群(replication group)
aws elasticache modify-replication-group \
  --replication-group-id <你的集群 id> \
  --user-group-ids-to-add mcp-devops-ro-group \
  --apply-immediately \
  --region us-east-1
```

> `access-string` 解读:
> - `on` 启用、`~*` 所有 key、`-@dangerous` 禁危险命令(FLUSHALL / CONFIG / SHUTDOWN 等)
> - `+@read` 只读数据命令、`+@connection` PING/ECHO、`+@keyspace` SCAN/TTL/TYPE
> - `+info` `+slowlog` `+client|list` `+memory|usage` `+cluster|info` `+pubsub|channels` 等是各巡检工具点名需要的命令
> - **没有任何写命令**(SET/DEL/EXPIRE 都不给),即使被注入也改不了数据

### 方式 B:Legacy AUTH token(只有密码,无用户名)

ElastiCache 老集群可能用 Redis AUTH(单一 token,无 ACL)。这种情况 Secret 里
**只放 password**,容器自动用 legacy AUTH 模式连接。**注意**:legacy AUTH 是全权限的,
做不到只读限制,建议尽快迁到方式 A 的 RBAC。

### 方式 C:无认证(仅限内网隔离 + 测试)

集群没开 AUTH 时,Secret 留空 / 不配 `REDIS_AUTH_SECRET_NAME`,容器无凭据连接。
**生产环境强烈不推荐**。

### 灌密码进 Secrets Manager

部署时 CDK 已建好空壳 Secret(密码 = `PLACEHOLDER_REPLACE_ME`),把真密码灌进去:

```bash
# 方式 A(RBAC,有用户名)
aws secretsmanager put-secret-value \
  --secret-id aws-devops-mcp/elasticache/devops-readonly \
  --region us-east-1 \
  --secret-string '{"username":"mcp_devops_ro","password":"<同 create-user 那个密码>"}'

# 方式 B(legacy AUTH,只有密码,username 留空字符串或不写)
aws secretsmanager put-secret-value \
  --secret-id aws-devops-mcp/elasticache/devops-readonly \
  --region us-east-1 \
  --secret-string '{"username":"","password":"<AUTH token>"}'
```

> Secret 名固定 `aws-devops-mcp/elasticache/devops-readonly`,在 **Runtime 所在 region(us-east-1)**,
> 不是集群所在 region。容器跨 region 拉这个 Secret。

---

## TLS 自适应(`REDIS_USE_TLS`)

TLS(in-transit encryption)是**实例属性**:一套 server 巡检多个集群,可能有的开有的没开。
所以默认按 endpoint **自适应探测**,而不是写死。

| `REDIS_USE_TLS` | 行为 |
|---|---|
| `auto`(**默认**) | 首次连某 endpoint 时探测:先试 TLS(ElastiCache 最佳实践),握手失败回退明文;决策**按 endpoint 缓存**,后续 tool call 复用 |
| `true` | 强制 TLS,跳过探测(全集群都开 TLS 时省一次握手) |
| `false` | 强制明文(全集群都没开 TLS) |

- 客户**不开 TLS**:`auto` 会探测后自动用明文,无需改任何配置即可连通。
- 想确定某 endpoint 实际用了哪种:调 `inspect_overview`,`raw_data.tls` 字段会显示 `enabled` / `disabled`。
- ElastiCache TLS 用自签证书,容器侧 `ssl_cert_reqs=None`(跳过 hostname 校验),这是连 ElastiCache 的标准做法。

---

## 工具清单(13 个)

### 数据面巡检(11 个,搬自 Valkey 沉淀)
| 工具 | 用途 |
|---|---|
| `inspect_overview` | 总览:版本 / uptime / 角色 / 客户端数 / TLS — 排障第一步 |
| `inspect_memory` | 内存使用 + maxmemory 占比 + 碎片化 + 累计 eviction |
| `inspect_slow_queries` | 慢命令 top N(SLOWLOG GET),找 KEYS / 大集合 HGETALL 等 |
| `inspect_clients` | 客户端连接分布(CLIENT LIST),排查连接泄漏(长 idle) |
| `inspect_keyspace` | 各 db 的 key 数 / TTL 占比 / 平均 TTL,排查永不过期 key 堆积 |
| `inspect_stats` | 命中率 + OPS + 拒绝连接 + 命令调用 top |
| `inspect_replication` | 主从复制状态 + lag,排查复制延迟 / 失去高可用 |
| `inspect_persistence` | RDB / AOF 状态 + last save / fsync 错误 |
| `inspect_big_keys` | SCAN 全库找 top N 内存占用大 key(只返回 key 名/大小/类型/TTL) |
| `inspect_hot_keys` | SCAN 全库找 top N 访问频繁 key(OBJECT FREQ,LFU 模式) |
| `inspect_eviction` | eviction 速率 + maxmemory 占比 + policy 健康 |

### ElastiCache 特性补充(2 个,基于 elasticache skill)
| 工具 | 用途 |
|---|---|
| `inspect_cluster_mode` | cluster mode(分片数 / hash slot 覆盖 / 各 shard 状态)— 排查 resharding 后 slot 缺失、cross-slot 错误 |
| `inspect_pubsub` | pub/sub channel 数 + 慢订阅者 output buffer 堆积(fan-out 内存风险) |

---

## 注意事项

### 1. SCAN 类工具(big_keys / hot_keys)建议传 replica endpoint
这两个工具会 SCAN 全库 + 批量 `MEMORY USAGE` / `OBJECT FREQ`,对 CPU 有压力。
**生产环境建议传 replica(读副本)endpoint**,避免增加 master CPU 负担。
两个工具都有 `max_scan_count`(默认 5000,上限 50000)护栏,大库扫到上限即停并提示。

### 2. hot_keys 需要 LFU 模式才准
`inspect_hot_keys` 用 `OBJECT FREQ`,只有 `maxmemory-policy` 是 `allkeys-lfu` / `volatile-lfu`
时返回真实访问频次。非 LFU 模式自动降级用 `OBJECT IDLETIME`(最近访问时间)兜底,
并在 recommendation 里提示切 LFU。

### 3. cluster mode disabled 不是错误
`inspect_cluster_mode` 在非 cluster mode(单 shard,1 primary + 最多 5 replica)的集群上
会返回 `ok` + 提示"cluster mode disabled",不是故障。hash slot / resharding 概念只适用于
cluster mode enabled。

### 4. ElastiCache 禁用 CONFIG 命令 —— 已优雅降级
ElastiCache managed service 禁用 `CONFIG GET` / `CONFIG SET`。涉及读 config 的地方
(如 `inspect_slow_queries` 读 slowlog 阈值)会捕获 `ResponseError` 降级到默认值(10ms),
不影响工具返回。

### 5. 密码 / TLS 轮换
- 轮换密码:先用 `aws elasticache modify-user` 改 ACL user 密码,再 `put-secret-value` 同步 Secret。
  容器不用重启,最长 5 分钟(Secret TTL 缓存)后自动用新密码。
- 改了集群 TLS 配置(开 / 关 in-transit encryption):`auto` 模式的 TLS 决策缓存在容器生命周期内,
  改 TLS 后需重启 Runtime 容器让它重新探测(或临时把 `REDIS_USE_TLS` 设成明确值)。

### 6. 跨 region
容器跑在 us-east-1,集群可在任意 region(经 TGW)。要确保:
- 集群安全组放行 Runtime VPC CIDR 的 6379
- Runtime VPC ↔ 集群 VPC 的 TGW 路由双向通
- Secret 在 us-east-1(Runtime 所在 region)

---

## 容器环境变量(由 target stack 注入,不用手动配)

| 变量 | 说明 |
|---|---|
| `DB_SECRET_NAME` | Secret 名,如 `aws-devops-mcp/elasticache/devops-readonly`(target stack 统一注入此变量名)|
| `REDIS_AUTH_SECRET_NAME` | 可选,显式指定凭据 Secret;不设则 fallback 用 `DB_SECRET_NAME` |
| `REDIS_PORT` | 默认 6379 |
| `REDIS_USE_TLS` | `auto`(默认) / `true` / `false`,见上文 TLS 自适应 |
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
