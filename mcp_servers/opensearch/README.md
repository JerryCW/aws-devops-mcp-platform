# OpenSearch 巡检 / 查询 MCP Server

让 DevOps Agent 能查询、分析 OpenSearch / Amazon OpenSearch Service 集群(索引、映射、
查询 DSL、分片、集群健康等),用于日志检索与集群排障。

> ## ⚠️ 代码来源声明(Attribution)
>
> **本目录的核心代码不是本项目原创,而是迁移(借鉴)自第三方开源项目:**
>
> - **上游项目**:[opensearch-project/opensearch-mcp-server-py](https://github.com/opensearch-project/opensearch-mcp-server-py)
> - **版权**:Copyright OpenSearch Contributors
> - **许可**:Apache License 2.0
>
> **哪些是 upstream 的、哪些是本项目写的:**
> - `mcp_server_opensearch/`、`opensearch/`、`tools/` —— **upstream 原样代码,未改核心逻辑**(保留原版权头)
> - `entrypoint.py`、`Dockerfile`、`requirements.txt`、`.dockerignore`、`README.md` —— **本项目原创的适配层**(为接 AgentCore Runtime + Secrets Manager 凭据)
>
> 完整 license 见同目录 [`LICENSE.txt`](./LICENSE.txt),来源与修改说明见 [`NOTICE.txt`](./NOTICE.txt)。
> 遵循 Apache-2.0 第 4 条:保留版权声明、附 license 副本、NOTICE 标注修改。

## 与其它 server 的设计差异(先看这个)

| 维度 | PG/MySQL/ElastiCache(本框架自研) | OpenSearch(迁移 upstream) |
|---|---|---|
| 工具哲学 | 语义化罐头、SQL 固化 | **通用查询透传**(SearchIndexTool/GenericApi 收 query DSL)|
| 框架 | FastMCP | 底层 mcp.server.Server,从 OpenAPI 动态生成工具 |
| endpoint | 调用必传 | **支持调用必传**(per-call `opensearch_url` override)|
| 认证 | 只读账号 + Secret | basic auth(FGAC 内部用户)/ IAM / no-auth |

⚠ **它包含通用查询工具**(`SearchIndexTool` 收完整 query DSL、`GenericOpenSearchApiTool`
透传 REST API),这与本框架 PG/MySQL 的"语义化罐头"原则不同(shall-not #3)。这是
OpenSearch 日志检索场景的客户诉求(自由查询日志),作为迁移工具保留。安全边界由
**只读账号权限**保证(见下)。

## 安全模型

| 设计 | 说明 |
|---|---|
| **只读账号** | 容器用 `mcp_devops_ro`(FGAC 内部用户)连集群,只授读权限,**不用 admin** |
| **endpoint 不注入** | 容器不预设实例地址;每个 tool 支持调用时传 `opensearch_url`(per-call override)|
| **凭据走 Secrets Manager** | entrypoint 启动时从 `DB_SECRET_NAME` 拉 basic auth 凭据注入,密码不进镜像/日志(SHALL NOT #12)|
| **TLS** | `OPENSEARCH_SSL_VERIFY=true`(Amazon OpenSearch 有有效证书,校验)|

## 启用的工具(19 个)

> 面向两个场景:**集群巡检** + **日志查询(替代 Grafana)**。
> 部署机制:upstream single 模式默认暴露 9 个 core_tools,再经 `OPENSEARCH_ENABLED_TOOLS`
> 追加 10 个巡检工具 = 19 个。搜索相关性 / ML memory / agentic 等 16 个 upstream
> 工具与这两个场景无关,默认不启用。

### 日志查询(替代 Grafana)
| 工具 | 用途 |
|---|---|
| `SearchIndexTool` | **日志检索核心**:query DSL 搜索(时间窗/字段过滤/全文/聚合,size ≤ 100)|
| `MsearchTool` | 多查询批量(多维度对比)|
| `CountTool` | 文档计数(先数命中量再细查)|
| `ListIndexTool` | 列日志索引(如 `k8s-logs-*`)|
| `IndexMappingTool` | 看字段映射(写查询前的 PREREQUISITE)|
| `ExplainTool` | 解释某文档为何匹配/不匹配查询(调日志查询条件时排查)|

### 集群巡检
| 工具 | 用途 / 对应 skill 故障域 |
|---|---|
| `ClusterHealthTool` | 集群健康 green/yellow/red(A 集群健康)|
| `CatNodesTool` | 节点负载 heap/cpu/disk(A/B/C 核心)|
| `GetNodesTool` | 节点详细 JVM/线程池/插件(B JVM/GC)|
| `GetClusterStateTool` | 集群状态 + settings(含 watermark,C 存储)|
| `GetShardsTool` | 分片状态 + unassigned reason(D 分片)|
| `GetAllocationTool` | 各节点磁盘分配(C 磁盘水位)|
| `GetSegmentsTool` | Lucene 段(force merge 判断)|
| `GetIndexInfoTool` | 索引详情 mappings/settings/aliases(E 映射)|
| `GetIndexStatsTool` | 索引统计 doc/store/性能(B 性能)|
| `GetQueryInsightsTool` | top 慢查询(B 搜索延迟)|
| `GetNodesHotThreadsTool` | 热线程(B CPU 飙高定位)|
| `GetLongRunningTasksTool` | 长任务 merge/recovery/search(D/E)|

### 兜底
| 工具 | 用途 |
|---|---|
| `GenericOpenSearchApiTool` | 透传任意**只读** OpenSearch REST API(自带写保护:POST/PUT/DELETE 受 allow_write 控制)|

### skill 高频端点用 GenericApi 兜底(无专用工具)
opensearch-troubleshooting skill 这几个排障端点 upstream 无专用工具,经
`GenericOpenSearchApiTool` 调用(纯 GET 只读):

| skill 场景 | 调用方式 |
|---|---|
| A1 红集群 / D1 unassigned shard(**最高频**)| `GET /_cluster/allocation/explain` |
| 分片恢复进度 | `GET /_cat/recovery?v` |
| J1/J2 ISM 策略 / rollover 失败 | `GET /_plugins/_ism/explain/<index>` |

---

## ⭐ 只读用户 `mcp_devops_ro` 配置(FGAC)

Amazon OpenSearch Service 开了细粒度访问控制(FGAC)时,用 OpenSearch Dashboards 的
Security 插件或 REST API 建只读用户 + role。

### 第 1 步:建只读 role(只读所有索引 + cluster monitor)

```bash
# 通过 _plugins/_security REST API(用 master user 调)
curl -XPUT "https://<domain-endpoint>/_plugins/_security/api/roles/mcp_devops_ro_role" \
  -u "<master-user>:<master-pass>" -H "Content-Type: application/json" -d '{
  "cluster_permissions": ["cluster_composite_ops_ro", "cluster_monitor"],
  "index_permissions": [{
    "index_patterns": ["*"],
    "allowed_actions": ["read", "indices_monitor", "indices:admin/mappings/get", "indices:monitor/*"]
  }]
}'
```

### 第 2 步:建内部用户

```bash
curl -XPUT "https://<domain-endpoint>/_plugins/_security/api/internalusers/mcp_devops_ro" \
  -u "<master-user>:<master-pass>" -H "Content-Type: application/json" -d '{
  "password": "<32+ 字符强随机密码>",
  "backend_roles": []
}'
```

### 第 3 步:role mapping(把用户绑到 role)

```bash
curl -XPUT "https://<domain-endpoint>/_plugins/_security/api/rolesmapping/mcp_devops_ro_role" \
  -u "<master-user>:<master-pass>" -H "Content-Type: application/json" -d '{
  "users": ["mcp_devops_ro"]
}'
```

### 第 4 步:灌进 Secrets Manager

```bash
aws secretsmanager put-secret-value \
  --secret-id aws-devops-mcp/opensearch/devops-readonly \
  --region us-east-1 \
  --secret-string '{"username":"mcp_devops_ro","password":"<同上那个密码>"}'
```

> Secret 名固定 `aws-devops-mcp/opensearch/devops-readonly`,在 **Runtime 所在 region(us-east-1)**。

---

## 注意事项

### 1. 容器启动不依赖凭据(重要)
entrypoint 在启动期**不因 placeholder/缺凭据退出**——因为 AgentCore Gateway 创建 target 时
要立即对 Runtime 做 `tools/list` 健康检查(不连 OpenSearch)。若启动期因缺凭据退出,容器起不来 →
Gateway "Failed to connect and fetch tools" → 部署失败(rev1 实测踩过,见 development-trace)。
真凭据问题在实际查询时由 upstream 报错。

### 2. endpoint per-call
跟 PG/MySQL 一致,容器不注入实例 endpoint。Agent 调用每个 tool 时传 `opensearch_url`
参数指定要查的集群。一套 server 可巡检多个 OpenSearch 域。

### 3. 网络(VPC 域)
Amazon OpenSearch VPC 域:
- OpenSearch 域安全组放行 Runtime VPC 出向网段(本环境 10.2.0.0/16)的 **443**
- Runtime VPC ↔ OpenSearch VPC 的 TGW 路由双向通
- egress port 是 **443**(HTTPS),不是 9200

### 4. 密码轮换
先用 `_plugins/_security/api/internalusers` 改密码,再 `put-secret-value` 同步 Secret。
容器需重启(entrypoint 在启动时一次性拉凭据;不像自研 server 有运行时 TTL 缓存)。

---

## 容器环境变量(由 target stack 注入)

| 变量 | 说明 |
|---|---|
| `DB_SECRET_NAME` | Secret 名(entrypoint 据此拉 basic auth 凭据)|
| `OPENSEARCH_SSL_VERIFY` | 默认 true |
| `AWS_REGION` | Secret 所在 region |
| `OPENSEARCH_NO_AUTH` | 可选,本地无凭据测试用 |
| ~~OPENSEARCH_URL~~ | **不注入** — endpoint 是 tool 调用必传参数 |

## Attribution

迁移自 opensearch-project/opensearch-mcp-server-py,遵循 Apache-2.0。upstream 源码
(`mcp_server_opensearch/` `opensearch/` `tools/`)保持不改;本框架新增 `entrypoint.py`
`Dockerfile` `requirements.txt` `.dockerignore` `README.md`。
