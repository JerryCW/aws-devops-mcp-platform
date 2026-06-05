# SHALL NOT 跨 spec 通用清单

> 跨所有 spec 都成立的"已知失败模式"。每条来源标注：架构决策 / 实施经验 / 安全要求。  
> Spec 内部的 SHALL NOT 只写 spec 特有的，通用条款放这里集中管理。  
> 新增条款的来源：每轮 spec 实施过程中发现的坑（写进 trace）→ 通用化后回灌到这里。

## Architecture 层

### 1. SHALL NOT 引入 Lambda + API Gateway 形态的 MCP server
本框架统一用 AgentCore Runtime 承载所有 MCP server。引入 Lambda+APIGW 会造成架构分裂、凭据管理多套。  
来源：架构决策（Runtime 是框架的统一计算面）

### 2. SHALL NOT 在 Construct / Stack 类外暴露 `aws_bedrock_agentcore_alpha` 类型
alpha 包随 cdk-lib 主版本同步可能 breaking change。所有 alpha 类型必须封装在 `framework/` 目录下的 Construct 内部，对外只暴露 Python 原生类型（str / Path / ISecret 等）。  
来源：架构决策（隔离 alpha 风险面到一个文件）  
检查方式：`grep -r "aws_bedrock_agentcore_alpha" --include="*.py" .` 应仅匹配 `framework/` 目录

### 3. SHALL NOT 给 MCP server 暴露通用 SQL / 命令执行类 tool（兜底查询除外，且需走严格白名单）
所有 tool 必须语义化（如 `inspect_long_transactions`、`inspect_lock_waits`），SQL/命令固化在 server 内。这是框架的核心安全模型。如未来确实需要兜底查询 tool，必须满足：read-only DB 账号 + SQL 解析白名单 + LIMIT 强制 + 审计日志。  
来源：架构决策（避免 prompt injection 攻击面、保 LLM 推理准确率）

### 4. SHALL NOT 创建多个 AgentCore Gateway（除非有明确多租户/多巡检域诉求）
当前框架第一阶段使用单 Gateway 聚合所有 MCP target。拆 Gateway 是有触发条件的工程决策（工具数 > 30 / 团队边界 / 权限差异），不是默认操作。  
来源：架构决策

### 5. SHALL NOT 跳过 AgentCore Gateway 直接让 DevOps Agent 注册多个 MCP Runtime
框架的入口收敛点是 Gateway。跳过 Gateway 直连 Runtime 会失去聚合 / 统一认证 / tool 命名空间管理能力。  
来源：架构决策

## Tech 层

### 6. SHALL NOT 使用 stdio 或 SSE-only 的 MCP 传输
AgentCore Runtime 只支持 Streamable HTTP，且必须 `stateless_http=True`。  
来源：AgentCore Runtime 硬约束

### 7. SHALL NOT 推送 amd64 / multi-arch 镜像到 AgentCore Runtime
Runtime 只接受 `linux/arm64` 单平台镜像。Dockerfile 第一行必须 `FROM --platform=linux/arm64 ...`。  
来源：AgentCore Runtime 硬约束

### 8. SHALL NOT 使用 Runtime / Gateway 的 IAM 默认认证或 NoAuth 模式
所有 inbound 认证必须 Cognito JWT（M2M 用 client credentials；3LO 留待企业 IDP 集成）。即使是验证性质的 spec，也不允许 NoAuth。  
来源：架构决策（避免后续重构引入认证体系）

### 9. SHALL NOT 在代码中硬编码 AWS region / account ID / Cognito 域名 / ARN
全部走 CDK env / context / 资源属性 token / Stack outputs。运行时脚本（verify / 自动化）从 CloudFormation describe-stacks 取，不读环境变量也不写死。  
来源：可移植性

### 10. SHALL NOT 把 alpha 包与 cdk-lib 主包版本错配
alpha 包版本号设计（如 `2.254.0a0`）就是为了和主包配对。`requirements.txt` 必须精确锁两者，且升级时同步升。  
来源：jsii 兼容性

## Security / Operational 层

### 11. SHALL NOT 给 Runtime / Gateway 执行角色赋予 `*:*`、`AdministratorAccess` 或任何过权限 managed policy
最小权限：仅必需的 ECR pull、CloudWatch Logs 写、X-Ray 写、（数据源 spec 才加）Secrets Manager 读特定前缀、目标资源最小操作权限。  
来源：least privilege

### 12. SHALL NOT 在 CloudFormation Outputs / 脚本 stdout / 日志中输出任何 secret 明文
Cognito client secret、数据库密码、API key 等一律存 Secrets Manager，对外只暴露 ARN。脚本调用时运行时拉取，不打印 token / secret 内容（最多打印长度）。  
来源：凭据零泄露

### 13. SHALL NOT 在容器镜像里复制本地凭据 / `.env` / `.aws/`
每个 MCP server 容器目录必须配套 `.dockerignore`，显式排除 `.env`、`.aws/`、`__pycache__/`、`.git/`、`.kiro/`。  
来源：凭据泄露风险

### 14. SHALL NOT 把会随 spec destroy 一起销毁的资源设为 `removalPolicy=RETAIN`
验证性质的 spec 资源（UserPool、Secret、LogGroup、Runtime 等）必须 `RemovalPolicy.DESTROY`，保证 `cdk destroy` 能完全清理，不阻塞下一轮迭代。生产场景的保留策略另议。  
来源：迭代纪律

### 15. SHALL NOT 让自动化脚本只输出 pass/fail
失败必须打印完整 stack trace（编译错给完整 stderr，运行时错给完整 traceback，性能问题给 profiling 数据）。trace 归档到 `docs/development-trace.md`。  
来源：meta-harness 完整 trace 原则（论文实证：完整 trace 50.0 vs score+summary 34.9）

### 16. SHALL NOT 在 spec 内同时修 2 次未解决就发起第 3 次尝试
第 2 次失败后停下来，回头改 Spec（requirements 或 design），不要做第 3 次尝试。  
来源：meta-harness 二次规则

## Spec 1 实施期回灌（2026-05-19，Spec 1 复盘）

### 17. SHALL NOT 在 MCP 协议的 AgentCore Runtime 容器里把 FastMCP / uvicorn 监听 port 设为 8080
MCP 协议下 Runtime 服务契约硬编码转发到容器的 port=8000、mount path=/mcp；8080 是 HTTP 协议端口。误用会导致 invocation read timeout 而非 4xx，诊断成本极高（Spec 0 实测花了 1 小时定位）。  
对照表（来自 `runtime-service-contract.html`）：

| 协议 | 容器 port | Mount path |
|------|-----------|------------|
| MCP  | 8000      | /mcp       |
| HTTP | 8080      | /invocations |
| A2A  | 9000      | /          |

来源：Spec 0 Task 6 实施期发现（`docs/development-trace.md` Spec 0 Task 6 根因 1）  
检查方式：`grep -rn "port=8080" mcp_servers/` 应不命中；Dockerfile `EXPOSE 8000`

### 18. SHALL NOT 在解析 AgentCore Runtime / Gateway 的 SSE 或 JSON 响应时依赖 `requests` 默认 encoding
AgentCore 返回的 `text/event-stream` 不带 `charset=utf-8`，`requests` 按 RFC 2616 默认按 ISO-8859-1 解 text/* —— 中文等非 ASCII 字段会全烂，但 ASCII 路径无感知（Spec 0 ASCII verify 全 pass，王总实测中文场景才暴露）。  
正确写法：

```python
# SSE 路径
for raw in resp.iter_lines(decode_unicode=False):  # 关键：False
    line = raw.decode("utf-8")                     # 手动 utf-8
# JSON 路径
data = json.loads(resp.content.decode("utf-8"))    # 不用 resp.json()，绕过默认 encoding
```

来源：Spec 0 跑通后 Kiro 集成期发现（`docs/development-trace.md` "Spec 0 跑通后的本机集成" 一节）

### 19. SHALL NOT 信任 alpha L2 keyword 的 Optional 标记 == 服务端 schema 允许该字段为空
`aws_bedrock_agentcore_alpha` 的 jsii 签名经常把字段标 `Optional`，但 AgentCore 服务端 schema 校验比 L2 严：`OAuth2CredentialProvider.using_cognito` 把 issuer / authorization_endpoint 标 Optional，服务端 deploy 时 reject "Missing Issuer" / "Missing AuthorizationEndpoint"；`GatewayProtocol.mcp` 把 supported_versions 标 Optional，服务端 reject "MCP configuration cannot be empty"。  
应对：写 framework Construct 时**显式传齐 alpha 文档列出的所有 endpoint / 版本字段**；遇到 InvalidRequest 报错优先怀疑 L2 签名宽容、服务端严格的 gap，按服务端报错信息逐个补字段。  
来源：Spec 1 Task 6 deploy v3 / v4 / v5 三次同型失败（`docs/development-trace.md` Spec 1 Task 6 修复尝试 #3-#5）

### 20. SHALL NOT 在 Cognito IDP 集成场景下只传 `token_endpoint` 而省略 issuer / authorization_endpoint
Cognito IDP 在 AgentCore Token Vault `OAuth2CredentialProvider.using_cognito` 中是强契约：服务端要求 4 个字段非空 —— `client_id` / `client_secret` / `token_endpoint` / `issuer` / `authorization_endpoint`（其中后两个虽然 alpha 标 Optional，实际必填，对应 SHALL NOT #19）。  
正确拼法（Construct 内部用 CDK token，部署期 CFN 解析）：

```python
cognito_issuer = f"https://cognito-idp.{stack.region}.amazonaws.com/{user_pool.user_pool_id}"
auth_endpoint  = cdk.Fn.join("", [
    cdk.Fn.select(0, cdk.Fn.split("/oauth2/token", token_endpoint)),
    "/oauth2/authorize"
])
```

来源：Spec 1 Task 6 deploy v3 / v4 失败的根因（`docs/development-trace.md` Spec 1 Task 6 修复尝试 #3-#4）

### 21. SHALL NOT 假设 AgentCore Gateway 给上游 tool 加的命名空间分隔符是 `__`（双下划线）
设计文档与 alpha L2 文档曾推测 Gateway 给 target tool 名加 `{target_name}__` 双下划线前缀，**实测是 `___` 三下划线**（如 target=`spec0helloworld` + tool=`hello_world` → 实际暴露 `spec0helloworld___hello_world`）。  
要求：tool 命名空间相关的所有断言、verify 脚本、README 注册指引、project-conventions A7 都以"三下划线"为准，不要在代码或文档里写双下划线占位。  
长度上限不变（target 名 + 3 个下划线 + tool 名 ≤ 64 字符）。  
来源：Spec 1 Task 6 verify_spec1 v1 实测（`docs/development-trace.md` Spec 1 Task 6 修复尝试 #6）

## 维护规则

- 新增条款来源：每轮 spec 实施时记录在 `docs/development-trace.md`，复盘时通用化后回灌到本文件
- 删除条款的唯一理由：该约束已不再适用（如 alpha 包变 stable 后，第 2、10 条可松绑）。删除前必须在 trace 文件留下说明
- 所有条款编号永久不变，只追加不重排，避免引用错位


## V2 实施期回灌(2026-05-23,TAM DevOps MCP Platform)

### 22. SHALL NOT 在不挂 target 的纯 Gateway stack 里假设 alpha L2 会自动给 gateway role 补齐 token vault / workload identity 权限
alpha L2 在 `Gateway.add_mcp_server_target(...)` **同 stack 调用**时才会自动给 gateway role attach 3 组关键权限(workload identity get token / token vault complete-and-get oauth token / secretsmanager `bedrock-agentcore-identity!*`)。生产形态把 Gateway 与 target 拆 stack(GatewayStack 不挂 target,RuntimeStack 用 `Gateway.from_gateway_attributes` + `iam.Role.from_role_arn(..., mutable=False)` 跨 stack 挂 target)时,alpha L2 没机会触发自动加权限,而且 `mutable=False` 也没办法补。结果:`tools/list` 通(纯 Gateway 元数据查询),`tools/call` 必定 `An internal error occurred. Please retry later.`(Gateway 调 Runtime 时换不到 OAuth2 access_token)。  
**应对**:GatewayConstruct 创建 Gateway 后**显式**给 `gateway.role.add_to_principal_policy(...)` 加齐这 3 组 statement,资源 ARN 模板与 v1 alpha 自动生成的对齐:

```python
# wid_dir_arn = arn:aws:bedrock-agentcore:{region}:{account}:workload-identity-directory/default
# wid_arn      = arn:aws:bedrock-agentcore:{region}:{account}:workload-identity-directory/default/workload-identity/{gateway_name}-*
# token_vault_arn = arn:aws:bedrock-agentcore:{region}:{account}:token-vault/default
# oauth_provider_arn_pattern = arn:aws:bedrock-agentcore:{region}:{account}:token-vault/default/oauth2credentialprovider/{gateway_name}-cognito-cc
# bedrock_identity_secret_arn = arn:aws:secretsmanager:{region}:{account}:secret:bedrock-agentcore-identity!*

gateway.role.add_to_principal_policy(iam.PolicyStatement(
    actions=["bedrock-agentcore:GetWorkloadAccessToken",
             "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
             "bedrock-agentcore:GetWorkloadAccessTokenForUserId"],
    resources=[wid_dir_arn, wid_arn]))
gateway.role.add_to_principal_policy(iam.PolicyStatement(
    actions=["bedrock-agentcore:CompleteResourceTokenAuth",
             "bedrock-agentcore:GetResourceOauth2Token"],
    resources=[oauth_provider_arn_pattern, token_vault_arn, wid_dir_arn, wid_arn]))
gateway.role.add_to_principal_policy(iam.PolicyStatement(
    actions=["secretsmanager:GetSecretValue"],
    resources=[bedrock_identity_secret_arn]))
```

来源:V2 实施期 Gateway tools/call internal error 调试(`docs/development-trace.md` "V2 / TAM DevOps MCP Platform — Gateway tools/call internal error 根因与修复" 一节)  
检查方式:`aws iam list-role-policies --role-name {GatewayServiceRole}` 必须至少 1 条 inline policy(对比 v1 必有 `Spec1GatewayServiceRoleDefaultPolicy*`);否则 `tools/list` OK 但 `tools/call` 必定 `An internal error occurred`。


## V2 IAM 重构回灌(2026-05-23,Cognito → AWS_IAM 切换)

### 23. SHALL NOT 用 alpha L2 `GatewayCredentialProvider.from_iam_role()` 直接喂 MCP target 而不补 IamCredentialProvider 字段
alpha L2(`aws_bedrock_agentcore_alpha 2.254.0a0`)在 `GatewayTarget.for_mcp_server(... credential_provider_configurations=[from_iam_role()])` 路径下只生成:
```json
{"CredentialProviderType": "GATEWAY_IAM_ROLE"}
```
但服务端 schema 要求 MCP server target 显式声明 IamCredentialProvider 的 Service / Region:
```json
{"CredentialProviderType": "GATEWAY_IAM_ROLE",
 "CredentialProvider": {"IamCredentialProvider": {"Service": "bedrock-agentcore", "Region": "us-east-1"}}}
```
不补会在部署期报 `InvalidRequest: IamCredentialProvider.Service is required`。

**应对**:framework/gateway_construct.py 内部用 L1 escape hatch 补:
```python
cfn_target = target.node.default_child
cfn_target.add_property_override(
    "CredentialProviderConfigurations.0.CredentialProvider.IamCredentialProvider.Service",
    "bedrock-agentcore")
cfn_target.add_property_override(
    "CredentialProviderConfigurations.0.CredentialProvider.IamCredentialProvider.Region",
    stack.region)
```
来源:V2 IAM 重构期 alpha 探针实测(`docs/development-trace.md` "V2 IAM 全切重构" 一节)。

### 24. SHALL NOT 在多个 stack 里用相同 construct id 引用同一个跨 stack 角色并 `mutable=True`
CDK 自动给 mutable role 加 IAM::Policy 时,Policy 的 logical id 基于 construct id hash。两个 stack 用相同的 construct id(如 `ImportedGatewayRole`)引用同一个 role,生成的 Policy logical id 一样 → CFN 并行 deploy 时第二个 stack 报 `policy already exists on the role`。

**应对**:每个 stack 用唯一 construct id(`ImportedGatewayRolePg` / `ImportedGatewayRoleValkey` 等),保证 hash 不冲突。

来源:V2 IAM 重构期实施(`docs/development-trace.md` 同一节,坑 1)。


## V2 tool 扩展期回灌(2026-05-23,Gateway tools/list bug)

### 25. SHALL NOT 假设 AgentCore Gateway tools/list 数量等于上游 Runtime tools/list 数量
AgentCore Gateway alpha 阶段 tools/list 存在 sync 截断 bug:Runtime 上游暴露 N 个 tool,Gateway tools/list 可能漏 1+(实测 Valkey 11 → Gateway 10,缺 inspect_stats)。已尝试 explicit synchronize-gateway-targets / 改 description 触发 update / 等 lastSync 完成 — 全部无效。**重要**:被漏的 tool 实际通过 `tools/call` 仍可正常调用并返回真实数据,只是不在 list。

**应对**:
- verify 脚本不能只断言 `len(tools_list) == N`,要兼容 `N-1` 同时附加显式 `tools/call` 探针
- 客户文档明确告知:tool 列表数字与实际可用数字可能不一致,以 Runtime 直查为准
- 监控 / 巡检自动化脚本如果按 tool 名单遍历调用,要从 Runtime 拿全集而不是 Gateway

来源:V2 tool 扩展期实施(`docs/development-trace.md` "V2 Tool 集扩展" 一节)。
检查方式:对 Runtime invocation URL 直查 tools/list,对 Gateway URL 也查 tools/list,diff 两者得到"隐形可调用"清单;对差集的每个 tool 跑 tools/call 验证可用。


## V2 客户一键部署加固回灌(2026-06-03,交付客户前系统检查)

### 26. SHALL NOT 把客户必填参数的"占位符默认值"放 `cdk.json`、真实值放 `cdk.context.json` 指望覆盖
CDK context 优先级是 **CLI `--context` > `cdk.json` > `cdk.context.json`**(只有 CLI 能盖过 `cdk.json`)。
若把 `<REPLACE_ME_*>` 占位符放 `cdk.json`、真实值放 `cdk.context.json`,占位符永远赢,`cdk synth` 读到的是占位符 → 部署失败或连错环境。
**应对**:客户参数**只放 `cdk.context.json`**(已 gitignore,每环境一份)+ 提供 `cdk.context.json.example` 全占位符模板;`cdk.json` 只放 CDK feature flag,两者键不重叠。
来源:V2 客户一键部署加固(`docs/development-trace.md` "V2 客户一键部署加固" 一节)。
检查方式:`grep -E 'rds:|valkey:|runtime:' cdk.json` 应无命中(客户参数不在 cdk.json)。

### 27. SHALL NOT 在交付客户的 stack 里给客户必填参数设"开发者自家环境"的兜底默认值
若 `stacks/*.py` 用 `self.node.try_get_context(key) or _DEFAULT_自家VPC` 这种兜底,客户漏配时会**静默连到开发者的数据源**(看似部署成功,实则连错库,极难发现)。
**应对**:必填参数用强校验 helper(如 `_require_context()`),值缺失或仍是 `<REPLACE_ME_*>` 时**立即抛错并指明缺哪个 key + 怎么补**,绝不兜底。可同时校验关联参数长度一致(如 subnetIds 与 availabilityZones 数量)。
来源:V2 客户一键部署加固(`docs/development-trace.md` 同一节)。
检查方式:`grep -rn "_DEFAULT_.*vpc-\|_DEFAULT_.*jupiter\|or \"vpc-" V2/stacks/` 应无命中。
