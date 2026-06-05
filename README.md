# TAM DevOps MCP Platform

为 AWS DevOps Agent 提供数据访问能力扩展的统一框架。通过 MCP 协议把 RDS PostgreSQL / Valkey 等数据源以"巡检工具"形式注入 DevOps Agent。部署是**两步、可插拔**的:

1. **第一步:部署 Gateway**(一次,极简)— AgentCore Gateway,IAM 入站,无 target
2. **第二步:按需注册 target**(每个 MCP server 一个独立部署单元)

## 设计原则

- **Gateway 与 target 解耦**:先有 Gateway,再按需挂 server。装哪个数据源就 deploy 哪个 target stack。
- **IAM 形态**:inbound 用 AWS SigV4(DevOps Agent 调用),Gateway → Runtime 出站用 GATEWAY_IAM_ROLE,**零 Cognito**。
- **endpoint 不注入容器**:MCP server 是「PG 巡检能力」而非「某个 cluster 的巡检器」。容器不预设任何实例,
  endpoint 是 tool 的**必传调用参数**,由 DevOps Agent 按用户上下文给出。一套 server 天然巡检 N 个同类实例。
- **配置驱动**:server 全部由 `servers.json` 声明。新增数据源 = 加配置 + 写 tool 函数,**不写新 stack 类**。
- **alpha 隔离**:`aws_bedrock_agentcore_alpha` 只在 `framework/` 下出现。

## 架构

```
DevOps Agent
    │ AWS SigV4 (service=bedrock-agentcore, action=InvokeGateway)
    ▼
DevopsMcpGatewayStack
   AgentCore Gateway: aws-devops-mcp
   Authorizer: AWS_IAM
    │
    │ Gateway 自身 SigV4 出站(GATEWAY_IAM_ROLE + IamCredentialProvider)
    ▼
DevopsMcp-<server>  (按需,每个一个独立 stack)
   Runtime(VPC mode, AWS_IAM 入站) + Gateway target + 空壳 Secret
    │ TGW
    ▼
   目标数据源(endpoint 由 tool 调用时传入,不在部署里)
```

全部部署在同一个 region(默认跟随你的 AWS profile / `AWS_REGION`)。

> **多 region**:region 不硬编码,由 CDK CLI 从当前凭据自动注入。换区部署:
> `AWS_REGION=ap-southeast-1 bash scripts/deploy_gateway.sh`,或 `cdk deploy -c region=ap-southeast-1 ...`。
> 前提:目标 region 必须支持 AgentCore(Runtime + Gateway)。

## 第一步:部署 Gateway

### 前置条件

| 项 | 要求 |
|---|---|
| AWS 凭据 | `aws sts get-caller-identity` 能跑 |
| Python | 3.13+ |
| Node | 16+(为了 `npx cdk`)|
| Docker | 第一步**不需要**(不 build 容器)|

### 部署

```bash
bash scripts/deploy_gateway.sh
```

脚本依次跑:前置检查 → `pip install` → `cdk bootstrap` → `cdk deploy DevopsMcpGatewayStack` → `verify_gateway.py`(SigV4 链路验证,tools/list 返回 0 个 tool 符合预期)。

预期末尾:`✅ Gateway verification passed`

### Gateway 输出(供第二步 target stack 引用)

| Output / Export | 用途 |
|---|---|
| `DevopsMcp-GatewayId` | Gateway ID |
| `DevopsMcp-GatewayUrl` | DevOps Agent 注册用的 MCP endpoint |
| `DevopsMcp-GatewayArn` | 注册时填 / IAM 权限 resource |
| `DevopsMcp-GatewayName` | target stack 挂载用 |
| `DevopsMcp-GatewayRoleArn` | target stack 引用,加 InvokeAgentRuntime 权限 |
| `DevopsMcp-Region` | us-east-1 |

查看:

```bash
aws cloudformation describe-stacks --stack-name DevopsMcpGatewayStack \
  --region us-east-1 --query 'Stacks[0].Outputs' --output table
```

## 第二步:按需注册 MCP server target

### 配置

复制示例,填你环境的网络信息(只有部署必需的网络信息,**没有数据源 endpoint**):

```bash
cp servers.json.example servers.json
# 编辑 servers.json,把 <REPLACE_ME_*> 改成实际值
```

`servers.json` 字段:
- `runtime`:Runtime 部署的 VPC + 2 个 private subnet + AZ(与部署 region 一致)
  (注:AgentCore 在 us-east-1 只支持 us-east-1a/b/c,不支持 use1-az6;其它 region 以实际可用 AZ 为准)
- `servers.<name>`:每个 MCP server 一个条目
  - `enabled`:false 的不部署
  - `egressCidr` / `egressPort`:数据源**网段**(SG 放行用),不是具体实例 endpoint
  - `secretName` / `secretUsername`:凭据 Secret(框架建空壳,客户灌真密码)

### 部署(按需选择装哪个)

```bash
# 只装 PG
npx cdk@2.1124.1 deploy DevopsMcp-rdspostgres --require-approval never

# 装多个
npx cdk@2.1124.1 deploy DevopsMcp-rdspostgres DevopsMcp-valkey --require-approval never
```

> 第二步会 build linux/arm64 容器镜像,需要 Docker 在线。

### 配置数据源凭据(客户 DBA / SRE 手工)

框架建的是空壳 Secret(PLACEHOLDER 密码)。客户在数据库上建只读用户 + 把同一密码灌进 Secret。
PG 侧示例:

```sql
ALTER USER mcp_devops_ro WITH PASSWORD '<强密码>';
```
```bash
aws secretsmanager put-secret-value \
  --secret-id aws-devops-mcp/rds-postgres/devops-readonly \
  --region us-east-1 \
  --secret-string '{"username":"mcp_devops_ro","password":"<同上>"}'
```

### 验证

```bash
# 只验 target 挂载(Gateway tools/list 含 28 个 PG tool)
python scripts/verify_pg.py

# 真实巡检(传 endpoint,endpoint 不进部署)
python scripts/verify_pg.py --pg-endpoint my-cluster.cluster-xxx.us-east-1.rds.amazonaws.com
```

## 销毁

```bash
# 只删 Gateway
bash scripts/destroy.sh

# 先删 target 再删 Gateway
bash scripts/destroy.sh DevopsMcp-rdspostgres
```

> AgentCore Runtime delete 偶发慢(VPC ENI 异步释放),SG 可能卡几分钟到十几分钟甚至 DELETE_FAILED;等 ENI 释放后重跑即可。

## 新增一个数据源

> **核心保证:加 target 永远不碰 Gateway。** Gateway stack 被 target stack 单向引用
> (`Fn.import_value`),挂 target 的写操作发生在 target stack 名下,不动 Gateway 资源。
> `cdk deploy DevopsMcp-<new>` 只动那一个 stack,Gateway 永不重部署。

### user/password 认证 + 单 TCP 端口(MySQL / MQ / Valkey 这类)——零代码扩展

1. 写 `mcp_servers/<datasource>/`(main.py + Dockerfile + requirements.txt + .dockerignore),tool 的 endpoint 设必传
2. 在 `servers.json` 加一个 `servers.<name>` 条目(端口写进 `extraEnv`)
3. `cdk deploy DevopsMcp-<name>`
4. (可选)写 `scripts/verify_<name>.py`

**不需要写新的 stack 类** —— `stacks/target_stack.py` 是通用的,由配置驱动。

### IAM 认证(MSK / OpenSearch 这类)——增量扩展 ServerConfig

这类服务用 IAM(SASL/IAM 或 SigV4)而非 user/password,需要扩展 `ServerConfig` + `target_stack.py`
(约 30 行,PG/Valkey 不受影响,framework 和 Gateway 完全不动)。具体改动清单见
`stacks/target_stack.py` 顶部的「扩展指南」注释。要点:凭据字段改 Optional、端口改多端口、
加 `extra_iam_statements` 给 Runtime 角色补数据源权限(如 `kafka-cluster:*` / `es:*`)。

## 目录结构

```
.
├── app.py                       # CDK app 入口(Gateway + 按 servers.json 生成 target stack)
├── cdk.json                     # 只放 CDK feature flag
├── servers.json(.example)       # 第二步 server 注册表(真实文件 gitignore)
├── requirements.txt
├── framework/                   # alpha 隔离层(只此处可 import alpha 包)
│   ├── gateway_construct.py     # Gateway + 跨 stack 挂 target(IAM SigV4)
│   └── runtime_construct.py     # AgentCore Runtime + 容器镜像
├── stacks/
│   ├── gateway_stack.py         # 第一步:Gateway(无 target)
│   └── target_stack.py          # 第二步:通用 target stack(配置驱动)
├── mcp_servers/
│   └── rds_postgres/            # PG 巡检容器(20 tool,endpoint 必传)
├── scripts/
│   ├── deploy_gateway.sh        # 第一步一键部署
│   ├── verify_gateway.py        # Gateway SigV4 链路验证
│   ├── verify_pg.py             # PG target 验证
│   └── destroy.sh
└── archive/                     # 历史代码(旧根 / V2 / V3),仅存档参考
```
