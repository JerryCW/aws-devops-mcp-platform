# Requirements Document

## Introduction

**Spec 2:RDS MySQL VPC 接入 + 第一个真实巡检 tool**

Spec 0 完成 AgentCore Runtime 骨架,Spec 1 完成 Gateway 聚合 + DevOps Agent 真实链路。Spec 2 把框架的"hello world 占位"替换为真实业务场景:**让 DevOps Agent 通过 Gateway 调用一个 RDS MySQL 巡检 tool,从私有子网中的 RDS 实例读取长事务列表**。

**Spec 2 单一目标**:在 Spec 1 Gateway 之上新增第二个 MCP target,后端是部署在 VPC 私有子网的 AgentCore Runtime,运行第一个真实巡检 tool `rds_mysql_inspect_long_transactions`,链路如下:

```
DevOps Agent
  → Spec 1 Gateway(已在线,加第二个 target)
  → Spec 2 Runtime(VPC 模式,jupiter-dev VPC 私有子网)
  → jupiter-dev-slurm-db(MySQL 8.0.44)
  → 返回长事务结构化结果
```

完成后 DevOps Agent 端能调 `rdsmysql___rds_mysql_inspect_long_transactions(cluster_endpoint, threshold_seconds=60)` 拿到结构化的"长事务巡检报告"。

**前置条件**:
- Spec 0 已通过 ✅(`McpInspectSpec0Stack` 在线)
- Spec 1 已通过 ✅(`McpInspectSpec1Stack` 在线,Gateway `spec1-gateway-ynylmr84rj` 在 DevOps Agent 注册成功)
- AgentCore Runtime 在 us-east-1 alpha L2 支持 VPC 模式(Spec 2 Task 1 探针实测确认;若不支持则 Spec 2 拆为 2a/2b)
- `jupiter-dev-slurm-db` MySQL 8.0.44 实例在线(实测属性归档于 `docs/development-trace.md` "Spec 2 起草前置事实")
- `mcp_devops_ro` DB user 已由王总在 RDS admin session 手工创建(Task 5 README 给一键脚本)

**单一验证条件**:

```bash
cdk deploy McpInspectSpec2Stack --require-approval never && python scripts/verify_spec2.py
```

末尾打印 `✅ Spec 2 verification passed`,退出码 0 = pass。

## Glossary

| 术语 | 定义 |
|------|------|
| **Spec 2 Runtime** | Spec 2 部署的 AgentCore Runtime,name = `spec2_rds_mysql`,protocol = MCP,VPC 模式 |
| **Spec 2 MCP server** | `mcp_servers/rds_mysql/` 容器,FastMCP + 第一个巡检 tool `rds_mysql_inspect_long_transactions` |
| **目标 RDS** | jupiter-dev-slurm-db,MySQL 8.0.44,在 vpc-0e4a299032154a1be 的 3 个 db 子网,SG = sg-09b483e0cb2e97f69 |
| **巡检 DB User** | `mcp_devops_ro`,read-only 全库 + PROCESS,密码存 Secrets Manager `mcp-inspect/rds-mysql/devops-readonly`,DevOps 巡检场景 100 个库共用一套(决策见 conventions A5 修订记录) |
| **巡检凭据 Secret** | Secrets Manager `mcp-inspect/rds-mysql/devops-readonly`,JSON `{"username": "mcp_devops_ro", "password": "..."}`,Spec 2 Stack 创建空壳,密码由王总 RDS admin session 一键脚本写入 |
| **MCP Target Name** | 字符串 `rdsmysql`(9 字符,符合 conventions A10 字符集) |
| **Tool 全名** | DevOps Agent 看到的工具名 = `rdsmysql___rds_mysql_inspect_long_transactions`(46 字符,< 64,三下划线分隔符见 conventions A7) |
| **长事务** | `information_schema.innodb_trx.trx_started` 距当前时间 ≥ `threshold_seconds`(默认 60 秒)的活跃事务 |

## Requirements

### Requirement 1:Spec 2 AgentCore Runtime VPC 部署

**User Story:** 作为框架开发者,我想要 Spec 2 Runtime 部署到 jupiter-dev VPC 的 3 个私有子网,以便 Runtime 容器能直接通过 VPC 内 SG 访问 jupiter-dev-slurm-db。

#### Acceptance Criteria

1. WHEN 开发者执行 `cdk deploy McpInspectSpec2Stack`, THE Spec_2_Stack SHALL 在 us-east-1 创建恰好一个 AgentCore Runtime,name = `spec2_rds_mysql`,protocol = MCP,network mode = VPC
2. WHEN Runtime 创建, THE Spec_2_Stack SHALL 把 Runtime 的 VPC 配置设为 `vpc-0e4a299032154a1be`,subnet 集合 = `[subnet-0508babc46b955ace, subnet-02b34ec2b4fae5edc, subnet-06f76750dd1e3ff35]`(jupiter-dev-vpc-db 3 个 AZ 私有子网)
3. WHEN Runtime 创建, THE Spec_2_Stack SHALL 给 Runtime 创建一个 Security Group(`sg-spec2-runtime`),允许 outbound 到 jupiter-dev-slurm-db SG `sg-09b483e0cb2e97f69` 的 3306 端口
4. WHEN Runtime 创建, THE Spec_2_Stack SHALL 给 jupiter-dev-slurm-db 的 SG `sg-09b483e0cb2e97f69` 添加一条 inbound 规则,允许来自 Runtime SG 的 3306 端口入站(通过 EC2 SG ingress rule,不修改 SG 名称)
5. WHEN Runtime 创建, THE Spec_2_Stack SHALL 配置 Runtime 入站 authorizer 为 Cognito JWT,引用 Spec 0 User Pool 与 M2M Client(通过 SSM Parameter `/mcp-inspect/spec0/cognito-user-pool-id` + Spec 0 Output `CognitoClientId` / `CognitoClientSecretArn`)
6. WHEN Runtime 创建, THE Spec_2_Stack SHALL 启用 X-Ray tracing 并把 application logs 投递到 CloudWatch LogGroup `/aws/bedrock-agentcore/runtimes/spec2_rds_mysql`
7. WHEN `cdk deploy` 完成且 Stack 状态为 `CREATE_COMPLETE`, THE Spec_2_Stack SHALL 通过 CloudFormation Output 暴露 `RuntimeArn` / `RuntimeUrl`,且 export_name 为 `McpInspectSpec2Stack-{OutputKey}`(conventions A9)
8. IF Runtime 创建失败导致 stack rollback, THEN THE Spec_2_Stack SHALL 不留任何残留 ENI / SG inbound 规则在 jupiter-dev-slurm-db SG 上

### Requirement 2:RDS MySQL 巡检 tool 容器

**User Story:** 作为框架开发者,我想要一个 MCP server 容器,实现 `rds_mysql_inspect_long_transactions` 工具,从 jupiter-dev-slurm-db 读取长事务并返回 conventions A8 标准结构。

#### Acceptance Criteria

1. WHEN 开发者构建 `mcp_servers/rds_mysql/`, THE Container SHALL 基于 `python:3.13-slim` linux/arm64,Dockerfile 第一行强制 `FROM --platform=linux/arm64 ...`
2. WHEN 容器启动, THE FastMCP_Instance SHALL 监听 `0.0.0.0:8000`,mount path `/mcp`,`stateless_http=True`(对应 conventions MCP 协议契约表)
3. WHEN tool `rds_mysql_inspect_long_transactions` 被调用, THE Tool SHALL 接受参数 `cluster_endpoint: str`(默认空,空时用环境变量 `DEFAULT_CLUSTER_ENDPOINT`,Spec 2 Construct 注入 jupiter-dev-slurm-db endpoint)+ `threshold_seconds: int = 60`(可选)+ `database: str = "mysql"`(默认 system DB,只用于建连接,查 `information_schema.innodb_trx` 不依赖业务库)
4. WHEN Tool 执行, THE Tool SHALL 通过 Secrets Manager `mcp-inspect/rds-mysql/devops-readonly` 拉取凭据(每次调用拉一次,不缓存到容器全局),用 `pymysql` 建立连接
5. WHEN Tool 查询 MySQL, THE Tool SHALL 仅执行以下固化 SQL,不接受任何动态拼接:
   ```sql
   SELECT trx_id, trx_state, trx_started,
          TIMESTAMPDIFF(SECOND, trx_started, NOW()) AS duration_seconds,
          trx_mysql_thread_id, trx_query, trx_rows_modified, trx_rows_locked
   FROM information_schema.innodb_trx
   WHERE TIMESTAMPDIFF(SECOND, trx_started, NOW()) >= %s
   ORDER BY duration_seconds DESC
   LIMIT 100
   ```
   传 `threshold_seconds` 作为参数化绑定(防 SQL injection,即使 LLM 试图传恶意值也无法越权)
6. WHEN Tool 返回, THE Tool SHALL 输出符合 conventions A8 的结构化 dict:
   ```python
   {
     "status": "ok" | "warning" | "critical",
     "findings": [{"severity": "warning|critical", "metric": "long_running_trx", "value": "<duration>s", "threshold": "<threshold>s"}],
     "raw_data": {"cluster_endpoint": "...", "threshold_seconds": ..., "transactions": [...]},
     "recommendation": "..."
   }
   ```
   - `status = critical`:存在 ≥1 条事务 `duration_seconds ≥ 5 * threshold_seconds`
   - `status = warning`:存在 ≥1 条事务 `duration_seconds ≥ threshold_seconds`(但 < 5×)
   - `status = ok`:无长事务
7. IF MySQL 连接失败 / 凭据错误 / SQL 执行错误, THEN THE Tool SHALL 抛 RuntimeError 带完整错误链(包含 host / database 但**不**含 password),由 MCP 协议层封装为 JSON-RPC error 返回给客户端(SHALL NOT #15 完整 trace)
8. THE Container SHALL NOT 在镜像中包含任何凭据 / `.env` / `.aws/`,通过 `.dockerignore` 强制排除
9. THE Tool SHALL 严格遵守 SHALL NOT #3 — **不**实现任何通用 SQL 执行 tool,SQL 全部固化在容器代码中

### Requirement 3:数据库巡检凭据 Secret

**User Story:** 作为框架开发者,我想要一个 Secrets Manager secret 存放 `mcp_devops_ro` 凭据,以便所有 RDS 实例的巡检 tool 共用一套凭据(conventions A5 修订定义)。

#### Acceptance Criteria

1. WHEN `cdk deploy McpInspectSpec2Stack`, THE Spec_2_Stack SHALL 创建恰好一个 Secrets Manager Secret,name = `mcp-inspect/rds-mysql/devops-readonly`,JSON 模板 `{"username": "mcp_devops_ro", "password": "PLACEHOLDER_REPLACE_BY_DBA"}`
2. THE Spec_2_Stack SHALL 把该 Secret 的 `RemovalPolicy` 设为 `DESTROY`(SHALL NOT #14)
3. THE Spec_2_Stack SHALL NOT 在 CDK 代码中写入任何真实密码 — 默认值是 PLACEHOLDER 占位字符串,真实密码由王总在 RDS admin session 用 `aws secretsmanager put-secret-value` 一键脚本灌入(脚本由 Task 5 README 提供)
4. WHEN Runtime 拉 Secret, THE Runtime_Execution_Role SHALL 拥有 `secretsmanager:GetSecretValue` 权限,资源段限定到 `arn:aws:secretsmanager:us-east-1:{account}:secret:mcp-inspect/rds-mysql/*`(conventions A5 IAM 路径限权)
5. THE Spec_2_Stack SHALL 给该 Secret 配置 KMS 加密(默认 AWS 托管 `aws/secretsmanager`,不自建 CMK 以减少 Spec 2 资源数)
6. THE Verify_Spec2_Script SHALL 在脚本前置检查中验证 Secret 中 password 字段不是 `PLACEHOLDER_REPLACE_BY_DBA`,若是则非零退出并明确提示"先跑王总手工脚本灌入真密码"

### Requirement 4:挂载到 Spec 1 Gateway 的第二个 MCP Target

**User Story:** 作为框架开发者,我想要 Spec 2 Runtime 注册为 Spec 1 Gateway 的第二个 MCP target(name = `rdsmysql`),以便 DevOps Agent 通过单一 Gateway 端点调用 RDS 巡检工具。

#### Acceptance Criteria

1. WHEN `cdk deploy McpInspectSpec2Stack`, THE Spec_2_Stack SHALL 在 Spec 1 Gateway(`spec1-gateway-ynylmr84rj`,gatewayId 通过 SSM Parameter 跨 stack 引用)上注册一个 MCP target,name = `rdsmysql`
2. WHEN target 注册, THE Spec_2_Stack SHALL 把 endpoint 设为 Spec 2 Runtime 的 invocation URL(从本 stack 内部直接引用,不走 cross-stack import)
3. WHEN target 注册, THE Spec_2_Stack SHALL 把 outbound credential provider 绑定到 Spec 1 已建的 OAuth2CredentialProvider(`spec1-gateway-cognito-cc`,通过 ARN 引用,不重新创建)
4. WHEN target 注册完成, THE Gateway SHALL 在 `tools/list` 响应中**同时**返回:
   - `spec0helloworld___hello_world`(Spec 1 已注册的 hello target)
   - `rdsmysql___rds_mysql_inspect_long_transactions`(本 spec 新增)
5. WHILE Spec 1 Gateway 不可用 / Spec 2 Runtime 不可用 / Spec 1 OAuth Provider 不可用任意一项, THE Gateway SHALL NOT 在 `tools/list` 响应中返回 `rdsmysql___rds_mysql_inspect_long_transactions`(严格 AND 语义,沿用 Spec 1 Requirement 2.5)
6. WHEN `cdk destroy McpInspectSpec2Stack`, THE Spec_2_Stack SHALL 仅删除新增的 target,**不**触碰 Spec 1 的 hello target 与 Gateway 本身

### Requirement 5:跨 Spec 引用与命名隔离

**User Story:** 作为框架开发者,我想要 Spec 2 资源严格独立于 Spec 0/1,通过跨 stack reference 关联,以便 Spec 2 能独立 destroy 不影响前序 spec。

#### Acceptance Criteria

1. WHEN `cdk synth`, THE Spec_2_Stack SHALL 通过 `Fn.import_value` 引用 Spec 0 Output 的至少 4 个:`CognitoClientId` / `CognitoClientSecretArn` / `CognitoOAuthScope` / `CognitoTokenEndpoint`(全部已在 Spec 1 Task 3 补 export_name=,合规)
2. WHEN `cdk synth`, THE Spec_2_Stack SHALL 通过 SSM Parameter `value_for_string_parameter` 引用 `/mcp-inspect/spec0/cognito-user-pool-id`(Spec 1 Task 2 已建立)以及新增 `/mcp-inspect/spec1/gateway-id`(本 spec Task 2 由 boot 脚本写入,因为 Spec 1 没显式 export `GatewayId` 这个 OutputKey 的形式给 Spec 2 直接用 — 走 SSM 中转更稳)
3. WHEN `cdk deploy McpInspectSpec2Stack`, THE Spec_2_Stack SHALL 不创建任何 Cognito / Gateway / OAuth2CredentialProvider 资源(全部由 Spec 0/1 提供,通过 from_xxx_id / from_xxx_arn 工厂引用)
4. WHEN `cdk destroy McpInspectSpec2Stack`, THE Spec_2_Stack SHALL 完全删除 Spec 2 创建的 Runtime / Target / SG / SG ingress rule / Secret / LogGroup,且不触碰 Spec 0/1 任何资源
5. WHEN destroy 后查询 AWS, THE Spec_0_Stack 与 Spec_1_Stack SHALL 仍然可以独立运行,且 Spec 0/1 的 verify 脚本仍 pass(回归)

### Requirement 6:端到端验证脚本

**User Story:** 作为框架开发者,我想要一个一键验证脚本回归 Spec 2 的端到端链路。

#### Acceptance Criteria

1. WHEN 开发者执行 `python scripts/verify_spec2.py`, THE Verify_Script SHALL 自动从 CloudFormation 读取 3 个 Stack 的 Output(Spec 0/1/2)
2. WHEN Verify_Script 运行, THE Verify_Script SHALL 验证 Secret `mcp-inspect/rds-mysql/devops-readonly` 中 password 已被王总替换为真实密码(若仍是 PLACEHOLDER 则 fail-fast 并提示)
3. WHEN Verify_Script 运行, THE Verify_Script SHALL 用 Spec 0 Cognito M2M client_credentials 拿 access_token(scope=mcp/invoke)
4. WHEN 拿到 token, THE Verify_Script SHALL 向 Spec 1 Gateway URL 发 MCP `initialize` + `tools/list`,断言返回的 tools 中**同时**包含 `spec0helloworld___hello_world` 与 `rdsmysql___rds_mysql_inspect_long_transactions`
5. WHEN tools 列表断言通过, THE Verify_Script SHALL 调用 `tools/call name=rdsmysql___rds_mysql_inspect_long_transactions arguments={"threshold_seconds": 60}`(不传 cluster_endpoint,用容器默认值)
6. WHEN Tool 返回, THE Verify_Script SHALL 断言返回结构符合 conventions A8(包含 `status` / `findings` / `raw_data` / `recommendation` 4 个 key),且 `status` 是 `ok` / `warning` / `critical` 之一
7. WHEN 所有断言通过, THE Verify_Script SHALL 打印 `✅ Spec 2 verification passed` + sys.exit(0)
8. IF 任意步骤失败, THEN THE Verify_Script SHALL 打印完整 Python traceback + sys.exit(1)(SHALL NOT #15)
9. THE Verify_Script SHALL NOT 在 stdout 输出 access_token / DB password / 完整 Authorization header(SHALL NOT #12 / #17)
10. THE Verify_Script SHALL NOT 在 stdout 完整打印 raw_data 中的事务 query 内容(可能含业务 SQL 语句,语义敏感),只打印 transaction count 与 max duration

### Requirement 7:Runtime 执行角色最小权限

**User Story:** 作为框架开发者,我想要 Spec 2 Runtime 执行角色严格最小权限。

#### Acceptance Criteria

1. WHEN Runtime 创建, THE Spec_2_Stack SHALL 给 Runtime 执行角色赋予恰好以下权限:
   - 沿用 Spec 0 L2 默认的 ECR pull / CloudWatch Logs / X-Ray / Workload Identity 权限(Construct 复用)
   - 新增 `secretsmanager:GetSecretValue` 限定到 `arn:aws:secretsmanager:us-east-1:{account}:secret:mcp-inspect/rds-mysql/*`(conventions A5)
   - 新增 EC2 Network Interface 权限(VPC 模式 Runtime 必需,L2 自动加,本条 Acceptance 仅记录事实,不要求显式授权)
2. THE Spec_2_Stack SHALL NOT 给 Runtime 执行角色赋予 `*:*` / `AdministratorAccess` / 任何 AWS 托管的过权限策略(SHALL NOT #11)
3. THE Spec_2_Stack SHALL NOT 给 Runtime 执行角色赋予 `rds:*` / `rds-data:*` / `rds-db:connect` 等 RDS 控制面权限 — Runtime 通过**网络层 + DB user/password** 访问 RDS,不需要 RDS API 权限

### Requirement 8:框架 Construct 抽象与 alpha 隔离

**User Story:** 作为框架开发者,我想要 VPC 模式的 Runtime 通过扩展 framework/runtime_construct.py 实现,保持 alpha 隔离边界不变。

#### Acceptance Criteria

1. WHEN 开发者实现 Spec 2 Runtime, THE Framework SHALL 扩展现有 `framework/runtime_construct.py` 的 `McpInspectRuntime` Construct,新增可选参数:
   - `vpc: ec2.IVpc | None = None`(stable CDK 类型)
   - `subnets: list[ec2.ISubnet] | None = None`(stable CDK 类型)
   - `security_groups: list[ec2.ISecurityGroup] | None = None`(stable CDK 类型)
   传 None 时保持 Spec 0 行为(Public Network);传齐时 Construct 内部转成 alpha L2 的 NetworkConfiguration(VPC 模式)
2. WHEN 对 codebase 执行 `grep -rn "aws_bedrock_agentcore_alpha" --include="*.py" --exclude-dir=.venv --exclude-dir=cdk.out .`, THE Framework SHALL 仅匹配 `framework/runtime_construct.py` 与 `framework/gateway_construct.py` 两个文件(Spec 1 已建立,Spec 2 不变)
3. THE Spec_2_Stack(`stacks/spec_two_stack.py`)SHALL NOT 直接 import `aws_bedrock_agentcore_alpha`
4. THE Spec_2_Stack SHALL 通过 `cognito.UserPool.from_user_pool_id(...)` / `secretsmanager.Secret.from_secret_complete_arn(...)` / `agentcore` Construct 等方式引用 Spec 0/1 资源,所有跨 stack 引用必须经过 stable CDK 类型边界

### Requirement 9:cdk destroy 完全清理

**User Story:** 作为框架开发者,我想要 Spec 2 资源能被 `cdk destroy` 完全清理。

#### Acceptance Criteria

1. WHEN `cdk destroy McpInspectSpec2Stack`, THE Spec_2_Stack SHALL 把所有自身创建的资源(Runtime / SG / SG ingress / Secret / LogGroup / Target / 执行角色)的 `RemovalPolicy` 设为 `DESTROY`
2. WHEN destroy 完成, THE Verification SHALL 通过 AWS CLI 验证以下全部为空:
   - `aws bedrock-agentcore-control list-agent-runtimes --query 'agentRuntimes[?agentRuntimeName==\`spec2_rds_mysql\`]'`
   - `aws bedrock-agentcore-control list-gateway-targets --gateway-identifier <Spec1 gateway> --query 'items[?name==\`rdsmysql\`]'`
   - `aws secretsmanager list-secrets --query 'SecretList[?Name==\`mcp-inspect/rds-mysql/devops-readonly\`]'`
3. IF AWS CLI 在 destroy 后仍能查到任意 Spec 2 资源, THEN THE Verification SHALL 立即非零退出,SHALL NOT 因 stack 状态 DELETE_COMPLETE 就视为通过
4. THE Spec_2_Stack SHALL NOT 把任何资源的 `removalPolicy` 设为 `RETAIN`
5. WHEN destroy 完成, THE Verification SHALL 验证 jupiter-dev-slurm-db SG `sg-09b483e0cb2e97f69` 的 inbound 规则中**不**残留来自 Spec 2 Runtime SG 的 3306 入站规则

## Constraints

| 类别 | 约束 |
|------|------|
| 部署区域 | us-east-1(与 Spec 0/1 一致) |
| Python 版本 | 3.13 |
| CDK 主包版本 | `aws-cdk-lib == 2.254.0`(沿用) |
| CDK alpha 包版本 | `aws-cdk.aws-bedrock-agentcore-alpha == 2.254.0a0`(沿用) |
| MCP SDK 版本 | `mcp ~= 1.27`(与 Spec 0 一致) |
| MySQL 客户端 | `pymysql >= 1.1, < 2.0`(纯 Python,无 C 扩展,arm64 编译简单) |
| 容器平台 | linux/arm64 |
| 容器 port / mount | 8000 / `/mcp`(MCP 协议契约,project-conventions 协议表) |
| 网络模式 | VPC,subnets = jupiter-dev-vpc-db 3 个 AZ 私有子网 |
| Runtime name | `spec2_rds_mysql`(下划线,符合 conventions A10 Runtime 命名) |
| Target name | `rdsmysql`(9 字符,无下划线无 hyphen) |
| Tool 全名 | `rdsmysql___rds_mysql_inspect_long_transactions`(46 字符 < 64) |
| 部署 + verify 总时长 | ≤ 15 分钟(VPC 模式 Runtime 比 Public 慢约 1-2 分钟) |
| 本 Spec 改动量 | 200-500 行新增代码,新增文件 ≤ 12 个 |
| 本 Spec 任务粒度 | 4-7 个 task |
| MySQL 引擎版本 | jupiter-dev-slurm-db = MySQL 8.0.44(`information_schema.innodb_trx` 标准 schema) |

## SHALL NOT(按三层分类)

### Requirements 层(功能边界)

1. **SHALL NOT** 在 Spec 2 创建任何 Cognito 资源,沿用 Spec 0(SHALL NOT #1 + Spec 1 沿用)
2. **SHALL NOT** 在 Spec 2 创建新的 Gateway,挂在 Spec 1 已有 Gateway 上(conventions A2 单 Gateway 策略)
3. **SHALL NOT** 在 Spec 2 创建第二个 OAuth2CredentialProvider,沿用 Spec 1 的 `spec1-gateway-cognito-cc`(用 ARN 引用)
4. **SHALL NOT** 在 Spec 2 注册除 RDS MySQL 之外的任何数据源 target(Redis / DynamoDB 留给 Spec 4+)
5. **SHALL NOT** 给 jupiter-dev-slurm-db SG 加 0.0.0.0/0 的 3306 入站(只允许来自 Runtime SG 的 SG 引用)

### Design 层(架构 / API 选择)

6. **SHALL NOT** 直接用 `pymysql` 之外的 MySQL 驱动(`mysqlclient` 需要 C 扩展,arm64 容器构建复杂;`mysql-connector-python` 体积大且许可证复杂)
7. **SHALL NOT** 在 tool 内缓存 Secrets Manager 拉到的 password 到容器全局变量(每次 tool call 拉一次,密码轮换可即时生效;轻量场景性能开销可忽略)
8. **SHALL NOT** 把 RDS endpoint 硬编码到 mcp_servers 容器代码(通过环境变量 `DEFAULT_CLUSTER_ENDPOINT` 注入,方便后续 spec 扩展到其他 RDS 实例)
9. **SHALL NOT** 给 Spec 2 Runtime 容器添加 admin 类 MySQL 操作 tool(`DROP TABLE` / `KILL QUERY` / `ALTER USER` 等),哪怕是"DBA 自动化场景"也不允许 — 这是 SHALL NOT #3 的延伸

### Tasks 层(实现写法 / 安全 / 边界)

10. **SHALL NOT** 在 verify_spec2.py 的 stdout 打印事务 raw query SQL 内容(可能泄露业务 SQL 模式),只打印 transaction count + max duration
11. **SHALL NOT** 用 `cdk` 管理 RDS admin 凭据 / 用 `cdk` 在 RDS 上建 user — 这两件事由王总在 RDS admin session 手工做,Spec 2 README 给一键脚本
12. **SHALL NOT** 把 Spec 2 Stack 的 Secret(`mcp-inspect/rds-mysql/devops-readonly`)的密码字段在 cdk 模板中以任何形式硬编码 — 默认值是 PLACEHOLDER 占位,真实密码 deploy 后由王总用 AWS CLI 灌入

## 明确不包含(Out of Scope)

| 事项 | 归属 |
|------|------|
| 第二个 RDS 实例的巡检 | Spec 3+(Spec 2 单库验证,工具已能复用,只需扩 secret 多 endpoint) |
| 第二个 MySQL 巡检 tool(`inspect_lock_waits` / `inspect_replication_lag` 等) | Spec 3 |
| Aurora MySQL 兼容 | Spec 3(Aurora 与 RDS MySQL `information_schema.innodb_trx` 兼容,代码通用,但需要补 Aurora 端到端验证) |
| Redis / DynamoDB / PostgreSQL 数据源 | Spec 4+ |
| RDS IAM Authentication 替代密码 | Spec 4+(凭据零泄露天花板,需要 RDS 配置改造) |
| 巡检结果归档到 S3 / 通知 SNS | Spec 5+ |
| 跨账号 / 跨 region 巡检 | Spec 5+ |
| 框架抽象提炼 / cookiecutter | 等 Spec 2/3/4 经验积累后再抽象 |

## 风险与备注

- **VPC 模式 alpha L2 风险**:Runtime VPC keyword 实际签名待 Task 1 探针实测;若 alpha 不直接支持 VPC,需要 L1 escape hatch 改 NetworkConfiguration,Construct 代码量增加。**Task 1 实测后若发现 alpha 完全不支持 VPC,本 spec 拆为 2a/2b**(2a 用 L1 escape hatch,2b 等 alpha 支持后切回 L2)
- **NAT 出口费用**:Runtime 容器拉 ECR / 调 Cognito / 调 Secrets Manager 全走同一 NAT,Spec 2 上线后每天数据出口费用预计 < 1 USD(单 Runtime 流量极低)
- **jupiter-dev-slurm-db SG 加白**:Spec 2 deploy 会改这个 SG 的 inbound,destroy 必须可逆。建议 Task 6 destroy 后用 AWS CLI 显式验证 SG 规则数,避免残留(Requirement 9.5)
- **`mcp_devops_ro` user 一旦权限收紧 / 被回收,所有数据源 tool 同时失败**:Mode A 单 secret 多库的爆炸半径就是这条 — 用户接受,因为巡检场景断了重新建即可
- **MySQL 8.0.44 `information_schema.innodb_trx`**:权威 schema,query 字段可能含敏感业务 SQL,verify 脚本只打 count + max duration(SHALL NOT #10)
- **RDS admin 凭据**:Spec 2 实施期间 user 的创建依赖王总有 RDS admin 权限,Task 5 README 给一键脚本但具体执行场景(本机 / 堡垒机 / SSM Session Manager)由王总决定
