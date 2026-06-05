# Implementation Plan

## Overview

Spec 0 拆成 6 个任务，按 meta-harness "3-7 task / 100-500 行 / 可一个 PR 完成" 原则切分。

**最终验证条件**（不变）：
```bash
cdk deploy --require-approval never && python scripts/verify.py
```
末尾打印 `✅ Spec 0 verification passed` 且退出码 0 = pass。

**任务设计原则**：
- 每个任务有明确"完成判据"（不是抽象的"实现 XX"，而是可观测的事实）
- 每个任务标注覆盖的 Requirement / Correctness Property / SHALL NOT 编号，形成可追溯链路
- Task 2 与 Task 3 互不依赖可并行；其余严格顺序
- 二次规则：同一问题修 2 次未解决，停下回去改 spec，不要做第 3 次尝试

## Tasks

- [x] 1. 搭建 CDK Python 项目骨架与依赖锁定
  - 创建仓库根目录文件：`app.py`（最小 cdk.App 入口，先创建一个空 Stack 占位）、`cdk.json`（指定 `python3 app.py` 命令、固定 `aws:cdk:enable-region-info` 等 feature flag）、`.gitignore`（排除 `cdk.out/`、`__pycache__/`、`.venv/`、`*.egg-info/`）
  - 创建 `requirements.txt`，精确锁定：`aws-cdk-lib==2.254.0`、`aws-cdk.aws-bedrock-agentcore-alpha==2.254.0a0`、`constructs>=10.0.0,<11.0.0`、`boto3>=1.34`、`requests>=2.31`
  - 创建 `requirements-dev.txt`（暂留空注释占位，避免 Spec 3 时再改一次）
  - 创建 Python 包结构：`stacks/__init__.py`、`framework/__init__.py`、`mcp_servers/hello_world/`、`scripts/`（每个目录配 `__init__.py` 或 `.gitkeep`）
  - 在仓库根创建 `.dockerignore`（虽然容器目录里也会有一份，根目录这份是 CDK asset 默认查找位置的兜底，排除 `.kiro/`、`.git/`、`cdk.out/`、`.venv/`）
  - 验证：在干净虚拟环境执行 `python -m pip install -r requirements.txt && cdk synth`，能成功合成出空 stack 模板（无错误，无 alpha 版本不匹配警告）
  - _Requirements: 1.1, 1.4_
  - _SHALL NOT covered: #9（不硬编码 region/account，cdk.json 走 env from CLI）_

- [x] 2. 实现 hello-world MCP server 容器与本地构建
  - 创建 `mcp_servers/hello_world/main.py`：FastMCP 实例 `host=0.0.0.0 port=8080 stateless_http=True`，注册 `@mcp.tool()` 装饰的 `hello_world(name: str) -> str`，调用 `mcp.run(transport="streamable-http")`
  - 在 `main.py` 中注入 `/ping` 路由：用 starlette `Route("/ping", lambda _: PlainTextResponse("ok"))` 注册到 FastMCP 暴露的底层 ASGI app；如果 SDK 1.27 的 `streamable_http_app.routes` 属性名不同，回退方案是用 `app.middleware('http')` 在中间件层拦截 `/ping`（实施时按 SDK 实际 API 选其一，文档化在 main.py 注释里）
  - 创建 `mcp_servers/hello_world/requirements.txt`：精确锁 `mcp==1.27.1`、`starlette>=0.40,<1.0`、`uvicorn>=0.30,<1.0`
  - 创建 `mcp_servers/hello_world/Dockerfile`：`FROM --platform=linux/arm64 public.ecr.aws/docker/library/python:3.13-slim`，`WORKDIR /app`，复制 `requirements.txt` 并 `pip install`，复制 `main.py`，`EXPOSE 8080`，`CMD ["python", "main.py"]`
  - 创建 `mcp_servers/hello_world/.dockerignore`：包含 `.env`、`.aws/`、`__pycache__/`、`*.pyc`、`.git/`、`.kiro/`
  - 验证：执行 `docker buildx build --platform linux/arm64 -t hello-world-mcp:spec0 mcp_servers/hello_world/`，构建成功；`docker run --rm -p 8080:8080 hello-world-mcp:spec0` 后另一终端 `curl http://localhost:8080/ping` 返回 `ok` 且状态码 200
  - 验证：用 Python 客户端（`mcp.client.streamable_http`）本地调用 `tools/list` 返回 `["hello_world"]`，调 `tools/call` 输入 `name=World` 返回 `"Hello, World!"`
  - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6_
  - _Correctness Property: Property 3, Property 7_
  - _SHALL NOT covered: #4（不用 stdio/SSE-only）、#11（.dockerignore 排除凭据）、#12（不跳过 arm64）_

- [x] 3. 实现 `McpInspectRuntime` Construct（alpha 隔离 + Cognito + Runtime 一站式封装）
  - 创建 `framework/runtime_construct.py`：定义 `class McpInspectRuntime(Construct)`；**alpha 包 `aws_bedrock_agentcore_alpha` 的 import 仅出现在本文件**
  - Construct 接收稳定参数：`source_path`、`runtime_name`、`resource_server_id="mcp"`、`scope_name="invoke"`（不接收任何 alpha 类型）
  - 内部按以下顺序创建资源：(a) Cognito UserPool（`removal_policy=DESTROY`、`deletion_protection=False`、`self_sign_up_enabled=False`），(b) UserPool Domain（`domain_prefix` 用 `runtime_name` + Stack name 哈希后 8 位保证全局唯一），(c) ResourceServer（id=`resource_server_id`，scope=`scope_name`），(d) UserPoolClient（`generate_secret=True`、`oauth.flows.client_credentials=True`、其他 grant 全部 disable、`access_token_validity=Duration.hours(1)`）
  - 把 Cognito UserPoolClient 返回的 `user_pool_client_secret`（CDK token）包到独立 `secretsmanager.Secret`（`removal_policy=DESTROY`），暴露 `client_secret: ISecret` 属性
  - 用 `agentcore.AgentRuntimeArtifact.from_asset(source_path)` 创建 artifact，触发 CDK 的 ARM64 镜像构建+ECR 推送
  - 创建 LogGroup `/aws/bedrock-agentcore/{runtime_name}`（`retention=ONE_WEEK`，`removal_policy=DESTROY`）
  - 创建 `agentcore.Runtime`：`protocol_configuration=MCP`、`authorizer_configuration=using_cognito(user_pool, [client], allowed_scopes=["{rs}/{scope}"])`、`tracing_enabled=True`、`logging_configs=[APPLICATION_LOGS → log_group]`
  - 暴露稳定属性给 Stack 用：`runtime_arn`、`runtime_url`（用 `Fn.join` + `Fn.split` 在 CFN 端拼接 `https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{encoded-arn}/invocations?qualifier=DEFAULT`）、`token_endpoint`、`client_id`、`oauth_scope`、`client_secret`
  - 不主动创建执行角色：依赖 `agentcore.Runtime` L2 自动创建的最小权限角色（仅 ECR pull、CloudWatch Logs 写、X-Ray 写）；如 L2 默认角色含 managed policy，task 实施时切换到 `executionRole=` 自定义最小权限 Role
  - 验证：`cdk synth` 输出的 CloudFormation 模板中，资源类型仅包含 `AWS::ECR::Repository`（CDK asset 自动创建）、`AWS::Cognito::UserPool`、`AWS::Cognito::UserPoolDomain`、`AWS::Cognito::UserPoolResourceServer`、`AWS::Cognito::UserPoolClient`、`AWS::SecretsManager::Secret`、`AWS::Logs::LogGroup`、`AWS::IAM::Role` (执行角色)、`AWS::BedrockAgentCore::Runtime`；模板大小 < 50 KB
  - 验证：`grep -r "aws_bedrock_agentcore_alpha" --include="*.py" .` 仅匹配 `framework/runtime_construct.py` 一行
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 4.1, 4.2, 4.3, 4.4, 4.6, 6.1, 6.2, 6.3_
  - _Correctness Property: Property 6_
  - _SHALL NOT covered: #6（强制 Cognito JWT，不用 NoAuth）、#7（优先 L2 alpha API）、#8（alpha 类型不外漏）、#10（最小权限）、#14（不用 RETAIN）_

- [x] 4. 拼装 `SpecZeroStack` 与 `app.py` 入口，输出 7 个 CfnOutput
  - 创建 `stacks/spec_zero_stack.py`：`class SpecZeroStack(Stack)`，实例化 `McpInspectRuntime(self, "HelloWorldRuntime", source_path=Path(__file__).parent.parent/"mcp_servers/hello_world", runtime_name="spec0_hello_world")`
  - 添加 7 个 `CfnOutput`：`RuntimeArn`、`RuntimeUrl`、`CognitoTokenEndpoint`、`CognitoClientId`、`CognitoOAuthScope`、`CognitoClientSecretArn`、`Region`（取 `self.region`）；**禁止**输出 client secret 明文，只输出 ARN
  - 更新 `app.py`：`SpecZeroStack(app, "McpInspectSpec0Stack", env=cdk.Environment(region="us-east-1"))`（account 从 CLI 凭据自动获取）
  - 验证：`cdk synth` 通过；`cdk synth --json | python -c "import json,sys; t=json.load(sys.stdin); print(list(t.get('Outputs',{}).keys()))"` 列出全部 7 个 output key
  - 验证：`cdk synth | grep -i 'secret'` 仅匹配到 `Ref: ...ClientSecretArn` 这类引用，**不出现明文 secret**
  - 验证：`cdk synth` 模板中 stack name 为 `McpInspectSpec0Stack`，region 字段固定 `us-east-1`，不出现其他硬编码 ARN/account
  - _Requirements: 1.2, 3.5, 4.5_
  - _Correctness Property: Property 5_
  - _SHALL NOT covered: #9（不硬编码）、#13（secret 不进 stdout/output 明文）_

- [x] 5. 实现 `scripts/verify.py` 端到端验证脚本
  - 创建 `scripts/verify.py`：模块级常量 `STACK_NAME = "McpInspectSpec0Stack"`、`REGION = "us-east-1"`
  - 实现 `main()` 函数，按以下步骤串联：(1) `boto3.client("cloudformation").describe_stacks()` 读 outputs 字典；(2) `boto3.client("secretsmanager").get_secret_value(SecretId=outs["CognitoClientSecretArn"])` 取 client_secret；(3) POST `outs["CognitoTokenEndpoint"]` 用 `client_credentials` flow 取 access_token，请求体 form-urlencoded `grant_type/client_id/client_secret/scope`，timeout=10；(4) POST `outs["RuntimeUrl"]` 发送 MCP `initialize`（protocolVersion=`2025-03-26`、capabilities={}、clientInfo），Authorization Bearer + Accept `application/json, text/event-stream`，timeout=15；(5) POST `tools/list`；(6) POST `tools/call name=hello_world arguments={"name":"Spec0"}`
  - 每步用 `requests.raise_for_status()` 触发 HTTP 错误，每步用 `print(f"✓ ...")` 输出进度（**只打印 token 长度，不打印 token 本身**）
  - 断言：`tools/list` 返回包含且仅包含 `hello_world`；`tools/call` 返回 `text == "Hello, Spec0!"`
  - 全部通过后打印 `✅ Spec 0 verification passed`，`sys.exit(0)`
  - 顶层 `try/except Exception` 捕获，`traceback.print_exc()` 输出完整 trace 后 `sys.exit(1)`（不能只打印 pass/fail）
  - 验证：本地干跑（先不 deploy）—— `python scripts/verify.py` 应在第一步因 `Stack ... does not exist` 失败，但**必须打印完整 traceback** 而不是 `Failed`
  - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 5.8_
  - _Correctness Property: Property 2, Property 3, Property 5_
  - _SHALL NOT covered: #13（secret 运行时拉取）、#15（完整 trace 不只 pass/fail）_

- [x] 6. 编写 README + destroy 脚本，并执行端到端部署验证（Spec 0 收尾）
  - 创建 `README.md`：包含 5 个 section：(a) 前置条件（M4 Mac、Docker、AWS CLI 凭据、CDK CLI、确认 us-east-1 已启用 AgentCore），(b) 部署步骤（`pip install -r requirements.txt` → `cdk bootstrap aws://ACCOUNT/us-east-1`（首次） → `cdk deploy --require-approval never`），(c) 验证步骤（`python scripts/verify.py` 预期输出 `✅ Spec 0 verification passed`），(d) 清理步骤（`bash scripts/destroy.sh`），(e) 故障排查 checklist（cdk synth 失败 / docker buildx 失败 / Runtime ROLLBACK / Cognito 401 / tools/list 空 等，对应 design.md 的 Error Handling 表格）
  - 创建 `scripts/destroy.sh`：`#!/usr/bin/env bash`、`set -euo pipefail`、`cdk destroy --force McpInspectSpec0Stack`、destroy 完成后用 `aws cognito-idp list-user-pools --region us-east-1 --max-results 60` 和 `aws bedrock-agentcore list-agent-runtimes --region us-east-1` 检查无 `spec0_hello_world` 残留，有残留则非零退出
  - 用 `chmod +x scripts/destroy.sh` 赋予执行权限
  - **执行最终端到端验证**：(a) 在干净 AWS 账户/region 跑 `cdk bootstrap`（如未做）→ `time cdk deploy --require-approval never`，记录耗时（应 ≤ 10 分钟）；(b) `python scripts/verify.py` 应打印 `✅ Spec 0 verification passed` 且退出码 0；(c) 用 `aws bedrock-agentcore get-agent-runtime` 检查 ECR 镜像 platform 为 `linux/arm64`（或 `docker buildx imagetools inspect` 查 ECR URI）；(d) 跑 `bash scripts/destroy.sh` 验证可逆清理
  - 把验证 trace（含部署时长、verify 输出、destroy 输出）追加到 `docs/development-trace.md`（新建）；如有失败也归档完整 stack trace
  - _Requirements: 1.4, 5.7, 5.8_
  - _Correctness Property: Property 1, Property 4, Property 5, Property 7, Property 8_
  - _SHALL NOT covered: #14（destroy 必须能完全清理）_

## Task Dependency Graph

任务依赖与并行波次：

```json
{
  "waves": [
    {
      "wave": 1,
      "tasks": ["1"],
      "description": "项目骨架先行，所有后续任务依赖 cdk synth 能跑通"
    },
    {
      "wave": 2,
      "tasks": ["2", "3"],
      "description": "MCP server 容器与 Construct 互不依赖，可并行实施"
    },
    {
      "wave": 3,
      "tasks": ["4"],
      "description": "Stack 拼装依赖 Construct（任务 3）和容器目录（任务 2）就位"
    },
    {
      "wave": 4,
      "tasks": ["5"],
      "description": "verify 脚本依赖 Stack output 结构已定（任务 4）"
    },
    {
      "wave": 5,
      "tasks": ["6"],
      "description": "README + 端到端验证收尾，依赖前面全部就绪"
    }
  ]
}
```

文字版示意：

```
Task 1 (项目骨架)
   │
   ├──► Task 2 (MCP server 容器)         ─┐
   │                                       │
   └──► Task 3 (Construct: Cognito+Runtime) ─┤
                │                            │
                └──► Task 4 (Stack 拼装) ◄──┘
                          │
                          └──► Task 5 (verify 脚本)
                                    │
                                    └──► Task 6 (README + 端到端验证)
```

Task 2 与 Task 3 互不依赖，可并行实施；其余任务严格按顺序。

## Notes

### Spec 0 整体完成判据

- [x] 6 个 task 全部勾选完成
- [x] `cdk deploy --require-approval never` 退出码 0，耗时 ≤ 10 分钟
- [x] `python scripts/verify.py` 末尾打印 `✅ Spec 0 verification passed`，退出码 0
- [x] `cdk synth` 模板中只包含 design.md 列出的资源类型，无 Lambda / API Gateway / VPC 等禁止资源
- [x] `grep -r "aws_bedrock_agentcore_alpha" --include="*.py"` 仅匹配 `framework/runtime_construct.py`
- [x] `bash scripts/destroy.sh` 退出码 0，AWS 账户中无 `spec0_hello_world` 相关残留
- [x] 新增代码 100-300 行，新增文件 ≤ 12 个（与 Constraints 一致）

### 失败时的处置（meta-harness 二次规则）

- 同一问题修 2 次未解决 → 停下来，回头改 Spec（requirements 或 design），不要做第 3 次尝试
- 失败必须留完整 trace（编译错给完整 stderr，运行时错给完整 stack trace），归档到 `docs/development-trace.md`
- 永远不要只给 pass/fail。论文实证：完整 trace 50.0 vs score+summary 34.9 vs score only 34.6

### 实施期高风险点（提前知会）

| 风险 | 触发任务 | 缓解 |
|------|---------|------|
| FastMCP 1.27 的 `streamable_http_app.routes` 私有属性可能改名 | Task 2 | 任务里已写 fallback：用 `app.middleware('http')` 拦截 `/ping` |
| CDK alpha 2.254.0a0 Python 类型签名与 TS 文档可能不一致 | Task 3 | 实施前先做最小 spike（只创建 Runtime 不带 Cognito）跑通 cdk synth，确认 API 字段名 |
| Cognito domain prefix 全局唯一 destroy 后短期不可重用 | Task 6 | domain_prefix 加 stack 哈希后缀；`destroy.sh` 失败时打印明确提示 |
| AgentCore Runtime URL 拼接需 ARN 解析 | Task 3 | 用 `Fn.join` + `Fn.split` 在 CFN 端构造，不在 Python 端字符串拼 |

### Out of Scope（再次强调）

下列**不在 Spec 0 任务范围**：

- AgentCore Gateway → Spec 1
- Token Vault `OAuth2CredentialProvider` → Spec 1
- DevOps Agent 注册 Gateway → Spec 1
- VPC 网络模式 → Spec 2
- RDS MySQL 巡检 tool → Spec 2
- 框架抽象提炼 / cookiecutter → Spec 3+
