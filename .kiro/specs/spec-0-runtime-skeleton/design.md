# Design Document

## Overview

Spec 0 在空仓库中搭建第一个 CDK Python 项目，部署到 us-east-1：

- 一个 ECR 仓库（CDK asset 自动创建）
- 一个最小 Python MCP server 容器镜像（FastMCP，linux/arm64）
- 一个 AgentCore Runtime（Public Network、MCP 协议、JWT 认证）
- 一套 Cognito M2M 认证（User Pool + Resource Server + Client + Secret 存 Secrets Manager）
- 一个本地 verify 脚本（`scripts/verify.py`）端到端调通 `tools/list`

设计核心目标：
1. **链路打通优先**：先证明 Runtime + Cognito + MCP 协议三方协作正确
2. **Construct 抽象就位**：把 alpha API 封装在 `framework/runtime_construct.py` 中，对外只暴露 `source_path` / `runtime_name`，未来 alpha breaking change 只改一处
3. **零硬编码**：所有跨资源引用走 CDK token，不在代码中写 ARN / endpoint

## Architecture

### 部署后运行时架构

```
┌─────────────────────────────────────────────────────────────────┐
│  Local Dev (M4 Mac, arm64)                                      │
│  ┌──────────────────┐                                           │
│  │ scripts/verify.py│                                           │
│  └────────┬─────────┘                                           │
│           │ 1) describe-stacks → 拿 outputs                     │
│           │ 2) GetSecretValue → 拿 client_secret                │
└───────────┼──────────────────────────────────────────────────────┘
            │ 3) POST client_credentials
            ▼
┌─────────────────────────────────────────────────────────────────┐
│  AWS us-east-1                                                   │
│                                                                  │
│  Cognito User Pool                                               │
│  ├─ Resource Server (mcp/invoke scope)                           │
│  └─ User Pool Client (M2M, client_credentials)                   │
│         │                                                        │
│         │ access_token (JWT)                                     │
│         ▼                                                        │
│  ┌────────────────────────────────────────────┐                 │
│  │ AgentCore Runtime (Public Network)         │                 │
│  │  ├─ Inbound Auth: Cognito JWT              │                 │
│  │  ├─ Protocol: MCP (Streamable HTTP)        │                 │
│  │  ├─ Container: linux/arm64                 │                 │
│  │  │   FastMCP + hello_world tool            │                 │
│  │  ├─ Tracing: X-Ray ON                      │                 │
│  │  └─ Logs: CloudWatch /aws/bedrock-agentcore│                 │
│  └────────────────────────────────────────────┘                 │
│                                                                  │
│  Secrets Manager: cognito-m2m-secret/<rand>                      │
│  ECR: cdk-asset-{hash} (CDK 自动管理)                            │
└─────────────────────────────────────────────────────────────────┘
```

### 部署期资源依赖

```
SpecZeroStack
 ├─ CognitoStack（同 stack 内的 nested 逻辑分组，非 NestedStack）
 │   ├─ UserPool
 │   ├─ ResourceServer ────────────┐
 │   ├─ UserPoolClient ────────────┤── 依赖关系
 │   └─ Secret (client_secret)     │
 │                                 │
 ├─ Runtime (McpInspectRuntime)    │
 │   ├─ AgentRuntimeArtifact.fromAsset(mcp-servers/hello-world/)
 │   │     └─ ECR auto-created
 │   ├─ ExecutionRole (最小权限)
 │   ├─ AuthorizerConfig (referenced from Cognito) ←
 │   ├─ X-Ray tracing
 │   └─ CloudWatch LogGroup
 │
 └─ CfnOutputs（11 个）
     ├─ RuntimeArn
     ├─ RuntimeUrl
     ├─ CognitoTokenEndpoint
     ├─ CognitoClientId
     ├─ CognitoOAuthScope
     ├─ CognitoClientSecretArn
     └─ ...
```

## 项目目录结构

```
aws-devops-mcp-platform/
├─ .kiro/
│   └─ specs/
│       └─ spec-0-runtime-skeleton/
│           ├─ requirements.md
│           ├─ design.md          ← 本文档
│           └─ tasks.md           ← 后续生成
├─ app.py                         ← CDK app 入口
├─ cdk.json                       ← CDK 配置
├─ requirements.txt               ← Python 依赖（CDK + mcp + boto3）
├─ requirements-dev.txt           ← 开发依赖（pytest 等，留空也行）
├─ .gitignore
├─ .dockerignore                  ← 镜像构建排除清单（容器目录会有自己的）
├─ README.md                      ← 部署 / 验证 / 清理说明
│
├─ stacks/
│   ├─ __init__.py
│   └─ spec_zero_stack.py         ← 主 Stack：拼装 Cognito + Runtime + Outputs
│
├─ framework/                     ← 框架层（未来 spec 复用）
│   ├─ __init__.py
│   └─ runtime_construct.py       ← McpInspectRuntime Construct
│
├─ mcp_servers/
│   └─ hello_world/
│       ├─ Dockerfile             ← linux/arm64
│       ├─ .dockerignore
│       ├─ requirements.txt       ← mcp, fastapi/starlette（FastMCP 依赖）
│       └─ main.py                ← FastMCP + hello_world tool
│
└─ scripts/
    ├─ verify.py                  ← 端到端验证脚本
    └─ destroy.sh                 ← 清理脚本（cdk destroy + 兜底删除残留）
```

## Components and Interfaces

### Component 1: `mcp_servers/hello_world/main.py`

最小 MCP server。

```python
from mcp.server.fastmcp import FastMCP

mcp = FastMCP(host="0.0.0.0", port=8080, stateless_http=True)

@mcp.tool()
def hello_world(name: str) -> str:
    """Greet someone by name. Spec 0 link-check tool."""
    return f"Hello, {name}!"

if __name__ == "__main__":
    mcp.run(transport="streamable-http")
```

**`/ping` 健康检查**：FastMCP 在 streamable-http 模式下会暴露 `/health`，但 AgentCore Runtime 要求 `/ping`。我们用 Starlette middleware 加一个 alias：

```python
from starlette.responses import PlainTextResponse
from starlette.routing import Route

async def ping(_): return PlainTextResponse("ok")

# FastMCP 暴露底层 starlette app，注入额外路由
mcp.streamable_http_app.routes.append(Route("/ping", ping))
```

> 注：FastMCP 的内部路由对象访问方式以实际 SDK 1.27 为准；如果属性名不同，task 实施时按实际 API 调整，但语义不变（在底层 ASGI 应用上注册 `/ping`）。

### Component 2: `mcp_servers/hello_world/Dockerfile`

```dockerfile
FROM --platform=linux/arm64 public.ecr.aws/docker/library/python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .
EXPOSE 8080
CMD ["python", "main.py"]
```

**`.dockerignore`（容器目录内）**：
```
.env
.aws/
__pycache__/
*.pyc
.git/
.kiro/
```

### Component 3: `framework/runtime_construct.py` —— `McpInspectRuntime`

框架核心 Construct。**alpha 类型只在内部出现**，对外只暴露稳定参数。

```python
from pathlib import Path
from typing import Optional
from aws_cdk import (
    Duration, RemovalPolicy, CfnOutput,
    aws_cognito as cognito,
    aws_secretsmanager as secretsmanager,
    aws_logs as logs,
)
from aws_cdk import aws_bedrock_agentcore_alpha as agentcore  # alpha 仅在此文件
from constructs import Construct


class McpInspectRuntime(Construct):
    """框架核心 Construct：Runtime + Cognito M2M + Logging 一站式封装。

    Spec 0：Public Network only。Spec 2 扩展 VPC 模式（不 break 本 API）。

    Outputs (作为属性暴露)：
    - runtime_arn: str (token)
    - runtime_url: str (token，已拼接 invocation URL)
    - token_endpoint: str (token)
    - client_id: str (token)
    - oauth_scope: str (固定 "{resource_server}/invoke")
    - client_secret: secretsmanager.ISecret
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        source_path: str | Path,    # MCP server 目录（含 Dockerfile）
        runtime_name: str,           # 必须满足 [a-zA-Z][a-zA-Z0-9_]{0,47}
        resource_server_id: str = "mcp",
        scope_name: str = "invoke",
    ) -> None:
        super().__init__(scope, construct_id)

        # 1) Cognito User Pool + Resource Server + M2M Client
        self._user_pool = cognito.UserPool(
            self, "UserPool",
            removal_policy=RemovalPolicy.DESTROY,
            deletion_protection=False,
            self_sign_up_enabled=False,
        )
        domain = self._user_pool.add_domain(
            "Domain",
            cognito_domain=cognito.CognitoDomainOptions(
                domain_prefix=f"{runtime_name.lower().replace('_','-')}-{self._stack_short_id()}"
            ),
        )
        scope_obj = cognito.ResourceServerScope(
            scope_name=scope_name, scope_description="Invoke MCP runtime"
        )
        resource_server = self._user_pool.add_resource_server(
            "ResourceServer",
            identifier=resource_server_id,
            scopes=[scope_obj],
        )
        client = self._user_pool.add_client(
            "M2MClient",
            generate_secret=True,
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(client_credentials=True),
                scopes=[
                    cognito.OAuthScope.resource_server(resource_server, scope_obj),
                ],
            ),
            access_token_validity=Duration.hours(1),
        )

        # 2) Client secret 存到 Secrets Manager（Cognito 不直接给 ARN，自己包装）
        self._secret = secretsmanager.Secret(
            self, "ClientSecret",
            removal_policy=RemovalPolicy.DESTROY,
            secret_string_value=client.user_pool_client_secret,  # CDK token
        )

        # 3) Runtime artifact 从本地 Dockerfile 构建（CDK 自动建 ECR + buildx arm64）
        artifact = agentcore.AgentRuntimeArtifact.from_asset(
            str(Path(source_path).resolve())
        )

        # 4) Runtime + Cognito JWT authorizer + tracing + logs
        log_group = logs.LogGroup(
            self, "LogGroup",
            log_group_name=f"/aws/bedrock-agentcore/{runtime_name}",
            removal_policy=RemovalPolicy.DESTROY,
            retention=logs.RetentionDays.ONE_WEEK,
        )

        self._runtime = agentcore.Runtime(
            self, "Runtime",
            runtime_name=runtime_name,
            agent_runtime_artifact=artifact,
            protocol_configuration=agentcore.ProtocolType.MCP,
            authorizer_configuration=agentcore.RuntimeAuthorizerConfiguration.using_cognito(
                user_pool=self._user_pool,
                user_pool_clients=[client],
                allowed_scopes=[f"{resource_server_id}/{scope_name}"],
            ),
            tracing_enabled=True,
            logging_configs=[{
                "log_type": agentcore.LogType.APPLICATION_LOGS,
                "destination": agentcore.LoggingDestination.cloud_watch_logs(log_group),
            }],
        )

        # 5) 暴露稳定属性（供 Stack 用来生成 Outputs）
        self.runtime_arn = self._runtime.runtime_arn
        self.runtime_url = self._build_runtime_url(self._runtime.runtime_arn)
        self.token_endpoint = (
            f"https://{domain.domain_name}.auth.us-east-1.amazoncognito.com/oauth2/token"
        )
        self.client_id = client.user_pool_client_id
        self.oauth_scope = f"{resource_server_id}/{scope_name}"
        self.client_secret = self._secret

    @staticmethod
    def _build_runtime_url(runtime_arn: str) -> str:
        """构建 invocation URL。CDK 部署期 token，运行时被替换。"""
        # encoded_arn = arn.replace(':', '%3A').replace('/', '%2F')
        # url = f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{encoded_arn}/invocations?qualifier=DEFAULT"
        # 由于 ARN 是 token，需要在 deploy 后由 verify 脚本动态拼接，或用 Fn::Sub。
        # 实施时用 Fn::Join + Fn::Split 在 CFN 端构造。
        # 详见 task 实施。
        ...

    def _stack_short_id(self) -> str:
        # 用 Stack name 哈希后取 8 位，保证 Cognito domain prefix 全局唯一
        ...
```

> **关键设计点：alpha 隔离**
> - `aws_bedrock_agentcore_alpha` 的 import 只出现在 `framework/runtime_construct.py`
> - `stacks/spec_zero_stack.py` 与 `scripts/verify.py` 都不引用 alpha 包
> - 未来 alpha API breaking change 时，只需改这一个文件

### Component 4: `stacks/spec_zero_stack.py`

```python
from pathlib import Path
from aws_cdk import Stack, CfnOutput
from constructs import Construct
from framework.runtime_construct import McpInspectRuntime


class SpecZeroStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        runtime = McpInspectRuntime(
            self, "HelloWorldRuntime",
            source_path=Path(__file__).parent.parent / "mcp_servers/hello_world",
            runtime_name="spec0_hello_world",
        )

        # 11 个 outputs
        CfnOutput(self, "RuntimeArn",            value=runtime.runtime_arn)
        CfnOutput(self, "RuntimeUrl",            value=runtime.runtime_url)
        CfnOutput(self, "CognitoTokenEndpoint",  value=runtime.token_endpoint)
        CfnOutput(self, "CognitoClientId",       value=runtime.client_id)
        CfnOutput(self, "CognitoOAuthScope",     value=runtime.oauth_scope)
        CfnOutput(self, "CognitoClientSecretArn",value=runtime.client_secret.secret_arn)
        CfnOutput(self, "Region",                value=self.region)
```

### Component 5: `app.py`

```python
#!/usr/bin/env python3
import aws_cdk as cdk
from stacks.spec_zero_stack import SpecZeroStack

app = cdk.App()
SpecZeroStack(
    app, "McpInspectSpec0Stack",
    env=cdk.Environment(region="us-east-1"),  # account 取 CLI 凭据
)
app.synth()
```

### Component 6: `scripts/verify.py`

```python
"""Spec 0 端到端验证脚本。

读取 CFN outputs → 拉 secret → 取 token → 调 Runtime → 断言 hello_world tool 存在。
失败必须打印完整 stack trace。
"""
import json
import sys
import traceback
from urllib.parse import quote
import boto3
import requests

STACK_NAME = "McpInspectSpec0Stack"
REGION = "us-east-1"


def main():
    cf = boto3.client("cloudformation", region_name=REGION)
    sm = boto3.client("secretsmanager", region_name=REGION)

    # 1) 读 outputs
    stack = cf.describe_stacks(StackName=STACK_NAME)["Stacks"][0]
    outs = {o["OutputKey"]: o["OutputValue"] for o in stack["Outputs"]}

    # 2) 拉 client secret
    secret_value = sm.get_secret_value(SecretId=outs["CognitoClientSecretArn"])
    client_secret = secret_value["SecretString"]

    # 3) client_credentials 取 token
    token_resp = requests.post(
        outs["CognitoTokenEndpoint"],
        data={
            "grant_type": "client_credentials",
            "client_id": outs["CognitoClientId"],
            "client_secret": client_secret,
            "scope": outs["CognitoOAuthScope"],
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=10,
    )
    token_resp.raise_for_status()
    access_token = token_resp.json()["access_token"]
    print(f"✓ Cognito token acquired (length={len(access_token)})")

    # 4) MCP initialize
    runtime_url = outs["RuntimeUrl"]
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    init_payload = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "spec0-verify", "version": "0.1.0"},
        },
    }
    init_resp = requests.post(runtime_url, headers=headers, json=init_payload, timeout=15)
    init_resp.raise_for_status()
    print(f"✓ MCP initialize succeeded (status={init_resp.status_code})")

    # 5) tools/list
    list_payload = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    list_resp = requests.post(runtime_url, headers=headers, json=list_payload, timeout=15)
    list_resp.raise_for_status()
    body = list_resp.json()
    tools = [t["name"] for t in body.get("result", {}).get("tools", [])]
    print(f"✓ tools/list returned: {tools}")

    assert "hello_world" in tools, f"hello_world not found in {tools}"

    # 6) tools/call hello_world
    call_payload = {
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "hello_world", "arguments": {"name": "Spec0"}},
    }
    call_resp = requests.post(runtime_url, headers=headers, json=call_payload, timeout=15)
    call_resp.raise_for_status()
    result = call_resp.json()["result"]
    text = result["content"][0]["text"]
    assert text == "Hello, Spec0!", f"unexpected response: {text}"

    print("✅ Spec 0 verification passed")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
```

## Data Models

Spec 0 没有持久化数据。只有以下"配置数据"流转：

| 数据 | 来源 | 去向 | 形态 |
|------|------|------|------|
| Cognito client_secret | Cognito 创建时返回 | Secrets Manager | SecretString |
| Cognito access_token | client_credentials flow | HTTP Authorization header | JWT (短期，1h) |
| MCP `tools/list` 响应 | Runtime → verify | stdout | JSON-RPC 2.0 |

**MCP 协议 wire format**（Spec 0 用到的 4 个方法）：

```json
// initialize 请求
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"spec0-verify","version":"0.1.0"}}}

// tools/list 响应
{"jsonrpc":"2.0","id":2,"result":{"tools":[{"name":"hello_world","description":"...","inputSchema":{...}}]}}

// tools/call 请求
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"hello_world","arguments":{"name":"Spec0"}}}

// tools/call 响应
{"jsonrpc":"2.0","id":3,"result":{"content":[{"type":"text","text":"Hello, Spec0!"}]}}
```

## Error Handling

| 失败场景 | 表现 | 诊断信号 |
|---------|------|---------|
| `cdk synth` 失败 | jsii / Python import 错 | 终端 stack trace；通常是 alpha 包版本对不上 cdk-lib |
| Docker buildx arm64 失败 | `cdk deploy` 卡在 asset 构建 | `docker buildx ls` 检查 arm64 builder；M4 应该天然支持 |
| AgentCore Runtime 创建失败 | CFN stack 状态变 ROLLBACK | CloudWatch Logs `/aws/bedrock-agentcore/...`；通常是 Cognito 引用错或镜像 platform 错 |
| Cognito token 申请 401 | verify 脚本失败 | 检查 user pool client 是否启用 `client_credentials` 且 scope 正确 |
| MCP initialize 401 | Authorization header 被拒 | 检查 token 的 `scope` claim 是否包含 `mcp/invoke` |
| MCP initialize 200 但 `tools/list` 返回空 | hello_world 装饰器未生效 | 容器日志看 FastMCP 启动是否报错 |
| `tools/call` 返回错误 | result.isError = true | 检查 main.py 中 hello_world 实现 |

**verify 脚本错误处理原则**（meta-harness 完整 trace）：
- 全部用 `requests.raise_for_status()` 触发 HTTP 错误
- 捕获最外层 Exception，打印完整 traceback
- 不静默吞错、不只输出 pass/fail

## Testing Strategy

### Unit / 自动化测试

Spec 0 不引入单测框架。理由：
- IaC 代码本身的合成正确性由 CDK 自身保证
- MCP server 只有一个 hello_world 函数，单测投入产出比低
- 端到端验证脚本（verify.py）就是 Spec 0 的"测试"

### 端到端验证（唯一权威验证）

执行命令：
```bash
cdk deploy --require-approval never
python scripts/verify.py
```

**Pass 条件**：
- `cdk deploy` 退出码 0
- `verify.py` 末尾打印 `✅ Spec 0 verification passed` 且退出码 0

**Fail 模式归类**（任一发生即视为 Spec 0 未完成）：
1. `cdk synth` 失败
2. `cdk deploy` 失败或超过 10 分钟
3. verify 脚本任一断言失败
4. 模板包含 SHALL NOT 中禁止的资源（如 Lambda、API Gateway、`*:*` 权限）

### 清理

```bash
cdk destroy --force
```

清理后必须验证：
- Stack 状态：`DELETE_COMPLETE`
- 无残留 ECR repository（CDK asset bucket 由 bootstrap 管，不需要清理）
- 无残留 Cognito User Pool / Domain（domain prefix 全局唯一，删错可能影响下次部署）

## Decisions and Tradeoffs

### Decision 1：用 CDK Python 而非 TypeScript

**选择**：CDK Python（`aws_cdk.aws_bedrock_agentcore_alpha`）

**理由**：
- 王总不熟 TS，团队后续 MCP server 必然用 Python
- 避免 IaC（TS）与应用代码（Py）双语言维护
- CDK Python 与 TS 功能等价（jsii 自动生成）

**Tradeoff**：CDK 官方文档示例 90% 是 TS，看文档要脑补；但代码由我写，王总看代码不看文档，这个代价由我承担

### Decision 2：alpha 包必须与 cdk-lib 主包配对升级

**选择**：精确锁 `aws-cdk-lib==2.254.0` + `aws-cdk.aws-bedrock-agentcore-alpha==2.254.0a0`

**理由**：alpha 包的版本号设计（2.254.0a0）就是为了和主包配对，主包升必须同步升 alpha；混用版本会出 jsii 兼容性问题

**Tradeoff**：未来升级要两个一起升，但这是 alpha 包的天然约束，不是设计选择

### Decision 3：`McpInspectRuntime` Construct 内 alpha 类型零外漏

**选择**：alpha 包 import 只出现在 `framework/runtime_construct.py`，Stack / 脚本不感知 alpha

**理由**：alpha API 可能 breaking change，隔离影响面到一个文件

**Tradeoff**：Construct API 设计要更克制（不能为了图方便把 alpha 类型作为参数暴露出去），但这正是框架质量的体现

### Decision 4：Cognito Client Secret 主动包装到 Secrets Manager

**选择**：CDK 创建独立 `secretsmanager.Secret`，把 Cognito 返回的 secret 存进去

**理由**：
- Cognito User Pool Client 的 secret 不能直接通过 ARN 引用（不是 Secrets Manager 资源）
- verify 脚本和未来 Spec 1 的 OAuth2CredentialProvider 都需要标准 ARN 来取值
- 统一凭据管理界面（避免多套 API）

**Tradeoff**：多了一个 Secrets Manager 资源（成本可忽略，约 $0.4/month），但换来 API 一致性

### Decision 5：Public Network 而非 VPC

**选择**：Spec 0 用 Public Network

**理由**：
- Spec 0 不接真实数据源，VPC 不必要
- VPC 模式涉及 subnet / SG 决策，留给 Spec 2 处理（要连 RDS 时再说）
- 减少 Spec 0 的认知负担

**Tradeoff**：Spec 2 升级 VPC 时 `McpInspectRuntime` Construct API 要扩展（加 `vpc` 参数），但属于增量扩展，不破坏 Spec 0 调用

### Decision 6：Stack 命名 `McpInspectSpec0Stack`

**选择**：固定 stack 命名 + 编号

**理由**：
- 每个 spec 自己的 stack 隔离部署
- Spec 1 引入 Gateway 时新建 `McpInspectSpec1Stack`（或合并复用 Spec 0 stack 由 Spec 1 决策）
- `cdk destroy McpInspectSpec0Stack` 操作清晰

**Tradeoff**：未来跨 spec 共享资源（如 Cognito User Pool）时要用 cross-stack reference，但这是后期问题

### Decision 7：固定 region us-east-1

**选择**：硬编码 `env=cdk.Environment(region="us-east-1")`

**理由**：王总确认；AgentCore us-east-1 GA 最早最稳

**Tradeoff**：未来要多 region 时改 app.py 一处即可；不影响 Construct 设计

## Correctness Properties

形式化 Spec 0 必须满足的执行级正确性属性。每条都对应 verify.py 或人工 checklist 可观测的事实。

### Property 1: 链路认证完整性

**Validates: Requirements 3.2, 4.3, 5.4**

**∀** 来自 verify.py 的请求，**IF** 请求未携带有效 Cognito JWT（`scope` 包含 `mcp/invoke`）**THEN** Runtime 返回 401/403，**且** MCP server 容器日志中不出现该请求的处理记录。

验证方式：手工实验，删去 Authorization header 调用一次，断言失败响应。

### Property 2: tool 名空间稳定性

**Validates: Requirements 2.3, 5.6**

**∀** 部署后的 Runtime，调用 `tools/list` 返回的 tools 列表 **SHALL** 严格等于 `["hello_world"]`（既不多也不少）。

验证方式：verify.py 第 5 步断言。

### Property 3: tool 行为确定性

**Validates: Requirements 2.4**

**∀** 输入 `name`，调用 `hello_world(name)` **SHALL** 返回字符串 `"Hello, {name}!"`，无状态、幂等、无副作用。

验证方式：verify.py 第 6 步断言。可加多次调用同一输入返回相同结果的检查。

### Property 4: 资源清理可逆性

**Validates: Requirements 4.6**

**WHEN** 执行 `cdk destroy --force`，**THEN** stack 状态变为 `DELETE_COMPLETE`，**且** 不留下：
- Cognito User Pool / User Pool Domain
- Secrets Manager Secret
- AgentCore Runtime / RuntimeEndpoint
- CloudWatch LogGroup `/aws/bedrock-agentcore/spec0_hello_world`

验证方式：destroy 后用 `aws cognito-idp list-user-pools` / `aws bedrock-agentcore list-agent-runtimes` 检查无残留。

### Property 5: 凭据零泄露

**Validates: Requirements 4.4, 5.2**

任何阶段（synth 模板、deploy 输出、CloudFormation Outputs、verify 脚本 stdout）**SHALL NOT** 出现 Cognito client_secret 明文。

验证方式：
- `cdk synth | grep -i 'secret'` 只能匹配到 ARN，不能出现 plaintext
- verify.py 的输出只包含 token 长度，不含 token 本身
- CloudFormation Outputs 中只暴露 Secret ARN

### Property 6: alpha 隔离性

**Validates: Requirements 1.2**

对 codebase 执行 `grep -r "aws_bedrock_agentcore_alpha" --include="*.py"`，**SHALL** 仅匹配 `framework/runtime_construct.py` 一个文件。

验证方式：CI / 人工 grep。这条性质保证未来 alpha breaking change 的影响面收敛（对应 Requirement 1.2 中"包含且仅包含"的资源清洁度要求，引申到代码层面的架构清洁度）。

### Property 7: 平台正确性

**Validates: Requirements 2.1**

最终推送到 ECR 的镜像 **SHALL** 是 `linux/arm64` 单平台镜像。

验证方式：`docker buildx imagetools inspect <ecr-uri>` 显示 platform 为 `linux/arm64`。

### Property 8: 部署时长上界

**Validates: Requirements 1.1**

从 `cdk deploy` 开始到 verify.py 完成，**SHALL** ≤ 10 分钟（首次冷启包含镜像构建上传）。

验证方式：`time cdk deploy && time python scripts/verify.py`。对应 Requirement 1.1 关于 `cdk synth` / `cdk deploy` 在干净环境中可执行性的隐含时间约束（详见 Constraints 表格中"单 stack 部署时长 ≤ 10 分钟"）。

---

这 8 条性质中，Property 2 / Property 3 / Property 5（部分）由 verify.py 自动断言；Property 1 / Property 4 / Property 5（全量）/ Property 6 / Property 7 / Property 8 通过 README 中的 checklist 人工验证。Spec 3（框架抽象）阶段可以考虑把 Property 1 / Property 4 / Property 6 / Property 7 也自动化。

## SHALL NOT 自检（设计层）

逐条验证设计满足 requirements.md 的 SHALL NOT：

| SHALL NOT | 设计中如何保证 |
|-----------|--------------|
| 不引入 Gateway | Stack 中无 `agentcore.Gateway` 引用 |
| 不接真实数据源 | mcp_servers 目录下只有 hello_world，无 DB driver 依赖 |
| 不暴露多个 tool | main.py 中只有一个 `@mcp.tool()` 装饰器 |
| 不用 stdio/SSE-only | FastMCP 显式 `transport="streamable-http"` |
| 不用 Lambda+APIGW | Stack 中无 Lambda / API Gateway 资源 |
| 不用 NoAuth | `RuntimeAuthorizerConfiguration.using_cognito(...)` 强制 JWT |
| 不直接用 L1 | Stack / Construct 中无 `Cfn*` 类引用（除非降级，本 spec 不需要）|
| alpha 类型不外漏 | alpha import 只在 `framework/runtime_construct.py` |
| 不硬编码 region/account | region 从 `cdk.Environment` 注入；account 从 CLI 凭据；其他从 token |
| 不过权限 | Runtime 执行角色由 L2 自动赋予最小权限（ECR pull / Logs / X-Ray），无 managed policy |
| 不复制本地凭据 | `mcp_servers/hello_world/.dockerignore` 显式排除 |
| 不跳过 arm64 | Dockerfile 第一行 `FROM --platform=linux/arm64 ...` |
| 不硬编码 secret | verify.py 通过 `secretsmanager:GetSecretValue` 动态拉取 |
| 不用 RETAIN | UserPool / Secret / LogGroup 全部 `RemovalPolicy.DESTROY` |
| trace 完整 | verify.py 用 `traceback.print_exc()` |

## 风险与未决项

| 风险 | 影响 | 缓解 |
|------|------|------|
| FastMCP 1.27 的 `streamable_http_app.routes` 私有 API 可能不存在或改名 | `/ping` 健康检查注册失败 | tasks 阶段实施时按实际 SDK 调整；备选方案：用 `app.middleware('http')` 拦截 `/ping` |
| CDK alpha 2.254.0a0 的 `Runtime` API 字段名与 TS 文档不一致 | Construct 编译失败 | tasks 阶段先做一个最小 `cdk synth` 跑通的 spike，确认 API；遇到不一致用 jsii 生成的 Python 类型签名为准 |
| Cognito domain prefix 全局唯一，删错会影响重新部署 | 重新部署失败 | domain prefix 用 stack id 哈希后缀；destroy 后等 24h 再重 deploy（或用全新 prefix）|
| AgentCore Runtime invocation URL 格式可能在 alpha 期变化 | verify 脚本调用失败 | URL 拼接逻辑封装在 Construct 内；如格式变化只改一处 |
| arm64 镜像在某些 base image 上拉取慢 | 部署时间超过 10 分钟约束 | 用 `public.ecr.aws/docker/library/python:3.13-slim`（AWS ECR Public，arm64 原生）|
