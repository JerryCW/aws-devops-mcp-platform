# Design Document

## Overview

Spec 2 在 Spec 1 已完成的链路(DevOps Agent → Gateway → Spec 0 Runtime → hello_world)之上,新增**第二个 MCP target**,后端是**部署在 jupiter-dev VPC 私有子网的 AgentCore Runtime**,运行**第一个真实巡检 tool** `rds_mysql_inspect_long_transactions`,从 jupiter-dev-slurm-db 读取长事务列表。

完成后 DevOps Agent 端能调:

```
rdsmysql___rds_mysql_inspect_long_transactions(threshold_seconds=60)
```

返回 conventions A8 标准结构(status / findings / raw_data / recommendation)。

新增 AWS 资源(单 stack 内):

- **AgentCore Runtime × 1**(VPC 模式,name = `spec2_rds_mysql`,部署到 jupiter-dev-vpc-db 3 个私有子网)
- **EC2 SecurityGroup × 1**(`sg-spec2-runtime`,outbound 到 RDS SG 的 3306)
- **EC2 SG ingress rule × 1**(给 jupiter-dev-slurm-db SG 加白来自 Runtime SG 的 3306 入站)
- **Secrets Manager Secret × 1**(`mcp-inspect/rds-mysql/devops-readonly`,默认 PLACEHOLDER 密码)
- **CloudWatch LogGroup × 1**(`/aws/bedrock-agentcore/runtimes/spec2_rds_mysql`,L2 自动)
- **MCP Target × 1**(挂在 Spec 1 Gateway 上,name = `rdsmysql`,后端 = 本 stack 新建的 Runtime)
- **IAM Role / Policy**(Runtime 执行角色,L2 自动 + 增量 SecretsManager 读权限)

新增本机工具:

- **`scripts/verify_spec2.py`** — 端到端验证(同 Spec 1 风格,SSE utf-8 修复内置)
- **`scripts/destroy_spec2.sh`** — 仅清 Spec 2,不连带 Spec 0/1
- **`scripts/bootstrap_spec2.sh`** — 把 Spec 1 Gateway ID 写到 SSM Parameter,供 Spec 2 跨 stack 引用(Spec 1 没显式 export GatewayId 这个 OutputKey 的形式给 Spec 2 用,走 SSM 中转更稳;沿用 Spec 1 Task 2 的 boot 脚本同款模式)
- **`scripts/init_rds_user.sh`** — 一次性 RDS admin 操作(王总在有 RDS admin 凭据的 session 跑):生成 32 字符随机密码 → `aws secretsmanager put-secret-value` 灌入 → 输出 SQL 让王总 copy 到 RDS admin session 执行,创建 `mcp_devops_ro` user

设计核心决定:

1. **alpha 隔离边界不变**:VPC 模式通过扩展 `framework/runtime_construct.py`(Spec 0 引入)实现,不引入新 framework 文件,不污染 Stack 层
2. **凭据策略 = 单 secret 多库(Mode A)**:王总决策,运维成本远高于"一库一密"的爆炸半径收益,巡检场景接受
3. **Spec 1 Gateway 复用**:不创建新 Gateway,挂第二个 target 上去,DevOps Agent 端无需重新注册
4. **VPC 网络拓扑复用 jupiter-dev**:Runtime 和 RDS 同 VPC 同 3 个 db 子网,SG 互通最简单,避免 VPC Peering / Endpoint 的额外复杂度
5. **RDS user 由王总手工创建**:cdk 不管 RDS admin 凭据(`shall-not.md` SHALL NOT #11 / #13 + 本 spec SHALL NOT #11),Spec 2 提供一键脚本但执行在 cdk 流程外

## Architecture

### 部署后运行时架构

```
┌─────────────────────────────────────────────────────────────────┐
│  AWS DevOps Agent(Agent Space 控制台,已注册 Spec 1 Gateway)    │
│       │ Streamable HTTP + Cognito JWT(scope=mcp/invoke)         │
│       ▼                                                          │
│  Spec 1 AgentCore Gateway(Public Network,本 spec 不动)         │
│       │  tools/list 现在返回 2 个 tool:                          │
│       │    - spec0helloworld___hello_world          ← Spec 1 注册│
│       │    - rdsmysql___rds_mysql_inspect_long_transactions ← 本 │
│       │                                                          │
│       ├─→ MCP Target spec0helloworld → Spec 0 Runtime(不动)    │
│       │                                                          │
│       └─→ MCP Target rdsmysql(本 spec 新增)                     │
│            │ Token Vault 自动换 Cognito access_token             │
│            │ 共用 Spec 1 OAuth2CredentialProvider                │
│            ▼                                                     │
│       ╔═══════════════════════════════════════════════════════╗  │
│       ║ McpInspectSpec2Stack(本 spec)                        ║  │
│       ║                                                       ║  │
│       ║  AgentCore Runtime(VPC 模式)                         ║  │
│       ║   ├─ Cognito JWT 入站(用 Spec 0 UP 引用)             ║  │
│       ║   ├─ X-Ray ON,LogGroup 7d                            ║  │
│       ║   ├─ NetworkConfiguration.using_vpc(...)              ║  │
│       ║   └─ ENI 部署到 jupiter-dev-vpc-db × 3 AZ             ║  │
│       ║       │ 出站 → NAT → ECR / Cognito / Secrets / CW    ║  │
│       ║       │ 出站 → RDS SG 3306(SG 互通)                  ║  │
│       ║       ▼                                              ║  │
│       ║  Secrets Manager:mcp-inspect/rds-mysql/devops-readonly║  │
│       ║   {"username":"mcp_devops_ro", "password":"..."}      ║  │
│       ║                                                       ║  │
│       ║  EC2 SecurityGroup × 1(sg-spec2-runtime)             ║  │
│       ║   └─ outbound: RDS SG 3306(对 sg ref 限定)           ║  │
│       ║                                                       ║  │
│       ║  EC2 SG ingress rule(在 RDS SG sg-09b483e0cb2e97f69  ║  │
│       ║  上,入站允许来自 Runtime SG 的 3306)                  ║  │
│       ╚═══════════════════════════════════════════════════════╝  │
│            │                                                     │
│            ▼ MySQL 协议(VPC 内私网,3306)                       │
│       jupiter-dev-slurm-db(MySQL 8.0.44,本 spec 不动)          │
│            └─ Runtime 用 mcp_devops_ro 只读账号查              │
│                information_schema.innodb_trx                     │
└─────────────────────────────────────────────────────────────────┘
```

### 三 Stack 边界与跨 Stack 引用关系

```
McpInspectSpec0Stack          McpInspectSpec1Stack          McpInspectSpec2Stack
(已部署,本 spec 不动)         (已部署,本 spec 不动)         (本 spec 新建)

Cognito M2M / UserPool        Gateway / Token Vault          Runtime / Target /
Runtime / Secret / 7 Output   2 Output                       Secret / SG / SG ingress

Spec 0 7 个 Output ──────────────→ 全部已带 export_name=  ─→ Fn.import_value × 4
                                  (Spec 1 Task 3 补好)        (CognitoClientId /
                                                                CognitoClientSecretArn /
                                                                CognitoOAuthScope /
                                                                CognitoTokenEndpoint)

Spec 0 UserPoolId(SSM 中转)── /mcp-inspect/spec0/cognito-user-pool-id ──→
                                  (Spec 1 bootstrap 已写) ─→ ssm.value_for_string_parameter
                                                                + cognito.UserPool.from_user_pool_id

Spec 1 GatewayId(SSM 中转,本 spec 新增)──/mcp-inspect/spec1/gateway-id ──→
                                  (本 spec bootstrap_spec2.sh 写)
                                                            ─→ ssm.value_for_string_parameter
                                                                + agentcore.Gateway.from_gateway_id

Spec 1 OAuth2CredentialProvider ARN(本 spec 用 SSM 中转或 Spec 1 补 export)──→
                                                            (待 design Task 1 决策)

Spec 2 Stack 内部新建:Runtime / Target / SG / Secret / LogGroup
```

### 部署期资源依赖

```
Fn.import_value × 4(Spec 0 export)
    │
    ├─→ Cognito UserPoolClient.from_user_pool_client_id ┐
    └─→ secretsmanager.Secret.from_secret_complete_arn  │
                                                         │
SSM × 2(spec0 user pool id + spec1 gateway id)         │
    │                                                    │
    ├─→ cognito.UserPool.from_user_pool_id              │
    └─→ agentcore.Gateway.from_gateway_id ───────────┐  │
                                                      │  │
新建:                                                 ▼  ▼
  EC2 SecurityGroup(Runtime SG)                Cognito 引用 + Spec 0 secret 引用
        │                                              │
        ▼                                              ▼
  EC2 SG ingress rule(RDS SG 加白来自 Runtime SG)    RuntimeAuthorizerConfig.using_cognito
                                                       │
                                                       ▼
  ec2.Vpc.from_lookup ─→ ec2.SubnetSelection ─→ RuntimeNetworkConfig.using_vpc
                                                       │
                                                       ▼
  Secrets Manager Secret(PLACEHOLDER 密码)─→ Runtime 执行角色 +secretsmanager:GetSecretValue
                                                       │
                                                       ▼
                                              AgentCore Runtime(VPC 模式)
                                                       │
                                                       ▼
                                              MCP Target(挂在 Spec 1 Gateway 上)
                                                       │
                                                       ▼
                                              CfnOutput × 5
```

## 项目目录结构(增量)

在 Spec 0/1 基础上,Spec 2 引入:

```
aws-devops-mcp-platform/
├─ .kiro/specs/spec-2-rds-mysql-inspect/   ← 本 spec 文档
│   ├─ requirements.md     ← 已落盘
│   ├─ design.md           ← 本文档
│   └─ tasks.md            ← 后续生成
│
├─ stacks/
│   ├─ spec_zero_stack.py             ← Spec 0(不动)
│   ├─ spec_one_stack.py              ← Spec 1(不动)
│   └─ spec_two_stack.py              ← 本 spec 新增(VPC 引用 + Runtime + SG + Target + Secret + 5 Output)
│
├─ framework/
│   ├─ runtime_construct.py           ← Spec 0 引入,本 spec 增量扩展(加 vpc / subnets / security_groups 3 个 Optional 入参)
│   ├─ gateway_construct.py           ← Spec 1 引入(不动)
│   └─ ...
│
├─ mcp_servers/
│   ├─ hello_world/                   ← Spec 0(不动)
│   └─ rds_mysql/                     ← 本 spec 新增
│       ├─ main.py                    ← FastMCP + rds_mysql_inspect_long_transactions tool
│       ├─ Dockerfile                 ← linux/arm64 + EXPOSE 8000(MCP 协议契约)
│       ├─ requirements.txt           ← mcp~=1.27 + pymysql>=1.1,<2.0 + boto3>=1.34
│       └─ .dockerignore              ← 沿用 Spec 0 模板
│
├─ scripts/
│   ├─ verify.py                      ← Spec 0(不动)
│   ├─ verify_spec1.py                ← Spec 1(不动)
│   ├─ verify_spec2.py                ← 本 spec 新增(端到端验证 RDS 巡检 tool)
│   ├─ destroy.sh                     ← Spec 0(不动)
│   ├─ destroy_spec1.sh               ← Spec 1(不动)
│   ├─ destroy_spec2.sh               ← 本 spec 新增(只清 Spec 2,RDS SG 加白规则反向)
│   ├─ bootstrap_spec1.sh             ← Spec 1(不动)
│   ├─ bootstrap_spec2.sh             ← 本 spec 新增(把 Spec 1 GatewayId 写 SSM)
│   └─ init_rds_user.sh               ← 本 spec 新增(王总 RDS admin session 一次性跑)
│
├─ app.py                              ← 修改(加 SpecTwoStack 实例化)
├─ README.md                           ← 增量补 ## 8 Spec 2 章节
└─ requirements.txt                    ← 不动(根 Python 依赖,Spec 2 容器依赖在 mcp_servers/rds_mysql/requirements.txt)
```

新增文件计数:7 个(spec_two_stack.py / 4 个 mcp_servers 文件 / verify / destroy / bootstrap / init_rds 共 9 个;含 design + tasks 是 11 个,符合 Constraints "≤ 12")。

## Components and Interfaces

### Component 1:`framework/runtime_construct.py` 增量扩展 — VPC 模式支持

Spec 0 引入的 `McpInspectRuntime` Construct 增加 3 个 Optional 入参,保持 alpha 隔离边界(SHALL NOT #2)。

实测签名(已归档 trace,Spec 2 起草前置事实 2/2):

```python
agentcore.RuntimeNetworkConfiguration.using_vpc(
    scope: Construct,
    *,
    vpc: ec2.IVpc,                                  # 必传
    allow_all_outbound: Optional[bool] = None,      # 默认 True
    security_groups: Optional[Sequence[ec2.ISecurityGroup]] = None,
    vpc_subnets: Optional[ec2.SubnetSelection] = None,
) -> RuntimeNetworkConfiguration
```

代码骨架(增量,只列改动):

```python
# framework/runtime_construct.py 内,McpInspectRuntime.__init__ 新增可选参数
def __init__(
    self,
    scope, construct_id, *,
    source_path,
    runtime_name,
    resource_server_id="mcp",
    scope_name="invoke",
    # ↓↓↓ Spec 2 新增 ↓↓↓
    vpc: ec2.IVpc | None = None,
    subnets: ec2.SubnetSelection | None = None,
    security_groups: list[ec2.ISecurityGroup] | None = None,
):
    ...
    # 已有 Cognito + Runtime 拼装代码不动
    
    # ↓↓↓ Spec 2 新增 ↓↓↓
    if vpc is not None:
        network_config = agentcore.RuntimeNetworkConfiguration.using_vpc(
            self, "RuntimeNetwork",
            vpc=vpc,
            vpc_subnets=subnets,
            security_groups=security_groups,
        )
    else:
        network_config = None  # L2 默认 = using_public_network()(Spec 0 行为不变)
    
    # Runtime 实例化时把 network_config 传进去(Spec 0 当前没传,保持 None 等价 public)
    runtime = agentcore.Runtime(
        self, "Runtime",
        ...,
        network_configuration=network_config,  # ← 新增 keyword
    )
```

**Spec 0 兼容性验证**:`network_configuration=None` 时 alpha L2 默认走 public network(实测 Spec 0 当前模板 NetworkMode = PUBLIC),Construct 升级后 Spec 0 cdk diff 应当无变化。Task 1 实施时 cdk diff 验证。

### Component 2:`stacks/spec_two_stack.py` — `SpecTwoStack`

```python
"""SpecTwoStack — Spec 2 部署单元(独立 Stack)。

职责:
- 通过 Fn.import_value 引用 Spec 0 4 个 Output(Cognito 4 项)
- 通过 SSM Parameter 中转引用 Spec 0 UserPoolId + Spec 1 GatewayId
- 通过 ec2.Vpc.from_lookup 引用 jupiter-dev VPC,选 3 个 db 私有子网
- 创建 Runtime SG + 给 jupiter-dev-slurm-db SG 加白来自 Runtime SG 的 3306
- 创建 Secrets Manager Secret(默认 PLACEHOLDER 密码,DBA 后填)
- 实例化 McpInspectRuntime(VPC 模式,新建容器镜像 = mcp_servers/rds_mysql/)
- 给 Runtime 执行角色加 secretsmanager:GetSecretValue 限定到 mcp-inspect/rds-mysql/*
- 在 Spec 1 Gateway 上挂第二个 MCP target(name=rdsmysql),绑定 Spec 1 OAuth Provider
- 5 个 CfnOutput(全带 export_name=,conventions A9)
"""

# 关键常量
_SPEC0_STACK_NAME = "McpInspectSpec0Stack"
_SPEC0_EXPORT_PREFIX = "McpInspectSpec0Stack-"  # Spec 1 Task 3 已建立
_SPEC2_EXPORT_PREFIX = "McpInspectSpec2Stack-"
_SSM_SPEC0_USER_POOL_ID = "/mcp-inspect/spec0/cognito-user-pool-id"
_SSM_SPEC1_GATEWAY_ID = "/mcp-inspect/spec1/gateway-id"  # 本 spec bootstrap 写入
_TARGET_RDS_VPC_ID = "vpc-0e4a299032154a1be"  # jupiter-dev,实测
_TARGET_RDS_SG_ID = "sg-09b483e0cb2e97f69"   # jupiter-dev-slurm-db SG,实测
_TARGET_RDS_SUBNET_IDS = [
    "subnet-0508babc46b955ace",  # us-east-1a
    "subnet-02b34ec2b4fae5edc",  # us-east-1b
    "subnet-06f76750dd1e3ff35",  # us-east-1c
]
_TARGET_RDS_ENDPOINT = "jupiter-dev-slurm-db.chdm2hflidwp.us-east-1.rds.amazonaws.com"
_TARGET_NAME = "rdsmysql"
_RUNTIME_NAME = "spec2_rds_mysql"
_DB_SECRET_NAME = "mcp-inspect/rds-mysql/devops-readonly"


class SpecTwoStack(cdk.Stack):
    def __init__(self, scope, construct_id, **kwargs):
        super().__init__(scope, construct_id, **kwargs)

        # 1) 跨 Stack 引用(Spec 0)
        client_id = Fn.import_value(f"{_SPEC0_EXPORT_PREFIX}CognitoClientId")
        client_secret_arn = Fn.import_value(f"{_SPEC0_EXPORT_PREFIX}CognitoClientSecretArn")
        oauth_scope = Fn.import_value(f"{_SPEC0_EXPORT_PREFIX}CognitoOAuthScope")
        token_endpoint = Fn.import_value(f"{_SPEC0_EXPORT_PREFIX}CognitoTokenEndpoint")

        # 2) SSM 中转
        user_pool_id = ssm.StringParameter.value_for_string_parameter(self, _SSM_SPEC0_USER_POOL_ID)
        gateway_id = ssm.StringParameter.value_for_string_parameter(self, _SSM_SPEC1_GATEWAY_ID)

        # 3) 转 stable CDK 类型
        user_pool = cognito.UserPool.from_user_pool_id(self, "ImportedUserPool", user_pool_id)
        user_pool_client = cognito.UserPoolClient.from_user_pool_client_id(
            self, "ImportedUserPoolClient", client_id,
        )
        client_secret = secretsmanager.Secret.from_secret_complete_arn(
            self, "ImportedClientSecret", client_secret_arn,
        )

        # 4) VPC 引用(jupiter-dev)
        vpc = ec2.Vpc.from_lookup(self, "JupiterDevVpc", vpc_id=_TARGET_RDS_VPC_ID)
        rds_subnets = ec2.SubnetSelection(subnets=[
            ec2.Subnet.from_subnet_id(self, f"DbSubnet{i}", sn)
            for i, sn in enumerate(_TARGET_RDS_SUBNET_IDS)
        ])
        rds_sg = ec2.SecurityGroup.from_security_group_id(
            self, "JupiterDevRdsSg", _TARGET_RDS_SG_ID, mutable=True,
        )

        # 5) Runtime SG(本 stack 新建)
        runtime_sg = ec2.SecurityGroup(
            self, "RuntimeSg",
            vpc=vpc,
            description="Spec 2 Runtime SG, outbound to jupiter-dev-slurm-db 3306",
            allow_all_outbound=False,
        )
        runtime_sg.add_egress_rule(
            ec2.Peer.security_group_id(_TARGET_RDS_SG_ID),
            ec2.Port.tcp(3306),
            "Outbound to jupiter-dev-slurm-db MySQL",
        )
        runtime_sg.add_egress_rule(
            ec2.Peer.any_ipv4(), ec2.Port.tcp(443),
            "Outbound to ECR / Cognito / SecretsManager / CloudWatch via NAT",
        )

        # 6) RDS SG 加白(从 RDS SG 一侧加 ingress)
        rds_sg.add_ingress_rule(
            ec2.Peer.security_group_id(runtime_sg.security_group_id),
            ec2.Port.tcp(3306),
            "Spec 2 Runtime → jupiter-dev-slurm-db",
        )

        # 7) Secrets Manager Secret(PLACEHOLDER,DBA 后填)
        db_secret = secretsmanager.Secret(
            self, "DbReadOnlyCredentials",
            secret_name=_DB_SECRET_NAME,
            description="MySQL mcp_devops_ro credentials, single secret multi DB (conventions A5)",
            removal_policy=RemovalPolicy.DESTROY,
            secret_object_value={
                "username": SecretValue.unsafe_plain_text("mcp_devops_ro"),
                "password": SecretValue.unsafe_plain_text("PLACEHOLDER_REPLACE_BY_DBA"),
            },
        )

        # 8) Runtime(VPC 模式 + 自定义环境变量注入 endpoint + secret name)
        runtime = McpInspectRuntime(
            self, "RdsMysqlRuntime",
            source_path=Path(__file__).parent.parent / "mcp_servers" / "rds_mysql",
            runtime_name=_RUNTIME_NAME,
            vpc=vpc,
            subnets=rds_subnets,
            security_groups=[runtime_sg],
            environment_variables={
                "DEFAULT_CLUSTER_ENDPOINT": _TARGET_RDS_ENDPOINT,
                "DB_SECRET_NAME": _DB_SECRET_NAME,
                "AWS_REGION": self.region,
            },
        )

        # 9) Runtime 执行角色 + secretsmanager:GetSecretValue
        db_secret.grant_read(runtime.execution_role)  # ★ 路径限定到 mcp-inspect/rds-mysql/*

        # 10) 在 Spec 1 Gateway 上挂第二个 target
        # 实测路径:agentcore.Gateway.from_gateway_id 拿 IGateway 引用,
        # 然后调 add_mcp_server_target;详细见 Task 1 探针确认
        # 关键:OAuth Provider ARN 需要从 Spec 1 跨 stack 拿(走 Fn.import_value 或 SSM 中转,Task 3 决策)
        # 完整代码见 framework Construct 增量(若需要)或 Stack 内直接调用

        # 11) 5 个 CfnOutput(全带 export_name=)
        cdk.CfnOutput(self, "RuntimeArn", value=runtime.runtime_arn,
                      export_name=f"{_SPEC2_EXPORT_PREFIX}RuntimeArn")
        cdk.CfnOutput(self, "RuntimeUrl", value=runtime.runtime_url,
                      export_name=f"{_SPEC2_EXPORT_PREFIX}RuntimeUrl")
        cdk.CfnOutput(self, "RuntimeSgId", value=runtime_sg.security_group_id,
                      export_name=f"{_SPEC2_EXPORT_PREFIX}RuntimeSgId")
        cdk.CfnOutput(self, "DbSecretArn", value=db_secret.secret_arn,
                      export_name=f"{_SPEC2_EXPORT_PREFIX}DbSecretArn")
        cdk.CfnOutput(self, "Region", value=self.region,
                      export_name=f"{_SPEC2_EXPORT_PREFIX}Region")
```

**关键实现要点**:

- `ec2.Vpc.from_lookup` 必须在 cdk synth 时连 AWS 拿 VPC 真实属性,需要 cdk context 缓存(deploy 期间 stack 第一次 synth 会写 `cdk.context.json`,后续从缓存读)
- `mutable=True` 让 `from_security_group_id` 返回的 SG 引用可以被 add_ingress_rule(否则只读引用,加规则不生效)
- `add_egress_rule` 限定到 RDS SG ID 而非 RDS endpoint IP(SG 引用,RDS IP 变了规则仍生效)
- `db_secret.grant_read(...)` 自动给 Runtime 执行角色加 IAM Policy,资源段 = 该 secret ARN(满足 Requirement 7.1 的 IAM 路径限权)
- `runtime.execution_role` 这个属性需要 Spec 0 Construct 暴露(Task 1 同时增量补 Construct 暴露 `execution_role: iam.IRole`,与 vpc / subnets / security_groups 一并加)


### Component 3:`mcp_servers/rds_mysql/` 容器

第一个真实业务 MCP server。FastMCP + 1 个语义化巡检 tool,严格遵守 SHALL NOT #3(无通用 SQL 执行)。

#### `mcp_servers/rds_mysql/main.py` 代码骨架

```python
"""Spec 2 RDS MySQL 巡检 MCP server。

只暴露 1 个语义化 tool:
  rds_mysql_inspect_long_transactions(cluster_endpoint, threshold_seconds, database)

凭据 / 端点注入(Spec 2 Stack 在 Runtime 上设置环境变量):
  DEFAULT_CLUSTER_ENDPOINT  默认 RDS endpoint(jupiter-dev-slurm-db,Spec 2 默认值)
  DB_SECRET_NAME           Secrets Manager 路径(mcp-inspect/rds-mysql/devops-readonly)
  AWS_REGION               us-east-1

不允许 / 不实现:
  - 通用 SQL 执行(SHALL NOT #3)
  - 写操作 / DDL(本 spec SHALL NOT #9)
  - 缓存 password 到全局(本 spec SHALL NOT #7,每次 tool call 拉一次 secret)
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Any

import boto3
import pymysql
from mcp.server.fastmcp import FastMCP
from starlette.responses import PlainTextResponse

# 模块级常量
_DEFAULT_THRESHOLD_SECONDS = 60
_CRITICAL_MULTIPLIER = 5
_QUERY_LIMIT = 100
_CONNECT_TIMEOUT_SECONDS = 5
_LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(level=_LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("rds_mysql_inspect")


# 容器启动时拉一次 region(没必要每次 tool call 重读 env)
_AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
_DB_SECRET_NAME = os.environ.get("DB_SECRET_NAME", "mcp-inspect/rds-mysql/devops-readonly")
_DEFAULT_CLUSTER_ENDPOINT = os.environ.get("DEFAULT_CLUSTER_ENDPOINT", "")

mcp = FastMCP(host="0.0.0.0", port=8000, stateless_http=True)


@mcp.custom_route("/ping", methods=["GET"])
async def ping(request):  # noqa: ARG001 -- starlette signature requirement
    return PlainTextResponse("ok")


def _fetch_db_credentials() -> tuple[str, str]:
    """每次 tool call 都现拉,不做容器层缓存(本 spec SHALL NOT #7)。

    密码轮换可即时生效;Cognito access_token / Token Vault 已经管了 token 缓存,
    我们这里只管 DB 凭据,轻量场景每次拉的开销 < 100ms,可忽略。
    """
    sm = boto3.client("secretsmanager", region_name=_AWS_REGION)
    raw = sm.get_secret_value(SecretId=_DB_SECRET_NAME)["SecretString"]
    payload = json.loads(raw)
    user = payload["username"]
    pwd = payload["password"]
    if pwd == "PLACEHOLDER_REPLACE_BY_DBA":
        raise RuntimeError(
            f"Secret {_DB_SECRET_NAME!r} 中 password 仍是 PLACEHOLDER,"
            "请先跑 scripts/init_rds_user.sh(王总 RDS admin session 一次性脚本)"
            "把真实密码灌入 Secret"
        )
    return user, pwd


def _classify_status(transactions: list[dict[str, Any]], threshold: int) -> str:
    if not transactions:
        return "ok"
    max_dur = max(int(t["duration_seconds"]) for t in transactions)
    if max_dur >= threshold * _CRITICAL_MULTIPLIER:
        return "critical"
    return "warning"


def _build_findings(transactions: list[dict[str, Any]], threshold: int) -> list[dict[str, Any]]:
    findings = []
    for trx in transactions:
        dur = int(trx["duration_seconds"])
        sev = "critical" if dur >= threshold * _CRITICAL_MULTIPLIER else "warning"
        findings.append({
            "severity": sev,
            "metric": "long_running_trx",
            "value": f"{dur}s",
            "threshold": f"{threshold}s",
            "trx_id": str(trx["trx_id"]),
            "thread_id": int(trx["trx_mysql_thread_id"]) if trx.get("trx_mysql_thread_id") else None,
        })
    return findings


def _build_recommendation(status: str, transactions: list[dict[str, Any]]) -> str:
    if status == "ok":
        return "无长事务,数据库事务健康。"
    n = len(transactions)
    max_dur = max(int(t["duration_seconds"]) for t in transactions)
    if status == "critical":
        return (
            f"发现 {n} 条长事务,最长 {max_dur}s,达 critical 阈值。"
            "建议:1) 立即检查应用层是否有未提交 / 未回滚事务;"
            "2) 用 thread_id 在 information_schema.processlist 查具体连接;"
            "3) 必要时手动 KILL(需要 RDS admin 权限,本 tool 不提供)。"
        )
    return (
        f"发现 {n} 条长事务,最长 {max_dur}s。"
        "建议:1) 排查应用层事务边界;2) 留意是否有未关闭的连接池连接 leak。"
    )


@mcp.tool()
def rds_mysql_inspect_long_transactions(
    cluster_endpoint: str = "",
    threshold_seconds: int = _DEFAULT_THRESHOLD_SECONDS,
    database: str = "mysql",
) -> dict[str, Any]:
    """巡检 MySQL 长事务。

    Args:
        cluster_endpoint: RDS endpoint。空字符串时用容器默认值
                          (DEFAULT_CLUSTER_ENDPOINT 环境变量,Spec 2 = jupiter-dev-slurm-db)。
        threshold_seconds: 长事务阈值,默认 60。
        database: 建连接时用的 schema,默认 mysql(系统库,巡检不依赖业务库)。

    Returns:
        conventions A8 标准结构 dict。
    """
    endpoint = cluster_endpoint.strip() or _DEFAULT_CLUSTER_ENDPOINT
    if not endpoint:
        raise RuntimeError(
            "cluster_endpoint 为空,且容器无 DEFAULT_CLUSTER_ENDPOINT 环境变量"
        )
    if threshold_seconds < 1:
        raise ValueError(f"threshold_seconds 必须 >= 1,收到 {threshold_seconds}")

    user, pwd = _fetch_db_credentials()
    log.info("connecting to %s as %s db=%s threshold=%ds",
             endpoint, user, database, threshold_seconds)

    conn = pymysql.connect(
        host=endpoint,
        port=3306,
        user=user,
        password=pwd,
        database=database,
        connect_timeout=_CONNECT_TIMEOUT_SECONDS,
        cursorclass=pymysql.cursors.DictCursor,
        # ssl 字段不传 — RDS 默认开 TLS,pymysql 自动协商;若强制 TLS 传 ssl_disabled=False
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT trx_id, trx_state, trx_started,
                       TIMESTAMPDIFF(SECOND, trx_started, NOW()) AS duration_seconds,
                       trx_mysql_thread_id, trx_query,
                       trx_rows_modified, trx_rows_locked
                FROM information_schema.innodb_trx
                WHERE TIMESTAMPDIFF(SECOND, trx_started, NOW()) >= %s
                ORDER BY duration_seconds DESC
                LIMIT %s
                """,
                (threshold_seconds, _QUERY_LIMIT),
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    # datetime / Decimal / bytes 转 JSON 安全形态
    transactions = []
    for r in rows:
        transactions.append({
            "trx_id": str(r["trx_id"]),
            "trx_state": str(r["trx_state"]),
            "trx_started": r["trx_started"].isoformat() if r["trx_started"] else None,
            "duration_seconds": int(r["duration_seconds"]),
            "trx_mysql_thread_id": int(r["trx_mysql_thread_id"]) if r["trx_mysql_thread_id"] is not None else None,
            "trx_query": r["trx_query"] if r["trx_query"] else None,
            "trx_rows_modified": int(r["trx_rows_modified"] or 0),
            "trx_rows_locked": int(r["trx_rows_locked"] or 0),
        })

    status = _classify_status(transactions, threshold_seconds)
    return {
        "status": status,
        "findings": _build_findings(transactions, threshold_seconds),
        "raw_data": {
            "cluster_endpoint": endpoint,
            "database": database,
            "threshold_seconds": threshold_seconds,
            "queried_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "transactions": transactions,
        },
        "recommendation": _build_recommendation(status, transactions),
    }


if __name__ == "__main__":
    log.info("starting Spec 2 RDS MySQL inspect MCP server on :8000")
    mcp.run(transport="streamable-http")
```

#### `mcp_servers/rds_mysql/Dockerfile`

```dockerfile
# 第一行强制 linux/arm64(SHALL NOT #7)
FROM --platform=linux/arm64 public.ecr.aws/docker/library/python:3.13-slim

WORKDIR /app

# 用 cache mount 加速构建(buildkit 默认开启)
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py /app/

# AgentCore Runtime MCP 协议契约:容器 port 8000,mount /mcp(conventions 协议表)
EXPOSE 8000

# 不用 CMD ["python", "main.py"] 直接 exec,确保 SIGTERM 能直接到 Python 进程
ENTRYPOINT ["python", "main.py"]
```

#### `mcp_servers/rds_mysql/requirements.txt`

```
mcp~=1.27
pymysql>=1.1,<2.0
boto3>=1.34
```

> **不引入 `mysqlclient` / `mysql-connector-python`**:前者需要 C 扩展 + libmariadb-dev 系统库,arm64 容器构建时间翻倍;后者体积大且许可证 GPL 复杂度高(本 spec SHALL NOT #6)。`pymysql` 纯 Python,arm64 直接装 wheel,< 5 秒。

#### `mcp_servers/rds_mysql/.dockerignore`

沿用 Spec 0 hello_world 同款模板:

```
.env
.env.*
.aws/
__pycache__/
*.pyc
.git/
.kiro/
docs/
.venv/
cdk.out/
```

#### MCP server 容器与 Spec 0 hello_world 的差异

| 维度 | Spec 0 hello_world | Spec 2 rds_mysql |
|------|-------------------|------------------|
| Tool 数 | 1(hello_world)| 1(inspect_long_transactions)|
| 出站依赖 | 无 | Secrets Manager(每次 call)+ RDS MySQL 3306 |
| 凭据 | 无 | Secrets Manager `mcp-inspect/rds-mysql/devops-readonly`(JSON)|
| 网络 | Public Network | VPC 私网 + 出 NAT |
| 业务逻辑 | `f"Hello, {name}!"` | DB 连接 + SQL + 结构化分类 |
| 镜像大小 | ~80 MB(slim + mcp)| ~95 MB(+ pymysql + boto3 增 ~15 MB)|
| 冷启延迟预期 | < 1s | < 2s(boto3 import 慢一点)|


### Component 4:`scripts/verify_spec2.py` 端到端验证脚本

沿用 Spec 1 verify_spec1.py 的骨架(SSE utf-8 修复内置,SHALL NOT #18),增量加 RDS 巡检 tool 调用与结构断言。

```python
"""Spec 2 端到端验证脚本。

链路:
  CFN outputs(3 个 stack)→ Spec 0 client_secret(SecretsManager)→
  Cognito client_credentials(scope=mcp/invoke)→ Spec 1 GatewayUrl initialize →
  tools/list 断言含 hello + rds tool → tools/call rdsmysql___rds_mysql_inspect_long_transactions

断言重点:
  - Property 4(沿用 Spec 1):tool 命名空间 + Cognito JWT 链路完整
  - Property 9 (本 spec 新增):Spec 2 tool 返回 conventions A8 标准结构
  - Property 10(本 spec 新增):secret password 不是 PLACEHOLDER

强约束:
  SHALL NOT #15 完整 traceback / SHALL NOT #17 不打印 token / 本 spec SHALL NOT #10 不打印 raw query SQL
"""

from __future__ import annotations

import json
import sys
import traceback
from typing import Any

import boto3
import requests

# 模块级常量
_SPEC0_STACK_NAME = "McpInspectSpec0Stack"
_SPEC1_STACK_NAME = "McpInspectSpec1Stack"
_SPEC2_STACK_NAME = "McpInspectSpec2Stack"
_REGION = "us-east-1"
_TIMEOUT_HTTP = 30  # RDS 巡检比 hello_world 慢一点(连 DB + 查 + 反序列化),给充裕预算
_MCP_PROTOCOL_VERSION = "2025-03-26"
_DB_SECRET_NAME = "mcp-inspect/rds-mysql/devops-readonly"

# 期望 Gateway 暴露的 tool 名(三下划线分隔符,SHALL NOT #21)
_HELLO_TOOL = "spec0helloworld___hello_world"
_RDS_TOOL = "rdsmysql___rds_mysql_inspect_long_transactions"


def _read_outputs(stack_name: str) -> dict[str, str]:
    cf = boto3.client("cloudformation", region_name=_REGION)
    stacks = cf.describe_stacks(StackName=stack_name)["Stacks"]
    return {o["OutputKey"]: o["OutputValue"] for o in stacks[0]["Outputs"]}


def _check_db_secret_not_placeholder() -> None:
    """前置校验:DB Secret 不是 PLACEHOLDER(本 spec Requirement 6.2)。"""
    sm = boto3.client("secretsmanager", region_name=_REGION)
    raw = sm.get_secret_value(SecretId=_DB_SECRET_NAME)["SecretString"]
    payload = json.loads(raw)
    if payload.get("password") == "PLACEHOLDER_REPLACE_BY_DBA":
        raise RuntimeError(
            f"Secret {_DB_SECRET_NAME!r} 中 password 仍是 PLACEHOLDER。"
            "请先跑 bash scripts/init_rds_user.sh(王总在 RDS admin session)"
            "灌入真实密码,然后重跑本脚本。"
        )
    print(f"✓ DB Secret 已就位(username={payload.get('username')!r}, password length={len(payload.get('password', ''))})")


def _fetch_access_token(*, token_endpoint, client_id, client_secret, scope) -> str:
    resp = requests.post(
        token_endpoint,
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": scope,
        },
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _post_mcp(url: str, payload: dict[str, Any], token: str) -> dict[str, Any]:
    """SSE / JSON 双路径,utf-8 强制(沿用 Spec 1 verify_spec1.py 同款)。"""
    resp = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        json=payload,
        timeout=_TIMEOUT_HTTP,
        stream=True,
    )
    resp.raise_for_status()
    ctype = resp.headers.get("Content-Type", "").lower()
    if "text/event-stream" in ctype:
        for raw in resp.iter_lines(decode_unicode=False):
            if not raw:
                continue
            line = raw.decode("utf-8").strip()
            if line.startswith("data:"):
                return json.loads(line[len("data:"):].strip())
        raise RuntimeError(f"SSE 响应缺 data 帧:{resp.content!r}")
    if "application/json" in ctype:
        return json.loads(resp.content.decode("utf-8"))
    raise RuntimeError(f"Unexpected Content-Type {ctype!r}; raw={resp.text!r}")


def _extract_result(rpc: dict[str, Any], step: str) -> dict[str, Any]:
    if "error" in rpc:
        raise RuntimeError(f"MCP {step} JSON-RPC error: {rpc['error']!r}")
    if "result" not in rpc:
        raise RuntimeError(f"MCP {step} 缺 result: {rpc!r}")
    return rpc["result"]


def _assert_a8_structure(payload: dict[str, Any]) -> None:
    """Property 9:本 spec 新增,断言 tool 返回符合 conventions A8。"""
    required = {"status", "findings", "raw_data", "recommendation"}
    missing = required - set(payload.keys())
    assert not missing, f"A8 标准缺字段:{missing},payload keys = {list(payload.keys())}"
    assert payload["status"] in ("ok", "warning", "critical"), \
        f"status 必须 ok/warning/critical,实际 {payload['status']!r}"
    assert isinstance(payload["findings"], list), "findings 必须是 list"
    assert isinstance(payload["raw_data"], dict), "raw_data 必须是 dict"
    assert isinstance(payload["recommendation"], str) and payload["recommendation"], \
        "recommendation 必须是非空 string"


def main() -> None:
    # 1) 三个 stack 的 Output
    spec0 = _read_outputs(_SPEC0_STACK_NAME)
    print(f"✓ {_SPEC0_STACK_NAME} outputs = {sorted(spec0)}")
    spec1 = _read_outputs(_SPEC1_STACK_NAME)
    print(f"✓ {_SPEC1_STACK_NAME} outputs = {sorted(spec1)}")
    spec2 = _read_outputs(_SPEC2_STACK_NAME)
    print(f"✓ {_SPEC2_STACK_NAME} outputs = {sorted(spec2)}")

    # 2) DB Secret 前置校验(Property 10)
    _check_db_secret_not_placeholder()

    # 3) Spec 0 client_secret + Cognito access_token(链路认证起点,Property 1)
    sm = boto3.client("secretsmanager", region_name=_REGION)
    cognito_secret = sm.get_secret_value(SecretId=spec0["CognitoClientSecretArn"])["SecretString"]
    print(f"✓ Spec 0 Cognito client_secret fetched (length={len(cognito_secret)})")

    token = _fetch_access_token(
        token_endpoint=spec0["CognitoTokenEndpoint"],
        client_id=spec0["CognitoClientId"],
        client_secret=cognito_secret,
        scope=spec0["CognitoOAuthScope"],
    )
    print(f"✓ Cognito access_token acquired (length={len(token)})")

    gateway_url = spec1["GatewayUrl"]

    # 4) initialize
    init = _extract_result(_post_mcp(gateway_url, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": _MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "spec2-verify", "version": "0.1.0"},
        },
    }, token), "initialize")
    print(f"✓ Gateway initialize succeeded (server protocolVersion={init.get('protocolVersion')!r})")

    # 5) tools/list — 断言 2 个 tool 都在(Property 4)
    listed = _extract_result(_post_mcp(gateway_url, {
        "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {},
    }, token), "tools/list")
    tool_names = sorted(t["name"] for t in listed.get("tools", []))
    print(f"✓ tools/list returned {len(tool_names)} tool(s): {tool_names}")
    for expected in (_HELLO_TOOL, _RDS_TOOL):
        assert expected in tool_names, f"期望 tool {expected!r} 不在列表 {tool_names!r}"

    # 6) tools/call rdsmysql___rds_mysql_inspect_long_transactions
    call = _extract_result(_post_mcp(gateway_url, {
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {
            "name": _RDS_TOOL,
            "arguments": {"threshold_seconds": 60},
            # cluster_endpoint 不传,用容器默认值(jupiter-dev-slurm-db)
        },
    }, token), "tools/call")

    # MCP tools/call 把 tool 返回值封装在 content[0].text(JSON 字符串)里
    contents = call.get("content", [])
    assert contents and contents[0].get("type") == "text", \
        f"tools/call 结果异常:{call!r}"
    payload = json.loads(contents[0]["text"])

    # 7) 断言符合 conventions A8(Property 9)
    _assert_a8_structure(payload)

    # 8) 摘要打印 — 严禁打印 raw query SQL(本 spec SHALL NOT #10)
    txns = payload["raw_data"].get("transactions", [])
    max_dur = max((t["duration_seconds"] for t in txns), default=0)
    print(
        f"✓ tool returned status={payload['status']!r}, "
        f"transactions={len(txns)}, max_duration={max_dur}s, "
        f"recommendation_len={len(payload['recommendation'])}"
    )

    print("✅ Spec 2 verification passed")
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
```

### Component 5:`scripts/destroy_spec2.sh` 与 `scripts/bootstrap_spec2.sh` / `scripts/init_rds_user.sh`

#### `scripts/bootstrap_spec2.sh`

把 Spec 1 GatewayId 写到 SSM(本 spec deploy 前置)。沿用 `scripts/bootstrap_spec1.sh` 同款结构。

```bash
#!/usr/bin/env bash
# Spec 2 boot:把 Spec 1 GatewayId 写到 SSM,供 SpecTwoStack 跨 stack 引用。
# 沿用 bootstrap_spec1.sh 模式 + Spec 1 Task 2 trace 经验。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

REGION="us-east-1"
SPEC1_STACK_NAME="McpInspectSpec1Stack"
SSM_PARAM_NAME="/mcp-inspect/spec1/gateway-id"

echo "==> [1/2] 验证 Spec 1 Stack 存在"
if ! STATUS=$(aws cloudformation describe-stacks --stack-name "${SPEC1_STACK_NAME}" \
    --region "${REGION}" --no-cli-pager --query 'Stacks[0].StackStatus' --output text); then
  echo >&2 "❌ Spec 1 stack 不存在,先 bash scripts/bootstrap_spec1.sh + cdk deploy McpInspectSpec1Stack"
  exit 2
fi
case "${STATUS}" in
  CREATE_COMPLETE|UPDATE_COMPLETE) ;;
  *) echo >&2 "❌ Spec 1 stack 状态 ${STATUS},非完成态"; exit 3 ;;
esac

echo "==> [2/2] 拉 Spec 1 GatewayId 写到 SSM ${SSM_PARAM_NAME}"
GATEWAY_ID=$(aws cloudformation describe-stacks --stack-name "${SPEC1_STACK_NAME}" \
  --region "${REGION}" --no-cli-pager \
  --query "Stacks[0].Outputs[?OutputKey=='GatewayId'].OutputValue" --output text)
[[ -n "${GATEWAY_ID}" ]] || { echo >&2 "❌ Spec 1 Output GatewayId 为空"; exit 4; }

aws ssm put-parameter \
  --name "${SSM_PARAM_NAME}" --value "${GATEWAY_ID}" \
  --type String --overwrite --region "${REGION}" --no-cli-pager >/dev/null

echo "✓ SSM ${SSM_PARAM_NAME} = ${GATEWAY_ID}"
echo "✅ Spec 2 boot 完成"
```

#### `scripts/init_rds_user.sh`(王总在 RDS admin session 跑)

```bash
#!/usr/bin/env bash
# Spec 2 一次性脚本(王总 RDS admin session 跑,不进 cdk)。
# 1) 生成 32 字符随机密码
# 2) 灌进 Secrets Manager mcp-inspect/rds-mysql/devops-readonly
# 3) 输出 SQL,王总 copy 到 RDS admin session 执行(创建 mcp_devops_ro user)
# 不打印 password 到 stdout 任何地方;只把 SQL 输出到一个临时文件,提示王总执行后 shred 删除。
#
# 对应 Spec 2 SHALL NOT #11(cdk 不管 RDS admin)+ #12(secret 不在 cdk 模板)
set -euo pipefail

REGION="us-east-1"
SECRET_NAME="mcp-inspect/rds-mysql/devops-readonly"
DB_USER="mcp_devops_ro"

# 1) 强随机密码(排除 SQL 注入与 JSON 转义麻烦字符)
PASSWORD=$(aws secretsmanager get-random-password \
  --password-length 32 --exclude-characters '"@/\\`' \
  --region "${REGION}" --query RandomPassword --output text)

# 2) 灌进 Secrets Manager
aws secretsmanager put-secret-value \
  --secret-id "${SECRET_NAME}" \
  --secret-string "{\"username\":\"${DB_USER}\",\"password\":\"${PASSWORD}\"}" \
  --region "${REGION}" --no-cli-pager >/dev/null
echo "✓ Secret ${SECRET_NAME} 已更新(username=${DB_USER}, password length=32)"

# 3) 把 SQL 写到 /tmp(只 600 权限),提示王总跑完后 shred 删
SQL_FILE="/tmp/spec2_init_rds_user.sql"
umask 077
cat >"${SQL_FILE}" <<EOF
-- 在 jupiter-dev-slurm-db 执行(用 admin 凭据;堡垒机或 SSM Session Manager)
-- 执行后 shred -u ${SQL_FILE}

CREATE USER IF NOT EXISTS '${DB_USER}'@'%' IDENTIFIED BY '${PASSWORD}';
GRANT SELECT, PROCESS ON *.* TO '${DB_USER}'@'%';
GRANT SELECT ON information_schema.* TO '${DB_USER}'@'%';
GRANT SELECT ON performance_schema.* TO '${DB_USER}'@'%';
ALTER USER '${DB_USER}'@'%' IDENTIFIED BY '${PASSWORD}';
FLUSH PRIVILEGES;
EOF
echo "✓ SQL 已写到 ${SQL_FILE}(0600 权限)"
echo ""
echo "下一步:"
echo "  1) 用 RDS admin 凭据登 jupiter-dev-slurm-db,执行 ${SQL_FILE} 中的 SQL"
echo "  2) 执行成功后跑 shred -u ${SQL_FILE}(删除明文密码文件)"
echo "  3) 回 cdk 仓库根跑 .venv/bin/python scripts/verify_spec2.py 端到端验证"
```

#### `scripts/destroy_spec2.sh`

```bash
#!/usr/bin/env bash
# Spec 2 清理脚本(只清 Spec 2,SHALL NOT #16 不连带 Spec 0/1)。
# 包含 RDS SG 加白规则的反向清理验证(本 spec Requirement 9.5)。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

STACK_NAME="McpInspectSpec2Stack"
REGION="us-east-1"
RUNTIME_NAME="spec2_rds_mysql"
TARGET_NAME="rdsmysql"
RDS_SG_ID="sg-09b483e0cb2e97f69"  # jupiter-dev-slurm-db SG
DB_SECRET_NAME="mcp-inspect/rds-mysql/devops-readonly"

if [[ -d "$REPO_ROOT/.venv/bin" ]]; then
  export PATH="$REPO_ROOT/.venv/bin:$PATH"
fi

echo "==> [1/5] cdk destroy ${STACK_NAME}"
npx --yes cdk@2.1122.0 destroy --force "${STACK_NAME}"

echo "==> [2/5] 检查无 Runtime 残留(name=${RUNTIME_NAME})"
RT=$(aws bedrock-agentcore-control list-agent-runtimes --region "${REGION}" --no-cli-pager \
       --query "agentRuntimes[?agentRuntimeName==\`${RUNTIME_NAME}\`]")
[[ "${RT}" == "[]" ]] || { echo "❌ Runtime 残留:${RT}"; exit 1; }
echo "✓ 无 Runtime 残留"

echo "==> [3/5] 检查无 Spec 1 Gateway 上的 ${TARGET_NAME} target 残留"
SPEC1_GW=$(aws cloudformation describe-stacks --stack-name McpInspectSpec1Stack --region "${REGION}" \
  --query "Stacks[0].Outputs[?OutputKey=='GatewayId'].OutputValue" --output text 2>/dev/null || echo "")
if [[ -n "${SPEC1_GW}" && "${SPEC1_GW}" != "None" ]]; then
  TGTS=$(aws bedrock-agentcore-control list-gateway-targets \
           --gateway-identifier "${SPEC1_GW}" --region "${REGION}" --no-cli-pager \
           --query "items[?name==\`${TARGET_NAME}\`]")
  [[ "${TGTS}" == "[]" ]] || { echo "❌ Target 残留:${TGTS}"; exit 1; }
  echo "✓ 无 Target 残留"
else
  echo "(Spec 1 stack 不存在,跳过 target 残留扫描)"
fi

echo "==> [4/5] 检查 jupiter-dev-slurm-db SG ${RDS_SG_ID} 无 Spec 2 Runtime SG ingress 残留"
INGRESS=$(aws ec2 describe-security-groups --group-ids "${RDS_SG_ID}" --region "${REGION}" \
            --query "SecurityGroups[0].IpPermissions[?FromPort==\`3306\` && contains(@.UserIdGroupPairs[].Description, \`Spec 2\`)]")
[[ "${INGRESS}" == "[]" ]] || { echo "❌ RDS SG 残留 Spec 2 ingress 规则:${INGRESS}"; exit 1; }
echo "✓ jupiter-dev-slurm-db SG 无 Spec 2 ingress 残留"

echo "==> [5/5] 检查 Secret ${DB_SECRET_NAME} 已删除"
SEC=$(aws secretsmanager list-secrets --region "${REGION}" --no-cli-pager \
        --query "SecretList[?Name==\`${DB_SECRET_NAME}\`]")
[[ "${SEC}" == "[]" ]] || { echo "❌ Secret 残留(可能在 7 天回收期内,如确认要立刻删用 force-delete-without-recovery):${SEC}"; }

echo "✅ Spec 2 清理完成,Spec 0/1 资源未受影响"
```

> **Secret 删除特殊性**:Secrets Manager 默认有 7-30 天 recovery 窗口,即便 `RemovalPolicy.DESTROY` cdk destroy 后 secret 在 list-secrets 仍可能可见(状态 `PendingDeletion`)。Step [5/5] 不强制 fail,只警告 — 这与 SHALL NOT #14 不冲突(cdk 已发起删除,7 天后真删)。如需立即清理,Construct 内可用 `secretsmanager.Secret(removal_policy=DESTROY, replica_regions=[])` + cdk hook 显式 `force-delete-without-recovery`,但本 Spec 2 不强求(Out of Scope 之外)。


## Data Models

Spec 2 不引入持久化数据。运行时数据流转:

| 数据 | 来源 | 去向 | 形态 |
|------|------|------|------|
| Cognito access_token(入站) | client → Spec 0 Cognito | Spec 1 Gateway 入站校验 | JWT |
| Cognito access_token(出站) | Spec 1 Token Vault → Cognito | Gateway → Spec 2 Runtime 的 Authorization | JWT(Token Vault 自动) |
| MySQL `mcp_devops_ro` 凭据 | Secrets Manager `mcp-inspect/rds-mysql/devops-readonly` | Spec 2 Runtime 容器(每 tool call 现拉) | JSON `{"username":"mcp_devops_ro","password":"..."}` |
| RDS endpoint | Spec 2 Stack 注入到 Runtime 环境变量 `DEFAULT_CLUSTER_ENDPOINT` | tool 默认 cluster_endpoint | str(`jupiter-dev-slurm-db.chdm2hflidwp.us-east-1.rds.amazonaws.com`) |
| 长事务记录 | `information_schema.innodb_trx`(MySQL) | tool 序列化为 JSON 返回 | dict(conventions A8 标准) |
| MCP wire format | Gateway ↔ Runtime ↔ verify | streamable-http SSE | JSON-RPC 2.0 |

### Tool 命名空间(沿用 Spec 1 实测)

Gateway 加 `{target_name}___` 三下划线前缀(SHALL NOT #21)。本 spec target=`rdsmysql`(9 字符),tool=`rds_mysql_inspect_long_transactions`(35 字符),全名 `rdsmysql___rds_mysql_inspect_long_transactions`(46 字符 < 64 ✓ conventions A7)。

### Tool 返回结构(conventions A8)

```json
{
  "status": "warning",
  "findings": [
    {
      "severity": "warning",
      "metric": "long_running_trx",
      "value": "127s",
      "threshold": "60s",
      "trx_id": "421594821093312",
      "thread_id": 8421
    }
  ],
  "raw_data": {
    "cluster_endpoint": "jupiter-dev-slurm-db.chdm2hflidwp.us-east-1.rds.amazonaws.com",
    "database": "mysql",
    "threshold_seconds": 60,
    "queried_at": "2026-05-19T18:42:11Z",
    "transactions": [
      {
        "trx_id": "421594821093312",
        "trx_state": "RUNNING",
        "trx_started": "2026-05-19T18:40:04",
        "duration_seconds": 127,
        "trx_mysql_thread_id": 8421,
        "trx_query": "SELECT * FROM ...(可能含业务 SQL,verify 不打印)",
        "trx_rows_modified": 0,
        "trx_rows_locked": 0
      }
    ]
  },
  "recommendation": "发现 1 条长事务,最长 127s。建议:1) 排查应用层事务边界;2) 留意是否有未关闭的连接池连接 leak。"
}
```

## Error Handling

| 失败场景 | 表现 | 诊断信号 |
|---------|------|---------|
| `cdk synth` 失败,`agentcore.RuntimeNetworkConfiguration.using_vpc` 签名不匹配 | jsii TypeError | 以 `.venv/.../aws_bedrock_agentcore_alpha/__init__.py` 实测签名为准修 framework Construct(SHALL NOT #19) |
| `cdk deploy` 报 ec2.Vpc.from_lookup 拿不到 VPC | "VPC vpc-... not found in account" | (a) 检查 cdk.context.json 是否需清理重建;(b) 验证 cdk app 的 env=us-east-1 + account 与 VPC 所在 account / region 一致 |
| Runtime CREATE_FAILED `Subnet not found` | CFN events 提示子网无效 | 重新跑 `aws ec2 describe-subnets` 确认 3 个 db 子网仍属于 jupiter-dev VPC,IDs 与 design 一致 |
| Runtime CREATE_FAILED `unable to assume execution role` | IAM 信任策略 / 权限错 | `aws iam get-role --role-name <McpInspectSpec2Stack-...>` 看角色;通常 L2 自动建,但 VPC 模式可能需要补 `ec2:CreateNetworkInterface` 等(L2 应自动加,实测确认) |
| MCP Target CREATE_FAILED `endpoint not reachable` | Gateway 无法发现新 Runtime | (a) Spec 2 Runtime 状态 `aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id <id>`(b) Spec 1 OAuth Provider 是否仍可用 |
| verify_spec2 失败 — DB Secret PLACEHOLDER | `_check_db_secret_not_placeholder` raise RuntimeError | 跑 `bash scripts/init_rds_user.sh`(王总 RDS admin session)灌入真实密码,然后跑 RDS admin SQL 创建 user |
| verify_spec2 失败 — Cognito 401 | tool/list 拒绝 | 解码 access_token 看 scope claim 是否含 `mcp/invoke`;走 Spec 1 README 7.5.3 排查 |
| verify_spec2 失败 — `tools/list` 不含 rds tool | Gateway target 未 ready / Runtime 不可达 / Provider 异常 | (a) `aws bedrock-agentcore-control list-gateway-targets --gateway-identifier <Spec1 GW>` 看 status;(b) Runtime 状态;(c) Spec 1 Provider 状态 |
| verify_spec2 失败 — `tools/call` 出站 401 / 网络 timeout | Runtime 容器到 RDS / Secrets Manager / Cognito 网络问题 | (a) 容器日志 `aws logs tail /aws/bedrock-agentcore/runtimes/spec2_rds_mysql --since 10m`;(b) 验 RDS SG 加白规则 `aws ec2 describe-security-groups --group-ids sg-09b483e0cb2e97f69` 看是否有 Runtime SG 入站规则;(c) 验 NAT 出口 `aws ec2 describe-nat-gateways --filter Name=vpc-id,Values=vpc-0e4a299032154a1be` |
| tool 返回 MySQL OperationalError(2003) | 网络不通到 RDS | 看 Runtime SG outbound 规则 + RDS SG inbound 规则双向是否互通;`aws logs ...` 看 pymysql 错误 |
| tool 返回 OperationalError(1045 Access denied) | DB user 凭据错 / DB user 未创建 | (a) `aws secretsmanager get-secret-value` 看密码是否变(可能 DBA 改过 DB 密码但忘记同步 Secret);(b) RDS admin session 跑 `SHOW GRANTS FOR 'mcp_devops_ro'@'%';` 验权限 |
| tool 返回 OperationalError(1142 PROCESS denied for innodb_trx) | DB user 缺 PROCESS 权限 | RDS admin 跑 `GRANT PROCESS ON *.* TO 'mcp_devops_ro'@'%'; FLUSH PRIVILEGES;`(本 spec init_rds_user.sh 已包含,但若 admin 漏跑会出现) |
| destroy 后 RDS SG 残留 ingress 规则 | `destroy_spec2.sh` step [4/5] fail | 手动 `aws ec2 revoke-security-group-ingress --group-id sg-09b483e0cb2e97f69 --source-group <Runtime SG ID> --protocol tcp --port 3306` |

## Testing Strategy

- **主验证**:`bash scripts/bootstrap_spec2.sh && cdk deploy McpInspectSpec2Stack --require-approval never && python scripts/verify_spec2.py`,末尾必须打印 `✅ Spec 2 verification passed`
- **回归 Spec 0 + Spec 1**:Spec 2 destroy 后,`python scripts/verify.py` + `python scripts/verify_spec1.py` 都仍要 pass(本 spec Requirement 5.5)
- **不引入单元测试**:沿用 Spec 0/1 设计哲学(端到端 verify 即唯一证据)。tool 内部的分类逻辑(`_classify_status` / `_build_findings` / `_build_recommendation`)若未来跨多个数据源复用,在 Spec 3 抽到 `framework/tool_response.py` 时再补单测
- **人工 checklist**:DevOps Agent 在 Agent Space 控制台触发会话,确认看到第二个 tool `rdsmysql___rds_mysql_inspect_long_transactions`,调用一次得到结构化结果(对应本 spec Requirement 6 之外的人工验收)
- **清理验证**:`destroy_spec2.sh` 5 步残留扫描(Runtime / Target / RDS SG ingress / Secret / Spec 0/1 资源)全过

## Decisions and Tradeoffs

### Decision 1:VPC 模式通过扩展 Spec 0 framework Construct 实现(不新建 Construct)
**选择**:在 `framework/runtime_construct.py` 的 `McpInspectRuntime.__init__` 增加 3 个 Optional 入参(vpc / subnets / security_groups),传 None 走 Spec 0 行为(public),传齐走 VPC 模式。

**Why**:alpha L2 探针实测 `RuntimeNetworkConfiguration.using_vpc` 接受全 stable CDK 类型,Construct API 增量扩展即可,不需要新建 `framework/runtime_vpc_construct.py` 这种第二个文件。alpha 隔离边界仍是 2 个文件(SHALL NOT #2 不变)。

**Tradeoff**:Spec 0 Construct 升级后,Spec 0 stack 重 deploy 时模板 hash 是否变化?Task 1 实施时 cdk diff 确认 — 预期等价(`network_configuration=None` ≡ `using_public_network`)。如果有差异,Spec 0 会触发 Runtime UPDATE_REPLACE,这是不可接受的(Spec 0 已部署生产意义的 hello target),需要回头改 Construct(传 None 时走旧路径,显式传 public 时才用 `using_public_network`)。

### Decision 2:VPC / 子网 / SG ID 硬编码到 stacks/spec_two_stack.py 模块级常量
**选择**:不通过 cdk context 或 SSM Parameter 中转,直接在代码里写 `_TARGET_RDS_VPC_ID = "vpc-0e4a299032154a1be"` 等 6 个常量。

**Why**:
1. 这些 ID 是 jupiter-dev 已存在资源,跨 spec 稳定,不会随 cdk deploy 变化(SHALL NOT #9 限定的是"AWS 自动生成的 ARN / region / account",对"现成基础设施 ID"约束较松)
2. cdk context 方案需要 `cdk synth` 时连 AWS,本机网络抖动容易卡(王总实测痛点)
3. SSM 中转方案需要再写一个 boot 脚本,Spec 2 已经有 bootstrap_spec2.sh 在干活,不再加一层

**Tradeoff**:如果未来 jupiter-dev VPC 重建 / 子网重排,需要回头改这 6 个常量。这风险小,且 Spec 3+ 接其他 RDS 时,反正也要改这些 ID。SHALL NOT #9 的精神(可移植性)在这里通过 region 仍走 self.region 来保证(account 由 cdk env 注入),不被这条妥协破坏。

### Decision 3:DB 凭据用 Mode A 单 secret 多库
**选择**:`mcp-inspect/rds-mysql/devops-readonly` 一个 secret,所有 RDS 实例的 `mcp_devops_ro` user 共用同一个密码。

**Why**(王总决策原文):"100 个库不可能 100 个不同密码,巡检场景运维成本远高于一库一密的爆炸半径收益,我只需要限制这个 user 最少的只读权限即可"。conventions A5 已修订定型。

**Tradeoff**:爆炸半径 = 单 secret 泄露 → 全部 RDS 巡检账号失守。控制点:
1. **DB user 权限纯只读**:无 DDL / DML / DROP,最坏情况 = 读到信息,不破坏数据
2. **Secrets Manager 自动轮换**(Spec 5+ 启用):降低单密码长期泄露风险
3. **CloudTrail 审计**:secretsmanager:GetSecretValue 全部记录,异常调用可追

### Decision 4:在 Spec 1 Gateway 上挂第二个 target,而非新建第二个 Gateway
**选择**:复用 Spec 1 Gateway,本 spec 只新增 MCP target。

**Why**:conventions A2 单 Gateway 策略 + DevOps Agent 端无需重新注册(Spec 1 已注册过的 Gateway URL 一旦添加新 target,DevOps Agent `tools/list` 自动发现新工具)。

**Tradeoff**:Spec 1 Gateway 的 inbound auth(Cognito client + scope=mcp/invoke)是否对所有 target 一刀切?— 实测是的,Gateway 入站策略是 Gateway 级,不是 target 级。如果未来需要"hello tool 任何人可调,RDS tool 仅 DBA team 可调"这种细粒度,需要拆 Gateway。Spec 2 不解这个,留 Out of Scope。

### Decision 5:Spec 1 GatewayId 走 SSM 中转,而非 Fn.import_value
**选择**:本 spec 引入 `bootstrap_spec2.sh` 把 Spec 1 GatewayId 写到 SSM Parameter,Spec 2 stack 用 `value_for_string_parameter` 读。

**Why**:Spec 1 起草时的 4 个 CfnOutput(GatewayId / GatewayUrl / GatewayCredentialProviderArn / Region)虽然加了 export_name,但 `GatewayId` 这个 OutputKey 的值是 Gateway ARN 末尾的 ID 字符串,Spec 2 需要的是这个 ID 用来 `agentcore.Gateway.from_gateway_id(...)` 构造引用。直接 Fn.import_value 拿到 token,然后传给 `from_gateway_id` 应该可行 — 但 Spec 1 Task 6 实测 export 元数据落地需要重 deploy 上游 stack(回头改的概率高),走 SSM 中转避免再踩这个坑。

**Tradeoff**:多一个 boot 脚本调用,但与 Spec 1 bootstrap_spec1.sh 模式一致,运维心智负担小。

### Decision 6:不为 Spec 2 Runtime 建 VPC Endpoint(ECR / SecretsManager / Logs / STS)
**选择**:Runtime 出站全部走 NAT Gateway,不建 4 类 VPC Interface Endpoint。

**Why**:
1. jupiter-dev 已有 NAT(实测),Runtime 通过 NAT 出公网拉 ECR / Cognito / Secrets Manager / Logs 完全可达
2. VPC Interface Endpoint 每个 ~7 USD/月 × 4 ≈ 28 USD/月,Spec 2 单 Runtime 流量低,NAT 数据费 < 1 USD/月,总成本反而 NAT 更省
3. VPC Endpoint 引入 4 个新资源 + private DNS / SG 配置复杂度,Spec 2 任务粒度 4-7 个 task 不允许这么大

**Tradeoff**:NAT 是单点(jupiter-dev 是否 multi-AZ NAT 待 Task 1 二次实测),如果 NAT 挂,Spec 2 Runtime 出口全部失败。生产场景考虑 multi-AZ NAT,Spec 2 不解。

## 风险与未决项

| # | 风险 | 触发任务 | 缓解 / 待验证项 |
|---|------|---------|----------------|
| 1 | Spec 0 framework Construct 升级触发 Spec 0 stack Runtime UPDATE_REPLACE | Task 1 | cdk diff 验证 `network_configuration=None` ≡ Spec 0 当前 public 模板;若不等价,改 Construct 兼容旧路径 |
| 2 | `ec2.Vpc.from_lookup` 在 cdk synth 时连 AWS 拿 VPC 真实属性,本机网络抖动卡 | Task 3 | 备选方案:用 `ec2.Vpc.from_vpc_attributes(...)` 直接传 ID,不查 AWS;牺牲一点跨 AZ 子网自动选择能力 |
| 3 | `agentcore.Gateway.from_gateway_id(...)` 是否存在(Spec 2 引用 Spec 1 Gateway 必需) | Task 1 探针 | 若不存在,需要 Spec 1 Stack 直接 export Gateway 实例(违反 Spec 1 已发布约束);备选用 L1 `CfnGatewayTarget` 直接传 GatewayId 字符串 |
| 4 | RDS SG inbound 规则被多 spec 同时管理时的潜在冲突(如 Spec 3 接第二个数据源)| Spec 3+ | 每个 spec 给规则加唯一 description tag(本 spec 用 "Spec 2 Runtime → jupiter-dev-slurm-db"),destroy 时按 description 反向清理 |
| 5 | DB user `mcp_devops_ro` 权限被 DBA 误改(如收回 PROCESS) | 运维期 | tool 返回 OperationalError 1142,verify 完整 traceback 暴露;init_rds_user.sh 可幂等重跑修复 |
| 6 | jupiter-dev-slurm-db 重启 / failover 后 endpoint DNS 解析延迟 | 运维期 | pymysql connect_timeout=5s 是 fail-fast 边界,实测可放宽到 10s;Construct 入参里加 `connect_timeout` 可调 |
| 7 | Spec 2 Runtime ENI 部署到 jupiter-dev 私网 → ENI 数额耗尽 | 部署期 | 实测 jupiter-dev 3 个 db 子网每个 /24 = 254 IP,Runtime 使用 1-3 个,远低于上限;若未来其他 Runtime 共用,留意 |
| 8 | Secret PLACEHOLDER → 真密码切换期间(deploy 完到 init_rds_user.sh 跑完之间)tool 调用失败 | Task 6 | verify_spec2 前置校验已挡(Property 10);但 DevOps Agent 在中间状态调 tool 仍会失败,提示用户"等 init_rds_user.sh 跑完再试" |
| 9 | Gateway tool 命名空间分隔符(三下划线) Task 6 实测后是否仍稳定 | Task 6 | Spec 1 Task 6 已实测三下划线;若 alpha 升级改了规则,verify_spec2 in 断言会立刻暴露 |
| 10 | NAT Gateway 单点 / multi-AZ 状态 | Task 6 | 本 design Decision 6 已说明;Spec 2 不解,留生产化时改造 |


## Correctness Properties

形式化的、可执行的程序级正确性约束,用 verify_spec2.py + 部署期断言 + grep 检查共同验证。

### Property 1:链路认证完整性

所有 inbound 调用必须经 Cognito JWT(scope=mcp/invoke)校验,无 access_token 时 Gateway 返回 401。  
**验证**:verify_spec2 默认链路用 token;不带 token 主动测一次(可选)。  
**Validates: Requirements 1.5, 6.3**

### Property 2:Tool 命名空间稳定性

Gateway 暴露的 tool 名 = `{target_name}___{tool_name}`(三下划线,SHALL NOT #21);Spec 2 后 tools/list 必须**同时**含 `spec0helloworld___hello_world` 与 `rdsmysql___rds_mysql_inspect_long_transactions`。  
**验证**:verify_spec2 step 5 双 tool 名 in 断言。  
**Validates: Requirements 4.4, 4.5, 6.4**

### Property 3:Tool 行为确定性 — 阈值语义

任意 `threshold_seconds = T`,返回的 `findings` 中每条 `value` 表达的 duration ≥ T;`status` 取值集合 ⊆ {ok, warning, critical}。  
**验证**:verify_spec2 `_assert_a8_structure` + 部署后人工验证一次。  
**Validates: Requirements 2.5, 2.6**

### Property 4:Tool 返回结构合规(conventions A8)

返回 dict 必含 4 key — `status` / `findings` / `raw_data` / `recommendation`;`status` ∈ {ok, warning, critical};`findings` 是 list;`raw_data` 是 dict;`recommendation` 是非空 str。  
**验证**:verify_spec2 `_assert_a8_structure`(本 spec 新增 Property,与 Spec 0/1 不重复)。  
**Validates: Requirements 2.6, 6.6**

### Property 5:跨 Stack 资源清理可逆性

`destroy_spec2.sh` 后,Spec 0/1 的端到端 verify 仍 pass;jupiter-dev-slurm-db SG 无 Spec 2 残留 ingress;Spec 2 Secret 进 PendingDeletion 状态。  
**验证**:destroy_spec2.sh 5 步残留扫描 + Spec 0/1 verify 回归(本 spec Requirement 5.5)。  
**Validates: Requirements 5.4, 5.5, 9.1, 9.2, 9.5**

### Property 6:凭据零泄露

CFN 模板 / verify stdout / Runtime 容器日志中无 access_token / Cognito client_secret / DB password 明文(Stack 模板里 secret 是 `{{resolve:secretsmanager:...}}` token,容器只 print length)。  
**验证**:`cdk synth | grep -i 'secret'` 仅 token 引用 + verify stdout 审计 + 容器日志审计。  
**Validates: Requirements 3.3, 6.9, 6.10**

### Property 7:Alpha 类型隔离边界

`grep -rn aws_bedrock_agentcore_alpha --include="*.py" --exclude-dir=.venv --exclude-dir=cdk.out .` 仅命中 `framework/runtime_construct.py` + `framework/gateway_construct.py` 两个文件(Spec 1 已建立,Spec 2 不破坏)。  
**验证**:Task 1 落地 / Task 6 最终回归 grep。  
**Validates: Requirements 8.2, 8.3**

### Property 8:部署时长上界

`bootstrap_spec2.sh + cdk deploy + verify_spec2` 单次端到端 ≤ 15 分钟(VPC 模式 Runtime 比 public 慢 1-2 分钟,total 仍要 ≤ 15)。  
**验证**:Task 6 端到端真实部署计时。  
**Validates: Requirements 6.7**(verify 末尾打印 ✅ 即视为完成,Constraints 表"部署 + verify ≤ 15 分钟"附带要求)

### Property 9:DB Secret 真实化

Secret `mcp-inspect/rds-mysql/devops-readonly` 的 password 字段不是 `PLACEHOLDER_REPLACE_BY_DBA`(verify 前置校验)。  
**验证**:verify_spec2 `_check_db_secret_not_placeholder`(本 spec 新增,Property 4 之外)。  
**Validates: Requirements 3.6, 6.2**

### Property 10:跨 Spec 凭据隔离

Spec 2 Runtime 执行角色不能读 Spec 0/1 的 secret(Spec 1 OAuth Provider 内部 secret) — IAM 资源段限定到 `arn:aws:secretsmanager:us-east-1:{account}:secret:mcp-inspect/rds-mysql/*`,不通配整个 `mcp-inspect/*`。  
**验证**:Task 1 落地 + Task 6 IAM Policy 文档审。  
**Validates: Requirements 3.4, 7.1**


