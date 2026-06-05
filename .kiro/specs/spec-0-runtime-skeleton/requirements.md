# Requirements Document

## Introduction

**Spec 0: AgentCore Runtime 骨架与 hello-world MCP**

本框架旨在为 AWS DevOps Agent 提供数据访问能力扩展，通过 MCP 协议解决 DevOps Agent 当前因权限受限无法直接访问 RDS / Redis / DynamoDB 等数据源的问题。整个框架按 Spec 编号迭代推进，Spec 0 是地基。

**Spec 0 单一目标**：在 AWS 账户里部署一个最小可用的 AgentCore Runtime，承载一个 hello-world MCP server，并验证从本地能用 Cognito Token 调用 `tools/list` 拿到 hello 工具。

**前置条件**：
- 这是框架的第一个 spec，无前置 spec 依赖
- 假设 AWS 账户已具备 AgentCore 服务可用性（GA region：us-east-1 / us-west-2 / ap-southeast-2 等，部署前确认）
- 假设本地已安装 Docker、Python 3.13+、AWS CDK CLI、AWS 凭据

**单一验证条件**：

```bash
cdk deploy --require-approval never && python scripts/verify.py
```

预期末尾输出：`✅ Spec 0 verification passed`，退出码 0 = pass。

## Glossary

| 术语 | 定义 |
|------|------|
| **AgentCore Runtime** | Amazon Bedrock AgentCore 的 serverless 计算环境，专为部署 AI agent 和 MCP server 设计 |
| **AgentCore Gateway** | AgentCore 的 MCP 聚合层，在 Spec 1 引入；Spec 0 不涉及 |
| **MCP** | Model Context Protocol，LLM 与外部工具/数据源标准化交互协议 |
| **MCP server** | 实现 MCP 协议、对外暴露 tool / resource / prompt 的服务 |
| **FastMCP** | MCP Python SDK 中提供的高层服务端封装，本框架统一使用 |
| **Streamable HTTP** | MCP 的一种传输实现，AgentCore Runtime 唯一支持的传输形式 |
| **Stateless HTTP** | FastMCP 的无状态模式（`stateless_http=True`），AgentCore Runtime 兼容性要求 |
| **Cognito M2M** | Cognito User Pool 配合 OAuth 2.0 Client Credentials flow，用于服务到服务认证 |
| **Resource Server** | Cognito 中定义 OAuth scope 的资源；本 spec 仅用一个 scope `mcp/invoke` |
| **L2 alpha** | CDK Construct 库中 `@aws-cdk/aws-bedrock-agentcore-alpha` 包，提供高层 API；experimental 状态 |
| **L1 (Cfn\*)** | CDK 中直接对应 CloudFormation 资源的最底层 Construct，stable 但抽象低 |
| **DevOps Agent** | AWS DevOps Agent 服务，本框架最终的 MCP 消费方；Spec 0 不涉及 |
| **Spec 0 / Spec 1 / ...** | 本项目按 meta-harness 准则切分的迭代单元，每个 spec ≈ 一个 PR |

## Requirements

### Requirement 1: CDK 项目骨架

**User Story:** 作为框架开发者，我想要一个干净的 CDK 项目结构，以便后续 spec 可以在此基础上增量扩展。

#### Acceptance Criteria

1. WHEN 开发者在干净环境执行 `cdk synth` THEN the system SHALL 成功合成出一个 CloudFormation 模板
2. WHEN `cdk synth` 完成 THEN the system SHALL 在合成模板中包含且仅包含以下顶层资源：ECR Repository、AgentCore Runtime、Cognito User Pool、Cognito User Pool Client、Cognito Resource Server、Runtime 执行 IAM Role、Cognito Client Secret 的 Secrets Manager Secret
3. WHEN `cdk synth` 完成 THEN the system SHALL 输出一个体积小于 50 KB 的 CloudFormation 模板（防止过度复杂）
4. WHEN 项目被复制到任意机器并 `pip install -r requirements.txt` 后 THEN the system SHALL 能够在不依赖额外手工配置的前提下完成 `cdk synth`

### Requirement 2: hello-world MCP Server 容器

**User Story:** 作为框架开发者，我想要一个最小可运行的 MCP server 镜像，以便验证 AgentCore Runtime 的容器规约符合预期。

#### Acceptance Criteria

1. WHEN 开发者执行 `cdk deploy` THEN the system SHALL 通过 CDK 的 `AgentRuntimeArtifact.fromAsset()` 自动构建一个 `linux/arm64` 平台的容器镜像并推送到 ECR
2. WHEN 容器启动 THEN the system SHALL 运行 FastMCP 实例（`stateless_http=True`）监听 `0.0.0.0:8080`
3. WHEN MCP 客户端调用 `tools/list` THEN the system SHALL 返回一个名为 `hello_world(name: str) -> str` 的工具
4. WHEN MCP 客户端调用 `tools/call` 携带 `name="World"` THEN the system SHALL 返回 `"Hello, World!"`
5. WHEN AgentCore Runtime 健康检查请求 `GET /ping` THEN the system SHALL 返回 200 状态码（AgentCore Runtime 容器规约要求）
6. WHEN 镜像被构建 THEN the system SHALL 包含 `.dockerignore` 排除 `.env` / `.aws/` / `__pycache__/` / `.git/`

### Requirement 3: AgentCore Runtime 资源

**User Story:** 作为框架开发者，我想要 Runtime 资源被 CDK 一键创建，以便其他 spec 直接复用其 ARN 和 URL。

#### Acceptance Criteria

1. WHEN `cdk deploy` 完成 THEN the system SHALL 创建一个 protocol 配置为 MCP 的 AgentCore Runtime
2. WHEN Runtime 创建 THEN the system SHALL 配置 inbound authorizer 为 Cognito JWT（指向 Spec 0 创建的 User Pool 与 Client）
3. WHEN Runtime 创建 THEN the system SHALL 启用 X-Ray tracing
4. WHEN Runtime 接收 invocation THEN the system SHALL 将 APPLICATION_LOGS 输出到名为 `/aws/bedrock-agentcore/{runtime-name}` 的 CloudWatch Log Group
5. WHEN `cdk deploy` 完成 THEN the system SHALL 通过 CloudFormation Output 暴露 `RuntimeArn` 和 `RuntimeUrl`（拼接好的 invocation URL，格式 `https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{encoded-arn}/invocations?qualifier=DEFAULT`）

### Requirement 4: Cognito M2M 认证基础设施

**User Story:** 作为框架开发者，我想要一套 Cognito M2M 认证开箱即用，以便本地 verify 脚本和未来 Gateway 都能复用。

#### Acceptance Criteria

1. WHEN `cdk deploy` 完成 THEN the system SHALL 创建一个 Cognito User Pool
2. WHEN User Pool 创建 THEN the system SHALL 创建一个 Resource Server，定义至少一个 scope `mcp/invoke`
3. WHEN User Pool 创建 THEN the system SHALL 创建一个 User Pool Client，启用 `client_credentials` grant type，启用 `mcp/invoke` scope，禁用其他 grant type
4. WHEN User Pool Client 创建 THEN the system SHALL 将 client secret 写入 Secrets Manager，并通过 Output 暴露 Secret ARN（**禁止**直接 Output 明文 secret）
5. WHEN `cdk deploy` 完成 THEN the system SHALL 通过 CloudFormation Output 暴露：`CognitoTokenEndpoint`、`CognitoClientId`、`CognitoOAuthScope`、`CognitoClientSecretArn`
6. WHEN `cdk destroy` 执行 THEN the system SHALL 完全删除 User Pool 与 Secret（`removalPolicy=DESTROY`），不留遗留资源

### Requirement 5: 本地端到端验证脚本

**User Story:** 作为框架开发者，我想要一个一键验证脚本，以便回归 Spec 0 的端到端链路。

#### Acceptance Criteria

1. WHEN 开发者执行 `python scripts/verify.py` THEN the system SHALL 自动从 CloudFormation Stack outputs 读取 Cognito 端点、client ID、scope、secret ARN、Runtime URL
2. WHEN verify 脚本运行 THEN the system SHALL 通过 AWS SDK 从 Secrets Manager 拉取 client secret（**禁止**任何形式的硬编码或 .env 注入）
3. WHEN verify 脚本运行 THEN the system SHALL 用 `client_credentials` flow 向 Cognito Token Endpoint 取得 access_token
4. WHEN verify 脚本运行 THEN the system SHALL 使用该 token 向 `RuntimeUrl` 发送 MCP `initialize` 请求建立会话
5. WHEN initialize 成功 THEN the system SHALL 发送 `tools/list` 请求
6. WHEN tools/list 返回 THEN the system SHALL 断言返回结果包含名为 `hello_world` 的工具，否则脚本以非零退出码失败
7. WHEN 全部断言通过 THEN the system SHALL 打印 `✅ Spec 0 verification passed` 并以退出码 0 退出
8. WHEN 任何步骤失败 THEN the system SHALL 打印完整 stack trace（不能只打印 pass/fail），便于诊断（meta-harness 完整 trace 原则）

### Requirement 6: 可观测性最低线

**User Story:** 作为框架开发者，我想要 Runtime 的最低可观测性默认开启，以便排查问题时不缺数据。

#### Acceptance Criteria

1. WHEN Runtime 接收任意 invocation THEN the system SHALL 生成 X-Ray trace
2. WHEN Runtime 应用产生日志 THEN the system SHALL 将 APPLICATION_LOGS 推送到 CloudWatch Logs
3. WHEN `cdk deploy` 完成 THEN the system SHALL 自动给 Runtime 执行角色赋予最小所需权限：ECR pull、CloudWatch Logs 写入、X-Ray 写入。SHALL NOT 包含任何 `*:*` 或 `AdministratorAccess` 等过权限

## Constraints

可量化约束清单：

| 类别 | 约束 |
|------|------|
| 部署区域 | `us-east-1`（固定，本 spec 不支持多 region）|
| Python 版本 | 3.13（CDK app + MCP server 容器内一致）|
| CDK 主包版本 | `aws-cdk-lib == 2.254.0`（精确锁定）|
| CDK alpha 包版本 | `aws-cdk.aws-bedrock-agentcore-alpha == 2.254.0a0`（精确锁定，与 cdk-lib 配对）|
| MCP SDK 版本 | `mcp ~= 1.27`（锁定次版本）|
| 容器平台 | `linux/arm64`（AgentCore Runtime 硬要求，amd64 不接受）|
| MCP 协议 | Streamable HTTP，`stateless_http=True`，无 SSE / stdio |
| 网络模式 | Public Network（VPC 模式留给 Spec 2）|
| 单 stack 部署时长 | ≤ 10 分钟（含 ARM64 镜像构建上传）|
| 本 spec 改动量 | 100-300 行新增代码，新增文件 ≤ 12 个 |

## SHALL NOT（按三层分类）

### Requirements 层（功能边界）

1. **SHALL NOT** 在 Spec 0 引入 AgentCore Gateway。Gateway + DevOps Agent 链路是 Spec 1 的事。原因：粒度控制，避免一次做太多
2. **SHALL NOT** 在 Spec 0 接入任何真实数据源（RDS / Redis / DynamoDB）。Secrets Manager 仅用于存 Cognito client secret，不存数据库凭据。原因：真实巡检 tool 是 Spec 2 的事
3. **SHALL NOT** 暴露多个 MCP tool。只能有 `hello_world` 一个，专门用于链路验证。原因：验证条件单一化

### Design 层（架构 / API 选择）

4. **SHALL NOT** 使用 stdio 或 SSE-only 的 MCP 传输。原因：AgentCore Runtime 仅支持 Streamable HTTP
5. **SHALL NOT** 使用 Lambda + API Gateway 的传统 MCP server 形态。原因：本框架统一用 AgentCore Runtime，避免架构分裂
6. **SHALL NOT** 使用 `GatewayAuthorizer.withNoAuth()` 或 Runtime 的 IAM 默认认证。必须 Cognito JWT。原因：避免后续重构引入认证体系
7. **SHALL NOT** 直接用 L1 `Cfn*` construct 写 Runtime / Cognito 资源。优先 L2 alpha API；只有 L2 不支持的才允许降级 L1。原因：维持框架风格统一
8. **SHALL NOT** 在 Construct 类外暴露 alpha 包的类型。alpha 类型必须封装在 Construct 内部。原因：alpha API 可能 breaking change，隔离影响面

### Tasks 层（实现写法 / 安全 / 边界）

9. **SHALL NOT** 在代码中硬编码 AWS region、account ID、Cognito 域名。一律走 CDK env / context / 资源属性 token。原因：可移植性
10. **SHALL NOT** 给 Runtime 执行角色赋予 `*:*`、`AdministratorAccess` 或任何过权限 managed policy。最小权限：ECR pull、CloudWatch Logs 写、X-Ray 写。原因：least privilege
11. **SHALL NOT** 在容器镜像里复制本地凭据、`.env`、`.aws/`。Dockerfile 必须配套 `.dockerignore`。原因：凭据泄露风险
12. **SHALL NOT** 跳过 `--platform linux/arm64` 设置。原因：AgentCore Runtime 不接受 amd64 镜像
13. **SHALL NOT** 在 verify 脚本里硬编码 Cognito client secret。必须运行时从 Secrets Manager 拉取。原因：凭据管理一致性
14. **SHALL NOT** 把 Cognito User Pool / Secret 设为 `removalPolicy=RETAIN`。原因：Spec 0 是验证性质，`cdk destroy` 必须能完全清理
15. **SHALL NOT** 让 verify 脚本只输出 pass/fail 文字。失败必须打印完整 stack trace。原因：meta-harness 完整 trace 原则（论文实证：完整 trace 50.0 vs score+summary 34.9）

## 明确不包含（Out of Scope）

下列事项**不属于 Spec 0**，将在后续 spec 处理：

| 事项 | 归属 |
|------|------|
| AgentCore Gateway 创建 | Spec 1 |
| Gateway 注册 Runtime 为 MCP target | Spec 1 |
| Token Vault `OAuth2CredentialProvider` 配置 | Spec 1 |
| DevOps Agent 端注册 Gateway 并端到端验证 | Spec 1 |
| Runtime VPC 模式 | Spec 2 |
| RDS MySQL 连接、Secrets Manager 数据库凭据管理 | Spec 2 |
| 第一个真实巡检 tool（如 `inspect_long_transactions`）| Spec 2 |
| 框架抽象提炼 / 项目脚手架 | Spec 3 或更晚 |
| 多数据源支持（Redis / DynamoDB 等）| Spec 4+ |
| Cognito 与企业 IDP 集成（SAML / OIDC 联邦）| Spec 4+ |
| 跨账户 / 多 region 部署 | Spec 4+ |

## 风险与备注

- **AgentCore L2 alpha 风险**：API 可能 breaking change。所有 alpha 类型封装在 `framework/` 目录的 Construct 内部，对外只暴露稳定参数（`source_path`、`runtime_name` 等）
- **ARM64 构建依赖**：M 系列 Mac 原生支持；Intel Mac / Linux x86 需要 docker buildx + QEMU。Spec 0 不阻塞 x86 开发机但 verify 时间会更长
- **Region 可用性**：deploy 前必须确认目标 region 已 GA AgentCore，建议先用 us-east-1
- **CDK alpha 版本锁定策略**：在 `requirements.txt` 中精确锁定 alpha 包版本号，避免随手升级引入 breaking change
