# Requirements Document

## Introduction

**Spec 1:AgentCore Gateway 与 Token Vault 接入 DevOps Agent**

Spec 0 已经把"AgentCore Runtime + Cognito M2M + hello-world MCP server"链路打通,本机 verify 脚本可以直连 Runtime 调用 `tools/list` / `tools/call`。但项目目标是把能力交付给 AWS DevOps Agent,而 DevOps Agent 不会、也不应该感知后端有几个 Runtime——它只看到一个 MCP 入口。

**Spec 1 单一目标**:在 Spec 0 的 Runtime 之上,引入 AgentCore Gateway 作为聚合入口,并通过 Token Vault 的 OAuth2 Credential Provider 让 Gateway 出站调用 Spec 0 Runtime 时自动换 Cognito Token,完成端到端链路:

```
DevOps Agent → AgentCore Gateway(JWT 入站) → Spec 0 Runtime(JWT 出站,经 Token Vault) → hello_world tool
```

Spec 1 完成后,DevOps Agent 在 Agent Space 控制台注册 Gateway 一次,即可调用 `<target>__hello_world` 工具并得到 `Hello, X!`。

**前置条件**:
- **Spec 0 已通过验证 ✅**(`docs/development-trace.md` 索引行 deploy v2 = 83s + verify = 9.57s,7 个 CfnOutput 全部就位且 verify.py 端到端 pass)
- Spec 0 Stack `McpInspectSpec0Stack` 当前部署在 us-east-1,且未被后续修改破坏
- AgentCore Gateway 在目标 region(us-east-1)可用
- 本地已具备 Spec 0 同样的工具链:Docker、Python 3.13、AWS CDK CLI ≥ 2.1122.0、AWS 凭据
- 用户在 AWS 控制台拥有 Agent Space 与 DevOps Agent 的访问权限(用于人工 checklist 阶段)

**单一验证条件**:

```bash
cdk deploy McpInspectSpec1Stack --require-approval never && python scripts/verify_spec1.py
```

预期末尾输出:`✅ Spec 1 verification passed`,退出码 0。

(DevOps Agent 在 Agent Space 控制台的真实链路验证是 README 中的人工 checklist,不进 verify 脚本——见 Requirement 6。)

## Glossary

| 术语 | 定义 |
|------|------|
| **AgentCore Gateway** | AWS Bedrock AgentCore 的 MCP 聚合层,对外暴露单一 MCP 端点,内部转发到一个或多个 MCP target |
| **Gateway Target** | Gateway 内部注册的后端,本 Spec 仅一个 target,类型为 `MCP`,指向 Spec 0 Runtime |
| **Token Vault** | AgentCore 的凭据托管能力,存储 Credential Provider 的配置 |
| **OAuth2CredentialProvider** | Token Vault 中存储的 OAuth2 client_credentials 配置;Gateway 调用 target 前会向其换取 access_token |
| **Inbound Auth** | DevOps Agent → Gateway 方向的认证,本 Spec 用 Cognito JWT |
| **Outbound Auth** | Gateway → Runtime 方向的认证,本 Spec 用 Token Vault 自动换 Cognito Token 注入 Authorization header |
| **DevOps Agent** | AWS DevOps Agent 服务,本 Spec 1 的最终消费方;通过 Agent Space 控制台注册 Gateway |
| **Agent Space** | AWS 控制台中管理 DevOps Agent 的页面;Spec 1 的人工 checklist 入口 |
| **Spec 0 Runtime** | Spec 0 部署的 AgentCore Runtime,Runtime name = `spec0_hello_world`,protocol = MCP,authorizer = Cognito JWT |
| **Spec 0 User Pool** | Spec 0 创建的 Cognito User Pool,含 ResourceServer `mcp` + scope `invoke` + M2M Client + Secret(ARN 在 Spec 0 Output `CognitoClientSecretArn` 中) |
| **Spec 0 Output** | Spec 0 Stack 的 7 个 CfnOutput:`RuntimeArn` / `RuntimeUrl` / `CognitoTokenEndpoint` / `CognitoClientId` / `CognitoOAuthScope` / `CognitoClientSecretArn` / `Region` |
| **Target Name** | Gateway 内部 target 的名字,本 Spec 固定为 `spec0helloworld`(可读性优先,15 字符);Gateway 自动给 tool 加 `{target_name}__` 前缀 |
| **Tool 命名空间** | DevOps Agent 看到的 tool 名,等于 `{target_name}__{原 tool 名}`,本 Spec 固定为 `spec0helloworld__hello_world`(共 28 字符,远小于 A7 上限 64) |

## Requirements

### Requirement 1: AgentCore Gateway 资源

**User Story:** 作为框架开发者,我想要一个 AgentCore Gateway 被 cdk 一键创建,以便所有后续 spec 的 Runtime 都通过同一个入口暴露给 DevOps Agent。

#### Acceptance Criteria

1. WHEN 开发者执行 `cdk deploy McpInspectSpec1Stack`, THE Spec_1_Stack SHALL 在 us-east-1 创建恰好一个 AgentCore Gateway 资源
2. WHEN Gateway 创建, THE Spec_1_Stack SHALL 配置 Gateway 的 protocol 为 MCP
3. WHEN Gateway 创建, THE Spec_1_Stack SHALL 配置 Gateway 的 inbound authorizer 为 Cognito JWT,引用 Spec 0 User Pool 与 Spec 0 M2M Client(通过 Spec 0 Output `CognitoClientId` 跨 stack 引用),且仅放行 OAuth scope `mcp/invoke`
4. WHEN Gateway 创建, THE Spec_1_Stack SHALL 启用 X-Ray tracing
5. WHEN Gateway 接收 invocation, THE Spec_1_Stack SHALL 把 Gateway 的 application logs 输出到 CloudWatch Log Group(命名 `/aws/bedrock-agentcore/gateway/{gateway-name}`,保留 7 天)
6. WHEN `cdk deploy` 完成且 Stack 状态为 `CREATE_COMPLETE` 或 `UPDATE_COMPLETE`, THE Spec_1_Stack SHALL 通过 CloudFormation Output 同时暴露 `GatewayId` 与 `GatewayUrl`(MCP endpoint URL,DevOps Agent 注册时使用),严禁仅暴露其中一个
7. WHILE Stack 处于任意非完成状态(包括 `CREATE_IN_PROGRESS` / `UPDATE_IN_PROGRESS` / `ROLLBACK_IN_PROGRESS` 等中间态), THE Spec_1_Stack SHALL NOT 让外部调用方读取到 `GatewayId` 或 `GatewayUrl` 任意一个的有效值
8. IF Gateway 创建过程中任何一步失败, THEN THE Spec_1_Stack SHALL 让 CloudFormation Stack 整体回滚,且回滚完成后 `GatewayId` 与 `GatewayUrl` 两个 Output 均不可被读取

### Requirement 2: Gateway 注册 Spec 0 Runtime 为 MCP Target

**User Story:** 作为框架开发者,我想要 Gateway 自动把 Spec 0 Runtime 注册为 MCP target,以便 DevOps Agent 可以通过 Gateway 调到 hello_world 工具。

#### Acceptance Criteria

1. WHEN Gateway 创建, THE Spec_1_Stack SHALL 在该 Gateway 上注册恰好一个 MCP target,target name 等于字符串 `spec0helloworld`
2. WHEN target 注册, THE Spec_1_Stack SHALL 通过 Spec 0 Output `RuntimeArn` 跨 stack 引用 Spec 0 Runtime,作为该 target 的后端
3. WHEN target 注册, THE Spec_1_Stack SHALL 把 target 的 outbound credential provider 绑定为 Requirement 3 创建的 OAuth2CredentialProvider
4. WHEN target 注册已完成 AND OAuth2CredentialProvider 已绑定 AND 后端 Spec 0 Runtime 处于可调用状态, THE Gateway SHALL 在 `tools/list` 响应中包含工具 `spec0helloworld__hello_world`
5. WHILE 上述三个条件(target 注册完成 / OAuth2CredentialProvider 绑定 / Spec 0 Runtime 可达)中至少有一项不成立, THE Gateway SHALL NOT 在 `tools/list` 响应中返回 `spec0helloworld__hello_world` 工具(即:严格 AND 语义,任一前提缺失即隐藏)
6. THE Spec_1_Stack SHALL 确保 target 名称 `spec0helloworld` 加上 tool 名前缀(`{target_name}__`)与原 tool 名 `hello_world` 拼接后总长度等于 28 个字符,严格小于 64 个字符

### Requirement 3: Token Vault OAuth2CredentialProvider 自动配置

**User Story:** 作为框架开发者,我想要 Gateway 出站调用 Spec 0 Runtime 时的 Cognito Token 由 Token Vault 自动管理,以便不在脚本里硬编码 token,也不在 Gateway 代码里写认证逻辑。

#### Acceptance Criteria

1. WHEN `cdk deploy McpInspectSpec1Stack` 执行, THE Spec_1_Stack SHALL 在 Token Vault 创建恰好一个 OAuth2CredentialProvider,grant type 为 `client_credentials`
2. WHEN OAuth2CredentialProvider 创建, THE Spec_1_Stack SHALL 通过 Spec 0 Output `CognitoTokenEndpoint` 跨 stack 引用 Spec 0 Cognito 的 token endpoint
3. WHEN OAuth2CredentialProvider 创建, THE Spec_1_Stack SHALL 通过 Spec 0 Output `CognitoClientId` 跨 stack 引用 Spec 0 M2M Client 的 client_id
4. WHEN OAuth2CredentialProvider 创建, THE Spec_1_Stack SHALL 通过 Spec 0 Output `CognitoClientSecretArn` 跨 stack 引用 Spec 0 Secrets Manager 中的 client_secret(只引用 ARN,不读取明文)
5. WHEN OAuth2CredentialProvider 创建, THE Spec_1_Stack SHALL 配置请求的 OAuth scope 等于字符串 `mcp/invoke`
6. WHEN Gateway 转发请求到 Spec 0 Runtime, THE Token_Vault SHALL 使用该 OAuth2CredentialProvider 自动获取 access_token,并以 `Authorization: Bearer <token>` 头注入出站请求
7. IF OAuth2CredentialProvider 获取 token 失败(网络错误 / scope 错误 / client_id 错误), THEN THE Gateway SHALL 在响应中返回明确的错误信息,且不静默成功
8. IF OAuth2CredentialProvider 获取 token 成功但 token 未能注入出站请求的 `Authorization` header, THEN THE Gateway SHALL 返回错误响应,SHALL NOT 以未认证状态继续转发到 Spec 0 Runtime

### Requirement 4: 跨 Stack 引用与命名隔离

**User Story:** 作为框架开发者,我想要 Spec 1 的资源严格独立于 Spec 0,通过跨 stack reference 关联,以便 Spec 1 / Spec 0 各自能独立 destroy。

#### Acceptance Criteria

1. WHEN `cdk synth` 执行, THE Spec_1_Stack SHALL 通过 CloudFormation cross-stack reference(`Fn::ImportValue` 或 CDK 的 `Fn.import_value`)读取 Spec 0 的 7 个 Output 之中至少 4 个:`RuntimeArn`、`CognitoTokenEndpoint`、`CognitoClientId`、`CognitoClientSecretArn`,AND THE 合成出的 CloudFormation 模板 SHALL 在 Resources 段中实际包含至少 4 个 `Fn::ImportValue` 引用,指向 Spec 0 的 Output Export 名(空跑 import 但模板里没用到不算合规)
2. WHEN `cdk deploy McpInspectSpec1Stack` 执行, THE Spec_1_Stack SHALL 不创建任何类型的 Cognito 资源,包括但不限于 `AWS::Cognito::UserPool`、`AWS::Cognito::UserPoolDomain`、`AWS::Cognito::UserPoolClient`、`AWS::Cognito::UserPoolResourceServer`、Cognito Identity Pool、以及任何与 Cognito 相关的 IAM Role(全部由 Spec 0 提供)
3. WHEN `cdk deploy McpInspectSpec1Stack` 执行, THE Spec_1_Stack SHALL 不修改 Spec 0 Stack 中的任何资源
4. IF 合成后的 CloudFormation 模板中实际出现任何 `AWS::Cognito::*` 资源类型, THEN THE Code_Review SHALL 视为违反本需求并要求改回跨 stack reference 实现(注:仅当资源在模板中真实落地时触发,代码中尝试但被注释掉或未触达 synth 的情况不计)
5. WHEN `cdk destroy McpInspectSpec1Stack` 执行, THE Spec_1_Stack SHALL 完全删除 Spec 1 创建的 Gateway / Target / OAuth2CredentialProvider / LogGroup,且不连带删除任何 Spec 0 资源
6. WHEN `cdk destroy McpInspectSpec1Stack` 执行后查询 AWS, THE Spec_0_Stack SHALL 仍然可以独立运行,且 Spec 0 的 verify.py 仍能 pass(回归)

### Requirement 5: 端到端验证脚本

**User Story:** 作为框架开发者,我想要一个一键验证脚本,以便回归 Spec 1 的端到端链路(本机模拟 DevOps Agent 走 Gateway 到 hello_world)。

#### Acceptance Criteria

1. WHEN 开发者执行 `python scripts/verify_spec1.py`, THE Verify_Script SHALL 自动从 CloudFormation 读取 `McpInspectSpec1Stack` 的 Output `GatewayUrl`,以及 `McpInspectSpec0Stack` 的 Output `CognitoTokenEndpoint` / `CognitoClientId` / `CognitoOAuthScope` / `CognitoClientSecretArn`
2. WHEN Verify_Script 运行, THE Verify_Script SHALL 通过 AWS SDK 从 Secrets Manager 拉取 Spec 0 的 client_secret(不读环境变量,不写死)
3. WHEN Verify_Script 运行, THE Verify_Script SHALL 用 `client_credentials` flow 向 Spec 0 Cognito Token Endpoint 取得 access_token,scope 等于字符串 `mcp/invoke`
4. WHEN Verify_Script 拿到 access_token, THE Verify_Script SHALL 用该 token 向 `GatewayUrl` 发送 MCP `initialize` 请求,与 Gateway 建立会话
5. WHEN MCP `initialize` 成功, THE Verify_Script SHALL 向 Gateway 发送 `tools/list` 请求
6. WHEN `tools/list` 返回, THE Verify_Script SHALL 断言返回的 tools 列表中包含名为 `spec0helloworld__hello_world` 的工具
7. WHEN tool 列表断言通过, THE Verify_Script SHALL 向 Gateway 发送 `tools/call`,name 等于 `spec0helloworld__hello_world`,arguments 等于 `{"name": "Spec1"}`
8. WHEN `tools/call` 返回, THE Verify_Script SHALL 断言返回的文本内容等于字符串 `Hello, Spec1!`
9. WHEN 所有断言全部明确通过(无任一异常或断言失败), THE Verify_Script SHALL 打印 `✅ Spec 1 verification passed` 并以退出码 0 退出
10. IF Verify_Script 任意步骤失败或抛出异常, THEN THE Verify_Script SHALL NOT 打印 `✅ Spec 1 verification passed`,且 SHALL 打印完整 Python traceback(`traceback.print_exc()`)并以非零退出码退出
11. THE Verify_Script SHALL NOT 在 stdout 输出 access_token 内容、client_secret 内容或任何完整 Authorization header(允许打印 token 长度、status code 等元信息)

### Requirement 6: DevOps Agent 端真实链路注册指引(README 人工 checklist)

**User Story:** 作为框架使用者,我想要一份在 Agent Space 控制台注册 Gateway 的步骤指引,以便我能用 DevOps Agent 真实调用 hello_world,完成业务侧验证。

#### Acceptance Criteria

1. WHEN `cdk deploy McpInspectSpec1Stack` 完成, THE README_Spec1_Section SHALL 提供一份 step-by-step 的人工 checklist,描述如何在 Agent Space 控制台把 Gateway 注册给 DevOps Agent
2. THE README_Spec1_Section SHALL 列出注册过程中需要的精确字段:`GatewayUrl`(从 Spec 1 Output 读取)、Cognito 入站认证所需的 client_id 与 token endpoint(从 Spec 0 Output 读取)、所需 scope `mcp/invoke`
3. THE README_Spec1_Section SHALL 提供注册成功后的人工验证步骤:在 DevOps Agent 中触发一次会话,确认它列出 `spec0helloworld__hello_world` 工具,并对话调用一次得到 `Hello, ...` 回复
4. THE README_Spec1_Section SHALL 标注此 checklist 是 **可选人工验证**,不阻塞 Spec 1 的"单一验证条件"通过
5. THE README_Spec1_Section SHALL 在文档中实际列出至少 3 条具体可执行的排查入口(精确到命令 / 控制台路径,如 `aws logs tail /aws/bedrock-agentcore/gateway/* --since 10m` / Agent Space → Gateway 注册页 → 测试调用按钮 / `curl <token-endpoint>` 直连 Cognito 验证),且明确这些入口仅在注册或调用失败时被参考,不要求用户在成功路径下预读

### Requirement 7: Gateway 执行角色最小权限

**User Story:** 作为框架开发者,我想要 Gateway 执行角色严格遵循最小权限原则,以便不引入安全漏洞。

#### Acceptance Criteria

1. WHEN Gateway 创建, THE Spec_1_Stack SHALL 给 Gateway 执行角色赋予恰好以下三类权限:CloudWatch Logs 写入(限定到 `/aws/bedrock-agentcore/gateway/*`)、X-Ray 写入(`PutTraceSegments` / `PutTelemetryRecords`)、Token Vault 读取(限定到 Spec 1 创建的 OAuth2CredentialProvider 资源 ARN)
2. THE Spec_1_Stack SHALL NOT 给 Gateway 执行角色赋予 `*:*`、`AdministratorAccess`,或任何 AWS 托管的过权限策略;AND THE Spec_1_Stack SHALL 在合成模板中实际授予 AC1 列出的三类权限(CloudWatch Logs 写入 + X-Ray 写入 + Token Vault 读取),三者缺一即视为违反本条
3. WHERE OAuth2CredentialProvider 的实现需要 Gateway 执行角色具备 `secretsmanager:GetSecretValue` 才能解析 Spec 0 的 client_secret, THE Spec_1_Stack SHALL 自动给 Gateway 执行角色赋予恰好该一个 action,资源限定到 Spec 0 Output `CognitoClientSecretArn` 提供的具体 Secret ARN,不使用任何通配资源

### Requirement 8: 框架 Construct 抽象与 alpha 隔离

**User Story:** 作为框架开发者,我想要 Gateway / Target / OAuth2CredentialProvider 的 alpha API 封装在 framework/ 目录,以便未来 alpha breaking change 时影响面收敛。

#### Acceptance Criteria

1. WHEN 开发者新建 Spec 1 的 Gateway 资源, THE Framework SHALL 提供一个 Construct(命名 `McpInspectGateway`,位于 `framework/gateway_construct.py`),封装 Gateway + Target + OAuth2CredentialProvider 的创建逻辑
2. THE McpInspectGateway SHALL 对外只暴露 Python 原生类型与稳定 CDK 类型(`str` / `Path` / `secretsmanager.ISecret` 等),不暴露任何 `aws_bedrock_agentcore_alpha` 包内的类型作为参数或属性
3. THE McpInspectGateway SHALL 通过参数接受 Spec 0 的关键引用,参数类型限定为 Python 原生类型(`str` / `Path`)与 stable CDK 类型(如 `secretsmanager.ISecret`),具体包括:`runtime_arn: str`、`token_endpoint: str`、`client_id: str`、`client_secret: secretsmanager.ISecret`、`oauth_scope: str`,严禁使用 `aws_bedrock_agentcore_alpha` 包内的类型作为参数
4. WHEN 对 codebase 执行命令 `grep -rn "aws_bedrock_agentcore_alpha" --include="*.py" --exclude-dir=.venv --exclude-dir=cdk.out .`, THE Framework SHALL 仅匹配 `framework/runtime_construct.py`(Spec 0 引入)与 `framework/gateway_construct.py`(本 Spec 引入)两个文件
5. THE Spec_1_Stack(`stacks/spec_one_stack.py`)SHALL NOT 直接 import `aws_bedrock_agentcore_alpha`

### Requirement 9: cdk destroy 完全清理

**User Story:** 作为框架开发者,我想要 Spec 1 的所有资源能被 `cdk destroy` 完全清理,以便迭代不留后账。

#### Acceptance Criteria

1. WHEN `cdk destroy McpInspectSpec1Stack` 执行, THE Spec_1_Stack SHALL 把所有自身创建的资源(Gateway / MCP Target / OAuth2CredentialProvider / Gateway 执行角色 / LogGroup)的 `RemovalPolicy` 设为 `DESTROY`
2. WHEN `cdk destroy` 完成, THE Verification SHALL 通过 AWS CLI(`aws bedrock-agentcore list-gateways` / `list-credential-providers`)确认上述资源均已删除
3. IF AWS CLI 在 destroy 后仍能查询到任意一个 Spec 1 创建的资源(Gateway / Target / OAuth2CredentialProvider / 执行角色 / LogGroup), THEN THE Verification SHALL 立即以非零退出码失败,SHALL NOT 因为 stack 状态显示 DELETE_COMPLETE 就视为通过
3. THE Spec_1_Stack SHALL NOT 把任何资源的 `removalPolicy` 设为 `RETAIN`

## Constraints

可量化约束清单:

| 类别 | 约束 |
|------|------|
| 部署区域 | `us-east-1`(与 Spec 0 一致,本 Spec 不支持多 region)|
| Python 版本 | 3.13(与 Spec 0 一致)|
| CDK 主包版本 | `aws-cdk-lib == 2.254.0`(沿用 Spec 0 锁定,不升级)|
| CDK alpha 包版本 | `aws-cdk.aws-bedrock-agentcore-alpha == 2.254.0a0`(沿用 Spec 0 锁定,不升级)|
| MCP SDK 版本 | `mcp ~= 1.27`(与 Spec 0 一致)|
| MCP 协议 | Streamable HTTP,与 Spec 0 一致 |
| 网络模式 | Gateway 与 Spec 0 Runtime 均 Public Network(VPC 模式留给 Spec 2)|
| Gateway 数量 | 恰好 1 个(对应 Spec 0 Runtime 一对一,符合单 Gateway 策略 A2)|
| MCP Target 数量 | 恰好 1 个(指向 Spec 0 Runtime)|
| OAuth2CredentialProvider 数量 | 恰好 1 个 |
| Target 名 | 字符串 `spec0helloworld`(15 字符)|
| 暴露 tool 名 | 字符串 `spec0helloworld__hello_world`(28 字符,严格 < 64)|
| Inbound scope | `mcp/invoke`(复用 Spec 0 ResourceServer 与 scope)|
| 部署 + verify 总时长 | ≤ 15 分钟(本 Spec 引入 Gateway / Target / Token Vault 共 3 类资源,放宽 Spec 0 的 10 分钟上限)|
| 本 Spec 改动量 | 100-500 行新增代码,新增文件 ≤ 12 个 |
| 本 Spec 任务粒度 | 后续 tasks.md 任务数 3-7 个 |

## SHALL NOT(按三层分类)

### Requirements 层(功能边界)

1. **SHALL NOT** 在 Spec 1 创建任何新的 Cognito User Pool / Resource Server / Client / Client Secret。Spec 0 已经提供完整 M2M 认证基础设施,Spec 1 一律跨 stack 引用。原因:符合 A1/A4 凭据集中,避免双 User Pool 维护成本
2. **SHALL NOT** 在 Spec 1 创建多于一个 AgentCore Gateway。原因:遵守 shall-not.md 第 4 条单 Gateway 策略,本 Spec 的工具数与团队边界都没有触发拆分条件
3. **SHALL NOT** 在 Spec 1 注册除 Spec 0 Runtime 之外的任何 MCP target。原因:验证条件单一化,真实数据源 target 是 Spec 2 起的事
4. **SHALL NOT** 在 Spec 1 引入任何真实数据源(RDS / Redis / DynamoDB)。原因:边界蠕变防护,数据源是 Spec 2 起的事
5. **SHALL NOT** 让 cdk 自动注册 Gateway 到 DevOps Agent / Agent Space。原因:Agent Space 没有公开 IaC API,自动化在技术上不可行;DevOps Agent 端的注册由 README 人工 checklist 承担(Requirement 6)

### Design 层(架构 / API 选择)

6. **SHALL NOT** 跳过 Gateway 让 DevOps Agent 直接注册 Spec 0 Runtime。原因:遵守 shall-not.md 第 5 条,Gateway 是框架的入口收敛点,跳过即失去聚合 / 统一认证 / tool 命名空间
7. **SHALL NOT** 给 Gateway 配置 `NoAuth` 或 IAM 默认认证。inbound 必须 Cognito JWT。原因:遵守 shall-not.md 第 8 条,即使是验证性质的 spec 也不允许 NoAuth
8. **SHALL NOT** 在 Gateway 与 Spec 0 Runtime 之间用任何形式的"硬编码 token"或"长期有效 token"作为出站凭据。出站认证必须走 Token Vault OAuth2CredentialProvider,运行时换 Cognito access_token。原因:凭据短期化、可旋转;符合 A5 凭据集中
9. **SHALL NOT** 把 Spec 0 的 client_secret 明文以任何形式写入 Spec 1 的代码、模板、Output 或脚本。Spec 1 仅引用 Secret 的 ARN,Token Vault 内部按 ARN 解析。原因:遵守 shall-not.md 第 12 条,凭据零泄露
10. **SHALL NOT** 在 `stacks/spec_one_stack.py` 或任何脚本中 import `aws_bedrock_agentcore_alpha`。alpha 类型必须封装在 `framework/gateway_construct.py` 内部。原因:遵守 shall-not.md 第 2 / 第 8 条,alpha 风险面继续严格收敛
11. **SHALL NOT** 直接用 L1 `CfnGateway` / `CfnGatewayTarget` / `CfnTokenVault*` 写资源。优先 L2 alpha API;仅当 L2 不支持的能力(如某项 target 配置)才允许局部降级 L1,且必须限定在 framework/gateway_construct.py 内部。原因:维持框架风格统一

### Tasks 层(实现写法 / 安全 / 边界)

12. **SHALL NOT** 在代码中硬编码 Spec 0 的任何 ARN / token endpoint / client_id。所有跨 stack 引用必须走 `Fn.import_value` 或 CDK 的 `Stack.from_stack_attributes` / `Output.export_name` 等机制,引用 Spec 0 Output。原因:遵守 shall-not.md 第 9 条
13. **SHALL NOT** 给 Gateway 执行角色挂任何 AWS 托管的过权限策略,以及任何含 `*:*` 通配的内联策略。原因:遵守 shall-not.md 第 11 条
14. **SHALL NOT** 把 Spec 1 创建的资源(Gateway / Target / OAuth2CredentialProvider / LogGroup)设为 `removalPolicy=RETAIN`。原因:遵守 shall-not.md 第 14 条
15. **SHALL NOT** 让 verify_spec1.py 只输出 pass/fail。失败必须打印完整 Python traceback。原因:遵守 shall-not.md 第 15 条 meta-harness 完整 trace 原则
16. **SHALL NOT** 在 `cdk destroy McpInspectSpec1Stack` 时连带删除 Spec 0 资源。原因:迭代纪律,各 spec 自闭环,跨 spec destroy 互不干扰
17. **SHALL NOT** 在 verify_spec1.py 输出中打印 access_token 内容、client_secret 内容或完整 Authorization header。允许打印 token 长度、HTTP status code、tools 列表等不含敏感信息的元数据。原因:遵守 shall-not.md 第 12 条

## 明确不包含(Out of Scope)

下列事项**不属于 Spec 1**,将在后续 spec 处理:

| 事项 | 归属 |
|------|------|
| Runtime VPC 模式 | Spec 2 |
| 第二个 MCP target(指向 RDS MySQL Runtime 等真实数据源)| Spec 2 |
| RDS MySQL 连接、Secrets Manager 数据库凭据管理 | Spec 2 |
| 第一个真实巡检 tool(如 `inspect_long_transactions`) | Spec 2 |
| 框架抽象提炼 / 项目脚手架(从 Spec 0 + Spec 1 经验提炼通用 Construct API) | Spec 3 或更晚 |
| 多数据源 Runtime(Redis / DynamoDB 等) | Spec 4+ |
| Cognito 与企业 IDP 集成(SAML / OIDC 联邦,DevOps Agent 改用 3LO) | Spec 4+ |
| 跨账户 / 多 region 部署 | Spec 4+ |
| Gateway 拆分(工具数 > 30 / 团队边界 / 权限差异触发的多 Gateway 策略) | 触发条件出现时新建 spec |
| 在 Token Vault 中支持 API Key Provider / 自定义认证 Provider | 后续按需 |
| DevOps Agent 端 cdk 自动注册(若 Agent Space 未来开放 IaC API) | 后续按需 |
| 端到端真实链路验证(DevOps Agent → Gateway → Runtime → tool)纳入自动化 verify | 当前停留在 README 人工 checklist;Agent Space 开放程序化测试入口后再考虑 |

## 风险与备注

- **AgentCore Gateway L2 alpha 风险**:与 Spec 0 一样,alpha API 可能 breaking change;Construct 隔离把影响面收敛到 `framework/gateway_construct.py`
- **Token Vault L2 alpha 风险**:OAuth2CredentialProvider 的 L2 API 可能仍在演进。design 阶段需以 .venv 中实际 jsii 生成签名为准,而非 design 推测
- **跨 stack reference 顺序**:`cdk destroy McpInspectSpec0Stack` 时若 Spec 1 仍在引用其 Output,CFN 会拒绝删除。README 必须标明清理顺序:先 destroy Spec 1,再 destroy Spec 0
- **DevOps Agent 端注册的可重复性**:Agent Space 没有公开 API,人工 checklist 步骤可能随 AWS 控制台改版微调。README 中的注册截图与字段名要标注"以 AWS 控制台实际界面为准"
- **Gateway domain / endpoint 全局唯一**:与 Cognito hosted UI domain 类似,Gateway URL 中可能包含全局唯一的子域;destroy 后短期重 deploy 可能需要等待资源释放(具体行为以 alpha 包实测为准)
- **Tool 命名空间稳定性**:DevOps Agent 端注册后会持久化 tool 名 `spec0helloworld__hello_world`;若未来改 target name,DevOps Agent 端的工具引用会断,这条要在 README 用粗体标注
