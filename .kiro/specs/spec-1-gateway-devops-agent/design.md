# Design Document

## Overview

Spec 1 在 Spec 0(已部署成功)之上引入聚合层,把"DevOps Agent → AgentCore Gateway → Spec 0 Runtime → hello_world tool"的端到端链路打通。

新增三类 AWS 资源:

- **AgentCore Gateway** × 1(单实例,符合 project-conventions.md A2)
- **MCP Target** × 1(挂在 Gateway 上,后端指向 Spec 0 Runtime)
- **Token Vault OAuth2CredentialProvider** × 1(Gateway 出站调 Spec 0 Runtime 时换 Cognito access_token)

新增的本机工具:

- **`scripts/verify_spec1.py`** — 端到端验证(Cognito → Gateway → tools/list / tools/call)
- **`scripts/destroy_spec1.sh`** — 仅清 Spec 1 资源,不影响 Spec 0

设计核心决定:

1. **Spec 1 与 Spec 0 严格双 Stack 隔离**,通过 CloudFormation cross-stack reference 关联;`cdk destroy McpInspectSpec1Stack` 不连带 Spec 0
2. **不创建任何新的 Cognito 资源**,完全复用 Spec 0 的 User Pool / M2M Client / Secret(requirements.md SHALL NOT #1)
3. **alpha 风险面继续收敛在 framework/**:新增 `framework/gateway_construct.py`,Spec 0 已建立的 alpha 隔离规则原样套用(grep 仅命中 framework/runtime_construct.py + framework/gateway_construct.py 两个文件)
4. **API 实证先行**:Spec 0 实施期踩过 `RuntimeAuthorizerConfiguration.using_cognito` 位置参数的坑;本 spec 起草前已对 `aws_bedrock_agentcore_alpha 2.254.0a0` 的 Gateway / OAuth2CredentialProvider / Authorizer 工厂方法做实证签名探查,代码骨架以 jsii 实测签名为准(详见 Components 章节)

## Architecture

### 部署后运行时架构

```
┌─────────────────────────────────────────────────────────────────┐
│  AWS DevOps Agent(云端,在 Agent Space 注册一次)                 │
│         │ Streamable HTTP + Cognito JWT                          │
│         │ scope=mcp/invoke                                       │
└─────────┼────────────────────────────────────────────────────────┘
          ▼
┌─────────────────────────────────────────────────────────────────┐
│  AWS us-east-1                                                   │
│                                                                  │
│  Cognito UserPool ◄────── 复用 Spec 0(不修改)                   │
│   └─ M2M Client(client_credentials, scope mcp/invoke)            │
│                                                                  │
│  ╔═════════════════════════════════════════════════╗             │
│  ║ McpInspectSpec1Stack(本 Spec 新建)             ║             │
│  ║                                                 ║             │
│  ║  AgentCore Gateway(Public Network, MCP 协议)   ║             │
│  ║   ├─ Inbound Auth: Cognito JWT(用 Spec 0 UP)  ║             │
│  ║   ├─ X-Ray ON                                   ║             │
│  ║   └─ LogGroup /aws/bedrock-agentcore/gateway/* ║             │
│  ║       │                                         ║             │
│  ║       │ tools/list: ['spec0helloworld__hello_world'] ║       │
│  ║       │ tools/call: name=spec0helloworld__hello_world ║      │
│  ║       ▼                                         ║             │
│  ║  MCP Target(name=spec0helloworld)              ║             │
│  ║       │                                         ║             │
│  ║       │ outbound: 由 Token Vault 自动换 Cognito ║             │
│  ║       │ access_token,注入 Authorization header  ║             │
│  ║       ▼                                         ║             │
│  ║  Token Vault OAuth2CredentialProvider           ║             │
│  ║   (vendor=Cognito, client_credentials,           ║             │
│  ║    引用 Spec 0 Cognito 的 client_id /            ║             │
│  ║    client_secret(SecretsManager))                ║             │
│  ╚═════════════════════════════════════════════════╝             │
│       │ Bearer <jwt with scope=mcp/invoke>                       │
│       ▼                                                          │
│  ╔═════════════════════════════════════════════════╗             │
│  ║ McpInspectSpec0Stack(已部署,本 Spec 不动)      ║             │
│  ║                                                 ║             │
│  ║  AgentCore Runtime(spec0_hello_world)           ║             │
│  ║   └─ Container: FastMCP + hello_world tool      ║             │
│  ╚═════════════════════════════════════════════════╝             │
└─────────────────────────────────────────────────────────────────┘
```

### 双 Stack 边界与跨 Stack 引用关系

```
McpInspectSpec0Stack                 McpInspectSpec1Stack
(已部署)                              (本 spec 新建)

Outputs:                              Inputs(via Fn.import_value):
  RuntimeArn ──────────────────────► runtime_arn          → Target.endpoint
  CognitoTokenEndpoint ────────────► token_endpoint       → OAuth2 token URL
  CognitoClientId ─────────────────► client_id            → OAuth2 client_id
  CognitoClientSecretArn ──────────► secret_arn           → OAuth2 client_secret
  CognitoOAuthScope ───────────────► oauth_scope (mcp/invoke)
  RuntimeUrl                         (本 spec 不需要)
  Region                             (本 spec 不需要)

McpInspectSpec1Stack 内部新建资源:
  - AgentCore Gateway
  - MCP Target(指向 RuntimeArn)
  - OAuth2CredentialProvider(引用 client_id + secret_arn)
  - Gateway 执行角色(最小权限)
  - LogGroup
```

### 部署期资源依赖

```
1. Spec 1 Stack 通过 Fn.import_value 解析 Spec 0 的 4 个 Output
   (此时 Spec 0 必须已 CREATE_COMPLETE,CDK 在 cdk.out manifest 里
    自动建立 stack-level dependsOn)
                         ▼
2. Token Vault OAuth2CredentialProvider 创建
   (用 client_id + Spec 0 SecretValue)
                         ▼
3. Gateway 创建
   (Cognito JWT 入站 + LogGroup + 最小权限 IAM 角色)
                         ▼
4. MCP Target 通过 gateway.add_mcp_server_target(...) 创建
   (endpoint = RuntimeArn → Runtime URL,credential_provider 引用上一步 Provider)
                         ▼
5. 7 个 CfnOutput 暴露:GatewayId / GatewayUrl / Region 等
```

文字版示意:

```
Fn.import_value × 4 ──► OAuth2CredentialProvider ──► Gateway ──► MCP Target
                                                       │
                                                       └──► CfnOutput × 2
```

## 项目目录结构(增量)

在 Spec 0 的目录基础上,Spec 1 引入:

```
aws-devops-mcp-platform/
├─ .kiro/specs/spec-1-gateway-devops-agent/    ← 本 spec 文档
│   ├─ requirements.md
│   ├─ design.md          ← 本文档
│   └─ tasks.md           ← 后续生成
│
├─ stacks/
│   ├─ spec_zero_stack.py             ← Spec 0(不动)
│   └─ spec_one_stack.py              ← 本 spec 新增(Stack 拼装)
│
├─ framework/
│   ├─ runtime_construct.py           ← Spec 0(不动)
│   └─ gateway_construct.py           ← 本 spec 新增(alpha 隔离唯一新增点)
│
├─ scripts/
│   ├─ verify.py                      ← Spec 0(不动)
│   ├─ destroy.sh                     ← Spec 0(不动)
│   ├─ verify_spec1.py                ← 本 spec 新增(端到端验证)
│   └─ destroy_spec1.sh               ← 本 spec 新增(只清 Spec 1)
│
├─ app.py                              ← 修改(增加 SpecOneStack 实例化)
├─ requirements.txt                    ← 不变(Spec 0 已锁版本一致复用)
└─ README.md                           ← 增量补 Spec 1 章节(部署 / 验证 / Agent Space 注册 checklist)
```

`mcp_servers/` 目录完全不动:Spec 1 的 MCP target 后端就是 Spec 0 那个容器,不重新构建镜像,不重新推 ECR。

## Components and Interfaces

### Component 1: `framework/gateway_construct.py` — `McpInspectGateway`

框架层核心 Construct。alpha 类型只在本文件出现,对外只暴露稳定参数。

实证签名摘要(以 `aws_bedrock_agentcore_alpha 2.254.0a0` 为准,jsii 1.130 生成):

| API | 位置参数 | 关键 keyword args |
|-----|---------|------------------|
| `Gateway.__init__` | scope, id | `protocol_configuration`、`authorizer_configuration`、`role`、`gateway_name` |
| `GatewayAuthorizer.using_cognito`(static) | — | `user_pool: IUserPool`、`allowed_clients: list[IUserPoolClient]`、`allowed_scopes: list[str]`、`allowed_audiences`、`custom_claims` |
| `GatewayProtocol.mcp`(static) | — | `instructions`、`search_type`、`supported_versions` |
| `OAuth2CredentialProvider.using_cognito`(static) | scope, id | `client_id: str`、`client_secret: SecretValue`、`token_endpoint: str`(可选还有 issuer / authorization_endpoint) |
| `Gateway.add_mcp_server_target`(instance) | id | `credential_provider_configurations: list[ICredentialProviderConfig]`、`endpoint: str`、`gateway_target_name` |
| `GatewayCredentialProvider.from_oauth_identity`(static) | — | 把 OAuth2CredentialProvider 包装成 ICredentialProviderConfig 给 target 用 |

代码骨架:

```python
"""McpInspectGateway — 框架核心 Construct(Spec 1 引入)。

一站式封装:
- Token Vault OAuth2CredentialProvider(Cognito client_credentials)
- AgentCore Gateway(MCP 协议、Cognito JWT 入站、X-Ray、CloudWatch Logs、最小权限 IAM)
- MCP Target(挂在 Gateway 上,后端指向上游 AgentCore Runtime)

设计要点(参考 .kiro/steering/shall-not.md / project-conventions.md):
- 第 2 / 第 8 条 alpha 隔离:`aws_bedrock_agentcore_alpha` import 仅在本文件;
  Construct 对外只暴露 Python 原生类型与稳定 CDK 类型(str / SecretValue / ISecret 等)
- 第 6 / 第 8 条 强制 Cognito JWT:Gateway 入站 authorizer 固定 using_cognito,不接受 NoAuth / IAM
- 第 11 条 最小权限:Gateway 执行角色仅 CW Logs / X-Ray / Token Vault 三类
- 第 14 条 RemovalPolicy.DESTROY:Gateway / Provider / LogGroup 全部 DESTROY

对外稳定参数(均为 Python 原生类型 / stable CDK 类型,严禁 alpha 类型外漏):
    runtime_arn:        Spec 0 AgentCore Runtime ARN(MCP target 的 endpoint 解析用)
    runtime_url:        Spec 0 Runtime invocation URL(直接给 target.endpoint)
    token_endpoint:     Spec 0 Cognito OAuth2 token endpoint
    client_id:          Spec 0 M2M UserPoolClient ID
    client_secret:      secretsmanager.ISecret(Spec 0 包装的 Cognito client_secret)
    oauth_scope:        OAuth scope(mcp/invoke)
    user_pool:          cognito.IUserPool(从 Spec 0 跨 stack 引用得到)
    user_pool_client:   cognito.IUserPoolClient(同上)
    gateway_name:       Gateway 名(满足 alpha 包对名字的限制)
    target_name:        MCP target 名(15 字符以内,project-conventions.md A7)

对外稳定属性:
    gateway_id:                 str(token,deploy 后 CFN 解析)
    gateway_url:                str(token,MCP invocation URL)
    credential_provider_arn:    str(token)
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from aws_cdk import (
    Duration,
    RemovalPolicy,
    SecretValue,
    Stack,
    aws_cognito as cognito,
    aws_logs as logs,
    aws_secretsmanager as secretsmanager,
)
# !!! alpha 隔离唯一允许出现的位置(继 framework/runtime_construct.py 后第二个文件)
from aws_cdk import aws_bedrock_agentcore_alpha as agentcore
from constructs import Construct


_TARGET_NAME_MAX_LEN: Final[int] = 15  # 给 Gateway 自动加 "{name}__" 前缀留 49 字符空间


class McpInspectGateway(Construct):
    """AgentCore Gateway + MCP Target + Token Vault OAuth2 Provider 一站式封装。

    Spec 1 默认 Public Network。Spec 2+ 接 RDS 时再增量加 vpc 参数。
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        runtime_arn: str,
        runtime_url: str,
        token_endpoint: str,
        client_id: str,
        client_secret: secretsmanager.ISecret,
        oauth_scope: str,
        user_pool: cognito.IUserPool,
        user_pool_client: cognito.IUserPoolClient,
        gateway_name: str,
        target_name: str,
    ) -> None:
        super().__init__(scope, construct_id)

        # 防御性校验(Gateway 加 "{name}__" 前缀后 + tool 名 ≤ 64 字符)
        if len(target_name) > _TARGET_NAME_MAX_LEN:
            raise ValueError(
                f"target_name {target_name!r} 超过 {_TARGET_NAME_MAX_LEN} 字符"
            )

        stack = Stack.of(self)

        # ---- 1) Token Vault OAuth2CredentialProvider(Cognito client_credentials)----
        #
        # 用 OAuth2CredentialProvider.using_cognito 工厂(实测签名:
        #   client_id / client_secret(SecretValue) 必填,
        #   token_endpoint / issuer / authorization_endpoint / name 可选)。
        #
        # 注意 client_secret 类型是 cdk.SecretValue,不是 str — 把 ISecret 转成 SecretValue:
        #   `client_secret.secret_value`
        # 这是 ISecret 的标准属性,部署期由 CFN 解析,不暴露明文。
        credential_provider = agentcore.OAuth2CredentialProvider.using_cognito(
            self,
            "OAuth2Provider",
            client_id=client_id,
            client_secret=client_secret.secret_value,
            token_endpoint=token_endpoint,
            o_auth2_credential_provider_name=f"{gateway_name}-cognito-cc",
        )
        credential_provider.apply_removal_policy(RemovalPolicy.DESTROY)

        # ---- 2) Application logs LogGroup ----
        log_group = logs.LogGroup(
            self,
            "LogGroup",
            log_group_name=f"/aws/bedrock-agentcore/gateway/{gateway_name}",
            removal_policy=RemovalPolicy.DESTROY,
            retention=logs.RetentionDays.ONE_WEEK,
        )

        # ---- 3) Gateway(Cognito JWT 入站,X-Ray,MCP 协议)----
        gateway = agentcore.Gateway(
            self,
            "Gateway",
            gateway_name=gateway_name,
            protocol_configuration=agentcore.GatewayProtocol.mcp(),
            authorizer_configuration=agentcore.GatewayAuthorizer.using_cognito(
                user_pool=user_pool,
                allowed_clients=[user_pool_client],
                allowed_scopes=[oauth_scope],
            ),
            # role 不传 — L2 默认创建最小权限执行角色;若 L2 默认权限不满足
            # SHALL NOT #11(最小权限)/Requirement 7,在 tasks 阶段再切换 role= 自定义
        )
        gateway.apply_removal_policy(RemovalPolicy.DESTROY)

        # ---- 4) 把 Provider 包装成 ICredentialProviderConfig 给 target ----
        # GatewayCredentialProvider.from_oauth_identity 是工厂方法(实测可用)
        target_creds = agentcore.GatewayCredentialProvider.from_oauth_identity(
            credential_provider,
            scopes=[oauth_scope],
        )

        # ---- 5) MCP Target(后端指向 Spec 0 Runtime URL)----
        # 用 instance 方法 add_mcp_server_target,而非独立构造 GatewayTarget;
        # 因为 add_mcp_server_target 自动把 gateway 关联好,代码更短。
        # endpoint 直接传 Spec 0 Runtime invocation URL(已是 https + 已 URL-encode 的 ARN)。
        target = gateway.add_mcp_server_target(
            "Target",
            credential_provider_configurations=[target_creds],
            endpoint=runtime_url,
            gateway_target_name=target_name,
        )
        target.apply_removal_policy(RemovalPolicy.DESTROY)

        # ---- 6) 暴露稳定属性 ----
        self.gateway_id: str = gateway.gateway_id  # 假设属性名,以 alpha 实测为准
        self.gateway_url: str = gateway.gateway_url  # 同上
        self.credential_provider_arn: str = credential_provider.credential_provider_arn  # 同上
```

> **alpha 实测注意**:`gateway.gateway_id` / `gateway.gateway_url` / `credential_provider.credential_provider_arn` 三个属性名是**最佳猜测**,实际属性名以 `aws_bedrock_agentcore_alpha 2.254.0a0` 中 `Gateway` / `OAuth2CredentialProvider` 类的实际定义为准(tasks 阶段实施时打开 .venv/lib/python3.13/site-packages/aws_cdk/aws_bedrock_agentcore_alpha/__init__.py 确认,踩到 Spec 0 同型坑时按 jsii 实测签名修正)。

> **关于 LogGroup**:Spec 0 实测中观察到 alpha L2 在 `tracing_enabled=True + logging_configs` 配置下会自动创建 Logs Delivery 链路;Gateway L2 是否也支持类似 `logging_configs` keyword 当前未在签名中暴露(`Gateway.__init__` 实测无 logging_configs)。Spec 1 兜底方案:LogGroup 资源由本 Construct 显式创建,Gateway 内部 logging delivery 走 alpha L2 的默认行为(若不生成 delivery 链,在 tasks 阶段补 L1 escape hatch 创建 `AWS::Logs::DeliverySource` + `DeliveryDestination` 关联到本 LogGroup)。

### Component 2: `stacks/spec_one_stack.py` — `SpecOneStack`

Stack 类只承载"组合 + 跨 stack 引用 + 输出",不重复定义 alpha 资源。

```python
"""SpecOneStack — Spec 1 部署单元(独立 Stack)。

职责:
- 通过 Fn.import_value 跨 stack 引用 Spec 0 的 4 个 Output(RuntimeArn / RuntimeUrl /
  CognitoTokenEndpoint / CognitoClientId / CognitoClientSecretArn)
- 通过 IUserPool / IUserPoolClient 的 from_xxx_id 工厂构造 Cognito 引用
- 实例化 McpInspectGateway(framework/gateway_construct.py)
- 通过 7 个 CfnOutput 暴露:GatewayId / GatewayUrl / GatewayCredentialProviderArn /
  Region(其余可选)

对应 design.md Component 2;requirements.md Requirement 1 / 4;
shall-not.md #2(alpha 隔离 — 本文件不出现 alpha import)、
#9(不硬编码 region/account/ARN)、#12 / #13(不输出 secret 明文)。
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import (
    Fn,
    aws_cognito as cognito,
    aws_secretsmanager as secretsmanager,
)
from constructs import Construct

from framework.gateway_construct import McpInspectGateway

# Spec 0 Stack 名(跨 stack reference 用)。固定字符串,与 spec_zero_stack.py 一致。
_SPEC0_STACK_NAME: str = "McpInspectSpec0Stack"


class SpecOneStack(cdk.Stack):
    """Spec 1 主 Stack。"""

    _GATEWAY_NAME: str = "spec1_gateway"
    _TARGET_NAME: str = "spec0helloworld"  # 15 字符,Gateway 加 "{name}__" 前缀后 + hello_world = 28 字符 < 64

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ---- 跨 Stack reference:从 Spec 0 Output 读 4 个值 ----
        #
        # Spec 0 当前 CfnOutput 没有显式 export_name,CDK 默认会基于 stack name + output key
        # 自动生成 export 名(形如 "McpInspectSpec0Stack:RuntimeArn",但确切格式以
        # cdk.out manifest 为准)。本 spec 用 Fn.import_value 配合 CDK 自动生成的 export 名。
        #
        # 注意:tasks 阶段实施时,先 cdk synth Spec 0 后查看
        # cdk.out/McpInspectSpec0Stack.template.json 的 Outputs 段,确认每个 Output 的
        # Export.Name 字段;如果没有(说明 Spec 0 没显式调用 export_name),Fn.import_value
        # 会失败,届时按 D2 备选方案处理(详见本文档 Decisions and Tradeoffs)。

        runtime_arn = Fn.import_value(f"{_SPEC0_STACK_NAME}:RuntimeArn")
        runtime_url = Fn.import_value(f"{_SPEC0_STACK_NAME}:RuntimeUrl")
        token_endpoint = Fn.import_value(f"{_SPEC0_STACK_NAME}:CognitoTokenEndpoint")
        client_id = Fn.import_value(f"{_SPEC0_STACK_NAME}:CognitoClientId")
        client_secret_arn = Fn.import_value(
            f"{_SPEC0_STACK_NAME}:CognitoClientSecretArn"
        )
        oauth_scope = Fn.import_value(f"{_SPEC0_STACK_NAME}:CognitoOAuthScope")

        # ---- 把 ARN 转成 ISecret(stable CDK 类型,framework Construct 接受) ----
        client_secret = secretsmanager.Secret.from_secret_complete_arn(
            self, "ImportedClientSecret", client_secret_arn,
        )

        # ---- Cognito User Pool / Client 引用 ----
        # Spec 0 没把 UserPoolId / UserPoolClientId 单独 export,我们这里用 SSM Parameter
        # 中转(详见 D2 选项 C),或者补一个 ID 提取环节。在 tasks 阶段决定。
        # 占位:暂用 from_user_pool_id 风格,实际 ID 来源 tasks 阶段再敲定。
        user_pool = cognito.UserPool.from_user_pool_id(
            self, "ImportedUserPool",
            Fn.import_value(f"{_SPEC0_STACK_NAME}:CognitoUserPoolId"),  # 需 Spec 0 补 export 或换 SSM
        )
        user_pool_client = cognito.UserPoolClient.from_user_pool_client_id(
            self, "ImportedUserPoolClient", client_id,
        )

        gateway = McpInspectGateway(
            self, "Spec1Gateway",
            runtime_arn=runtime_arn,
            runtime_url=runtime_url,
            token_endpoint=token_endpoint,
            client_id=client_id,
            client_secret=client_secret,
            oauth_scope=oauth_scope,
            user_pool=user_pool,
            user_pool_client=user_pool_client,
            gateway_name=self._GATEWAY_NAME,
            target_name=self._TARGET_NAME,
        )

        # ---- CfnOutputs(严格命名,verify_spec1.py 通过 OutputKey 读取) ----
        cdk.CfnOutput(self, "GatewayId", value=gateway.gateway_id,
                      description="AgentCore Gateway ID")
        cdk.CfnOutput(self, "GatewayUrl", value=gateway.gateway_url,
                      description="AgentCore Gateway MCP invocation URL")
        cdk.CfnOutput(self, "GatewayCredentialProviderArn",
                      value=gateway.credential_provider_arn,
                      description="Token Vault OAuth2CredentialProvider ARN")
        cdk.CfnOutput(self, "Region", value=self.region,
                      description="AWS region the stack is deployed to")
```

> **关于 UserPoolId**:Spec 0 当前没把 `UserPoolId` 直接 export 出来(只 export 了 `CognitoTokenEndpoint`/`CognitoClientId`/`CognitoClientSecretArn` 等)。这是 Spec 0 起草期的认知盲点,但**已发布 spec 不可重写**(project-conventions.md A 类原则)。Spec 1 的解决方案(D2)在下方详述。

### Component 3: `app.py` 改动

```python
#!/usr/bin/env python3
"""CDK app 入口。

Spec 0 + Spec 1 双 Stack 并存。CDK 通过 Fn.import_value 自动建立 stack 间 dependsOn,
deploy 时按依赖顺序执行(Spec 0 先,Spec 1 后);destroy 时手动控制(README 强调先 destroy
Spec 1)。
"""
import aws_cdk as cdk

from stacks.spec_zero_stack import SpecZeroStack
from stacks.spec_one_stack import SpecOneStack

app = cdk.App()

env_us_east_1 = cdk.Environment(region="us-east-1")

SpecZeroStack(app, "McpInspectSpec0Stack", env=env_us_east_1)
SpecOneStack(app, "McpInspectSpec1Stack", env=env_us_east_1)

app.synth()
```

deploy 命令推荐:

```bash
# 方式 A:一次性按依赖顺序
cdk deploy --all --require-approval never

# 方式 B:显式控制(推荐,避免无意触发 Spec 0 的二次部署)
cdk deploy McpInspectSpec1Stack --require-approval never
# 注:如果 Spec 0 已部署且 Output 没变,只 deploy Spec 1 也能拿到 import 值
```

destroy 命令(顺序敏感):

```bash
# 1. 先 destroy Spec 1(否则 Spec 0 的 Output 还被 Spec 1 引用,CFN 拒绝删除 Spec 0)
bash scripts/destroy_spec1.sh

# 2. 然后才能 destroy Spec 0
bash scripts/destroy.sh
```

### Component 4: `scripts/verify_spec1.py`

端到端验证脚本,与 Spec 0 verify.py 同栈(requests + boto3),复用 Spec 0 实施期总结的 SSE utf-8 修复(content-type 不含 charset 时 requests 默认 ISO-8859-1 解,中文会乱码;详见 docs/development-trace.md "Spec 0 跑通后的本机集成"一节)。

```python
"""Spec 1 端到端验证脚本。

链路:
  CFN outputs → Secrets Manager(Spec 0) → Cognito client_credentials token
    → Gateway initialize → tools/list → tools/call

断言:
  - tools/list 包含 'spec0helloworld__hello_world'
  - tools/call(name=Spec1) 返回 text == 'Hello, Spec1!'

失败必须打印完整 traceback(meta-harness 完整 trace 原则);
SHALL NOT 在 stdout 输出 access_token 内容、client_secret 内容或完整 Authorization header。
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
_REGION = "us-east-1"
_TIMEOUT_HTTP = 15


def _read_outputs(stack_name: str, region: str) -> dict[str, str]:
    cf = boto3.client("cloudformation", region_name=region)
    stack = cf.describe_stacks(StackName=stack_name)["Stacks"][0]
    return {o["OutputKey"]: o["OutputValue"] for o in stack["Outputs"]}


def _post_mcp(
    url: str, payload: dict[str, Any], token: str
) -> dict[str, Any]:
    """POST 到 Gateway MCP endpoint,处理 SSE / JSON 双路径(含 utf-8 修复)。"""
    r = requests.post(
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
    r.raise_for_status()
    ctype = r.headers.get("content-type", "").lower()

    if "text/event-stream" in ctype:
        # 强制 utf-8 解(requests 默认 ISO-8859-1 会把中文等 utf-8 字段烂掉)
        for raw_bytes in r.iter_lines(decode_unicode=False):
            if not raw_bytes:
                continue
            line = raw_bytes.decode("utf-8")
            if line.startswith("data:"):
                return json.loads(line[len("data:"):].strip())
        raise RuntimeError(f"SSE response from {url!r} had no data frame")

    if "application/json" in ctype:
        return json.loads(r.content.decode("utf-8"))

    raise RuntimeError(f"Unexpected content-type {ctype!r} from {url!r}")


def main() -> None:
    spec0 = _read_outputs(_SPEC0_STACK_NAME, _REGION)
    spec1 = _read_outputs(_SPEC1_STACK_NAME, _REGION)
    print(f"✓ Stack outputs read: spec0={sorted(spec0)}, spec1={sorted(spec1)}")

    sm = boto3.client("secretsmanager", region_name=_REGION)
    secret = sm.get_secret_value(SecretId=spec0["CognitoClientSecretArn"])[
        "SecretString"
    ]
    print(f"✓ Spec 0 client_secret fetched (length={len(secret)})")

    # client_credentials 取 Cognito access_token
    tr = requests.post(
        spec0["CognitoTokenEndpoint"],
        data={
            "grant_type": "client_credentials",
            "client_id": spec0["CognitoClientId"],
            "client_secret": secret,
            "scope": spec0["CognitoOAuthScope"],
        },
        timeout=10,
    )
    tr.raise_for_status()
    token = tr.json()["access_token"]
    print(f"✓ Cognito access_token acquired (length={len(token)})")

    # MCP initialize → tools/list → tools/call
    gateway_url = spec1["GatewayUrl"]

    init = _post_mcp(
        gateway_url,
        {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "spec1-verify", "version": "0.1.0"},
            },
        },
        token,
    )
    pv = init.get("result", {}).get("protocolVersion")
    print(f"✓ Gateway initialize succeeded (server protocolVersion={pv!r})")

    listed = _post_mcp(
        gateway_url,
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        token,
    )
    tools = [t["name"] for t in listed.get("result", {}).get("tools", [])]
    print(f"✓ tools/list returned: {tools}")
    assert "spec0helloworld__hello_world" in tools, (
        f"未找到 spec0helloworld__hello_world,实际返回 {tools}"
    )

    called = _post_mcp(
        gateway_url,
        {
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {
                "name": "spec0helloworld__hello_world",
                "arguments": {"name": "Spec1"},
            },
        },
        token,
    )
    text = called["result"]["content"][0]["text"]
    assert text == "Hello, Spec1!", f"unexpected response: {text!r}"
    print(f"✓ tools/call returned text={text!r}")

    print("✅ Spec 1 verification passed")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
```

### Component 5: `scripts/destroy_spec1.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

STACK_NAME="McpInspectSpec1Stack"
REGION="us-east-1"
TARGET_NAME="spec0helloworld"

if [[ -d "$REPO_ROOT/.venv/bin" ]]; then
  export PATH="$REPO_ROOT/.venv/bin:$PATH"
fi

echo "==> [1/3] cdk destroy ${STACK_NAME} (region=${REGION})"
npx --yes cdk@2.1122.0 destroy --force "${STACK_NAME}"

echo "==> [2/3] 检查无 AgentCore Gateway 残留"
GW_RESIDUE=$(
  aws bedrock-agentcore-control list-gateways \
    --region "${REGION}" --no-cli-pager --output json \
    --query "items[?name==\`spec1_gateway\`]"
)
if [[ "${GW_RESIDUE}" != "[]" ]]; then
  echo "❌ 残留 Gateway:"; echo "${GW_RESIDUE}"; exit 1
fi
echo "✓ 无 Gateway 残留"

echo "==> [3/3] 检查无 OAuth2CredentialProvider 残留"
PV_RESIDUE=$(
  aws bedrock-agentcore-control list-oauth2-credential-providers \
    --region "${REGION}" --no-cli-pager --output json \
    --query "items[?contains(name, \`spec1_gateway\`)]" 2>/dev/null || echo "[]"
)
if [[ "${PV_RESIDUE}" != "[]" ]]; then
  echo "❌ 残留 OAuth2CredentialProvider:"; echo "${PV_RESIDUE}"; exit 1
fi
echo "✓ 无 OAuth2CredentialProvider 残留"

echo "✅ Spec 1 清理完成,无残留资源"
```

> **AWS CLI 命令名注意**:`aws bedrock-agentcore-control list-gateways` / `list-oauth2-credential-providers` 是基于命名约定的最佳猜测。实施时先用 `aws bedrock-agentcore-control help` 列出真实 subcommand 名再敲定。

## Data Models

Spec 1 不引入持久化数据。运行时配置数据流转:

| 数据 | 来源 | 去向 | 形态 |
|------|------|------|------|
| Cognito access_token(入站) | client → Cognito | Gateway 入站验证 | JWT |
| Cognito access_token(出站) | Token Vault → Cognito | Gateway → Spec 0 Runtime 的 Authorization | JWT(Token Vault 自动注入) |
| Gateway invocation URL | CFN Output | verify_spec1.py / DevOps Agent 注册 | str |
| MCP wire format | Gateway ↔ Runtime ↔ verify | stdio / SSE | JSON-RPC 2.0 |

**tool 命名空间**:Gateway L2 在 MCP target 注册时,自动给上游所有 tool 加 `{target_name}__` 前缀(实测以 alpha 包默认行为为准)。本 spec target_name = `spec0helloworld`,Spec 0 提供的 tool 是 `hello_world`,所以 DevOps Agent / verify_spec1.py 看到的 tool 名是 `spec0helloworld__hello_world`(共 28 字符,远小于 64)。

## Error Handling

| 失败场景 | 表现 | 诊断信号 |
|---------|------|---------|
| `cdk synth` 失败,提示 `OAuth2CredentialProvider.using_cognito` 签名不匹配 | jsii TypeError | 以 `.venv/lib/python3.13/site-packages/aws_cdk/aws_bedrock_agentcore_alpha/__init__.py` 实测签名为准修正 |
| `cdk synth` 失败,提示 `Fn.import_value` 导入名不存在 | CFN: No export named ... found | Spec 0 Output 没有显式 export_name → 走 D2 备选方案(SSM Parameter 中转) |
| Gateway CREATE_FAILED | CFN events 显示 invalid authorizer config / invalid endpoint | `aws bedrock-agentcore-control describe-gateway` 看具体字段 |
| MCP Target 创建失败 | "endpoint not reachable" 或 "credential provider invalid" | (a) endpoint 是不是 Spec 0 RuntimeUrl 完整(含 qualifier=DEFAULT)(b) Provider 是否已 ready |
| OAuth2CredentialProvider 创建失败 | "client_secret SecretValue cannot be resolved" | 通过 `client_secret.secret_value` 拿,而非 `secret_arn` |
| verify initialize 401 | Authorization header 拒绝 | 解码 JWT 看 scope claim 是否含 mcp/invoke;检查 GatewayAuthorizer.using_cognito 的 allowed_clients 是否含 Spec 0 Client |
| `tools/list` 不含 `spec0helloworld__hello_world` | target 还没 ready / 后端 Runtime 不可达 | `aws bedrock-agentcore-control get-gateway-target` 看 status;直接 curl Spec 0 Runtime 验证后端可达 |
| `tools/call` 出站 401 | Token Vault 没成功换 token | Gateway LogGroup 看 outbound token request 错误;check OAuth2 Provider 的 client_id/secret 是否引用对了 |
| 中文乱码 | requests 用 ISO-8859-1 解 SSE | verify_spec1.py 已经按 utf-8 强制解码,设计层面已防 |

## Testing Strategy

- **主验证**:`cdk deploy McpInspectSpec1Stack --require-approval never` + `python scripts/verify_spec1.py`,末尾必须打印 `✅ Spec 1 verification passed`
- **回归 Spec 0**:在 Spec 1 destroy 后,Spec 0 的 verify.py 仍要 pass(Requirement 4.6)
- **不引入单元测试**:沿用 Spec 0 设计原则
- **人工 checklist**:在 Agent Space 控制台真实注册 Gateway,触发一次会话验证 tool 出现且可调(Requirement 6,不阻塞自动化)
- **清理验证**:destroy_spec1.sh exit 0,且 AWS CLI 残留扫描通过(Requirement 9)

## Decisions and Tradeoffs

### Decision 1: Gateway 入站 authorizer 用 `using_cognito` 工厂

**选择**:`GatewayAuthorizer.using_cognito(user_pool=..., allowed_clients=[client], allowed_scopes=["mcp/invoke"])`

**Why**:实测 `aws_bedrock_agentcore_alpha 2.254.0a0` 已经提供这个工厂方法,签名干净(全 keyword args),与 Spec 0 的 `RuntimeAuthorizerConfiguration.using_cognito` 风格一致,**无需实施者去看 Cognito 的 OIDC discovery URL 拼接**。备选 `using_custom_jwt(discovery_url=..., allowed_audiences=...)` 需要手动算 issuer URL,容易出错。

**Tradeoff**:`using_cognito` 需要 IUserPool 与 IUserPoolClient 引用,而 Spec 0 没单独 export `UserPoolId`(只 export 了 `ClientId`)→ 牵出 Decision 2 / Decision 3。

### Decision 2: 跨 Stack reference 实现 — `Fn.import_value` + 部分用 SSM Parameter 中转

**选择**:已 export 的 4 个 Output(`RuntimeArn` / `RuntimeUrl` / `CognitoTokenEndpoint` / `CognitoClientId` / `CognitoClientSecretArn`)用 `Fn.import_value`;**Spec 0 没 export 的字段(主要是 `CognitoUserPoolId`)用 SSM Parameter 中转**。

**Why**:CDK 默认 `Fn.import_value` 需要源 stack 显式 `export_name`;Spec 0 实测每个 CfnOutput 都没显式调 `export_name=...`,CDK 会用 stack name + output key 自动生成 export 名,**但格式以 cdk.out manifest 实际为准,不能在 design 层面假设**(实施时第一步 cdk synth 后看真实 export 名)。`UserPoolId` 在 Spec 0 完全没 export → 必须给 Spec 0 一个补救途径,但**已发布 spec 不可重写**(project-conventions.md)。

替代方案:
- ❌ **回头给 Spec 0 加 `export_name`**:违反"已发布 spec 不可重写"原则
- ✅ **在 Spec 1 deploy 前先用 SSM Parameter 写一次 UserPoolId**:用 boot script(scripts/bootstrap_spec1.sh)在 Spec 0 deploy 后跑一次,通过 `aws cognito-idp list-user-pools` 找到 Spec 0 创建的 UserPool 并写到 SSM Parameter `/mcp-inspect/spec0/cognito-user-pool-id`,Spec 1 Stack 通过 `ssm.StringParameter.value_for_string_parameter()` 读取。这样 Spec 0 不变,Spec 1 多一步 boot,但完整闭环
- ⚠️ **运行时 Lookup**:用 `cognito.UserPool.from_user_pool_arn(...)` 配合 `Fn.import_value` import `RuntimeArn` 解出 region/account 再硬拼 UserPoolId — 不可行,因为 UserPoolId 不能从 Runtime ARN 算出来

**Tradeoff**:
- 多一步 boot script 增加部署链路复杂度
- 如果未来 Spec 2+ 也需要类似引用,会形成"每个新 spec 都要跑一遍 boot"的尾巴 → 在 Spec 3 框架抽象阶段考虑统一封装
- README 必须写清楚:首次部署 Spec 1 之前要运行一次 `bash scripts/bootstrap_spec1.sh` 写 SSM(后续 Spec 0 重 deploy 也无需重跑,UserPoolId 不变)

> **替代选择**:如果 tasks 阶段实施时发现 cdk.out 里 `Fn.import_value` 自动 export 名能工作,且 UserPoolId 只是恰好没 export,那么可以**仅在 Spec 1 内部建一个 SsmParameter 资源,在 boot 之外用 CFN 自动写**——但这要求 Spec 1 能**读到** UserPoolId,绕了个圈子又回到原问题。最稳还是 boot script。

### Decision 3: `McpInspectGateway` 内置 OAuth2CredentialProvider 创建

**选择**:Construct 内部一并创建 OAuth2CredentialProvider,不拆出独立 Construct。

**Why**:Spec 1 仅需 1 个 Provider,且与 Gateway 一一对应。拆开会增加调用方负担(Stack 要先建 Provider,再建 Gateway,再建 Target,3 段代码)。

**Tradeoff**:Spec 4+ 如果一个 Gateway 有多个 target(比如 RDS / Redis 各一个 Provider),需把 Provider 创建逻辑抽到独立 Construct。Spec 1 不预考虑(YAGNI)。

### Decision 4: cdk deploy 用 `--all` 还是显式 stack 名

**选择**:**README 推荐显式 `cdk deploy McpInspectSpec1Stack`**,以避免无意触发 Spec 0 的二次部署。

**Why**:Spec 0 已经 deploy 完成,二次部署虽然是 no-op 也会消耗几十秒;Spec 1 的目标是叠加,不是重新部署一切。CDK 的 dependsOn 机制保证只 deploy Spec 1 时,如果 Spec 0 没部署会 import_value 失败,**这种失败方式比"自动连带部署"更明确**(失败比错对好)。

**Tradeoff**:CI 脚本可能希望 `cdk deploy --all` 一把梭。README 提供两种命令,让使用者按场景选。

### Decision 5: target_name 用 `spec0helloworld` 而非 `spec0_hello_world`

**选择**:固定字符串 `spec0helloworld`(15 字符,无下划线)。

**Why**:Gateway 自动给 tool 名加 `{target_name}__` 前缀(双下划线分隔)。如果 target_name 含下划线,会和分隔符混淆,日后从 tool 名反向推 target_name 难度增加。**用纯小写 + 无分隔符的命名**让 `spec0helloworld__hello_world` 解析路径单一(以最后的 `__` 拆即可)。15 字符也满足 project-conventions.md A7 的"target 前缀 16 字符以内"的非正式约束。

**Tradeoff**:与 Runtime name `spec0_hello_world`(下划线版)不一致,看代码时要切两套命名风格。

## Correctness Properties

### Property 1: 链路认证完整性 Inbound

**Validates: Requirements 1.3, 5.4**

**∀** 来自 verify_spec1.py / DevOps Agent 的请求,**IF** 未携带有效 Cognito JWT(scope 含 `mcp/invoke`)**THEN** Gateway 返回 401/403,**且** 请求不被转发到 MCP target。

### Property 2: 链路认证完整性 Outbound

**Validates: Requirements 3.6, 3.7, 3.8**

**∀** Gateway 转发到 Spec 0 Runtime 的请求,**SHALL** 由 Token Vault 自动注入 `Authorization: Bearer <jwt>`;**IF** Token Vault 取 token 失败 **THEN** Gateway 返回错误,**SHALL NOT** 以未认证状态继续转发。

### Property 3: tool 命名空间稳定性

**Validates: Requirements 2.4, 2.5, 5.6**

**∀** 部署后的 Gateway,在 target 注册完成 ∧ Provider 绑定 ∧ Spec 0 Runtime 可达 三个条件全部成立时,`tools/list` 返回的列表 **SHALL** 包含且仅包含 `spec0helloworld__hello_world`(本 Spec 范围内 — 未来加 target 后不再仅一个);任一前提缺失时 **SHALL NOT** 出现该 tool。

### Property 4: tool 行为确定性

**Validates: Requirements 5.7, 5.8**

**∀** 输入 `name`,Gateway 转发的 `tools/call name=spec0helloworld__hello_world arguments={"name": <name>}` **SHALL** 返回字符串 `"Hello, {name}!"`,无状态、幂等、无副作用。

### Property 5: 资源清理可逆性与跨 Stack 边界

**Validates: Requirements 4.5, 4.6, 9.1, 9.2, 9.3**

**WHEN** 执行 `bash scripts/destroy_spec1.sh`,**THEN** Spec 1 创建的 Gateway / Target / Provider / LogGroup / IAM Role 全部删除(AWS CLI 扫描通过),**且** Spec 0 Stack 仍可独立运行,Spec 0 verify.py 仍 pass。

### Property 6: 凭据零泄露

**Validates: Requirements 4.4, 5.11**

任何阶段(synth 模板、deploy 输出、CFN Outputs、verify_spec1.py stdout)**SHALL NOT** 出现 Cognito client_secret 明文、access_token 明文、完整 Authorization header。
- `cdk synth | grep -i 'secret'` 仅匹配 ARN / Ref / Fn::GetAtt token
- `verify_spec1.py` 输出仅含 token 长度,不含 token 本身
- CFN Outputs 仅暴露 ARN / ID / URL,不暴露 secret value

### Property 7: alpha 隔离性

**Validates: Requirements 8.4**

对 codebase 执行 `grep -rn "aws_bedrock_agentcore_alpha" --include="*.py" --exclude-dir=.venv --exclude-dir=cdk.out .`,**SHALL** 仅匹配 `framework/runtime_construct.py`(Spec 0 引入)与 `framework/gateway_construct.py`(本 spec 引入)两个文件的 docstring + import 行。

### Property 8: 部署时长上界

**Validates: Requirements 1.1**

`time cdk deploy McpInspectSpec1Stack --require-approval never && time python scripts/verify_spec1.py` 总耗时 **SHALL** ≤ 15 分钟(对应 Constraints 表格中"部署 + verify 总时长 ≤ 15 分钟")。

---

自动化与人工分工:Property 1 / 2 / 3 / 4 / 6 的部分内容由 verify_spec1.py 自动断言;Property 5 / 6 全量 / 7 / 8 通过 README 中的 checklist 加 `grep` 命令与 `time` 测量人工验证。

## SHALL NOT 自检(设计层逐条对照 requirements.md)

| SHALL NOT | 设计中如何保证 |
|-----------|--------------|
| #1 不创建新 Cognito 资源 | SpecOneStack / McpInspectGateway 都不实例化 cognito.UserPool 等;只用 from_xxx_id 工厂引用 Spec 0 |
| #2 不创建多于一个 Gateway | SpecOneStack 仅实例化一次 McpInspectGateway,Construct 内仅创建一个 Gateway |
| #3 不注册除 Spec 0 之外的 target | McpInspectGateway 只调一次 add_mcp_server_target,endpoint 写死 Spec 0 RuntimeUrl |
| #4 不引入真实数据源 | mcp_servers/ 目录不动 |
| #5 不让 cdk 自动注册 Gateway 到 DevOps Agent | README 把 Agent Space 注册作为人工 checklist;代码里没有任何 Agent Space 调用 |
| #6 不跳过 Gateway | Stack 中没有任何"DevOps Agent 直连 Spec 0 Runtime"的代码路径 |
| #7 不用 NoAuth/IAM | GatewayAuthorizer.using_cognito 强制 Cognito JWT;不引用 NoAuthAuthorizer / IamAuthorizer |
| #8 出站不硬编码 token | OAuth2CredentialProvider 由 Token Vault 运行时换 token;Construct 没有任何"读 secret 然后塞 header"的代码 |
| #9 不输出 client_secret 明文 | client_secret 用 `client_secret.secret_value`(SecretValue token,部署期解析);Stack Output 不暴露 secret value |
| #10 alpha 类型不外漏 | McpInspectGateway 参数全是 str / SecretValue / IUserPool / IUserPoolClient(stable);属性全是 str |
| #11 不直接用 L1 Cfn* | 用 L2 Gateway / GatewayTarget / OAuth2CredentialProvider;只在 LogGroup delivery 兜底时才考虑 escape hatch(tasks 阶段决策) |
| #12 不硬编码跨 stack ARN | 所有引用走 Fn.import_value / SSM Parameter / from_xxx_id,无字面量 ARN |
| #13 Gateway 执行角色无过权限 | role= 不传则 L2 默认最小权限;若 L2 默认角色不达标,tasks 阶段切自定义 Role |
| #14 RemovalPolicy.DESTROY | Gateway / Provider / Target / LogGroup 全部 apply_removal_policy(DESTROY) |
| #15 完整 traceback | verify_spec1.py 顶层 try/except + traceback.print_exc(),不只输出 pass/fail |
| #16 destroy 不连带 Spec 0 | destroy_spec1.sh 只执行 `cdk destroy McpInspectSpec1Stack`;残留扫描只查 Spec 1 资源 |
| #17 不打印 token / secret 明文 | verify_spec1.py 的 print 仅含 token 长度、status code、tool 名,不含完整 token / secret / Authorization 值 |

## 风险与未决项

| 风险 | 影响 | 缓解 |
|------|------|------|
| `OAuth2CredentialProvider.using_cognito` 实测签名与 design 推测不一致(Spec 0 已踩过 `RuntimeAuthorizerConfiguration.using_cognito` 位置参数的坑) | Construct 编译失败 | tasks 阶段第一个 task 加"实证 alpha 签名"步骤,以 .venv jsii 实际签名为准修正 design 代码骨架 |
| Spec 0 Output 没有显式 `export_name` | `Fn.import_value` 失败 | D2 已给方案:用 SSM Parameter 中转 + boot script;先 cdk synth 看实际 export 名再决定 |
| `gateway.gateway_id` / `gateway.gateway_url` / `credential_provider.credential_provider_arn` 属性名实测不存在 | CfnOutput 取值失败 | 第一个 task 实证 alpha 类的 attribute,按实测改 |
| Gateway L2 是否支持 `logging_configs` 参数(Spec 0 Runtime L2 支持但 Gateway 实测签名里没有) | App logs 没自动配 delivery | tasks 阶段补 L1 escape hatch 创建 DeliverySource + DeliveryDestination,关联到本 spec 创建的 LogGroup |
| Gateway 资源类型名 / domain 全局唯一约束 | destroy 后短期重 deploy 撞名 | gateway_name 加 sha hash 后缀(类似 Spec 0 domain prefix 做法);domain 由 alpha L2 自管 |
| AWS CLI `bedrock-agentcore-control` 子命令名 | destroy_spec1.sh 残留扫描命令报 unknown subcommand | 第一次部署后用 `aws bedrock-agentcore-control help` 查实际命令名,修脚本 |
| Agent Space UI 路径变化 | README checklist 步骤过时 | README 标注"以 AWS 控制台实际界面为准";截图不进 git,只放 step-by-step 文字 |
| target tool 名前缀分隔符是 `__` 还是别的 | verify_spec1.py 断言失败 | 第一次 deploy 后 `tools/list` 看真实返回,以实测为准修正 verify_spec1.py 的断言字符串 |
