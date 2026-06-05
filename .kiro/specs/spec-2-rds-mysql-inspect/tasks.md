# Implementation Plan

## Overview

Spec 2 拆 6 个任务,严格按 meta-harness "3-7 task / 100-500 行 / 一个 PR" 原则。

**最终验证条件**(不变):

```bash
bash scripts/bootstrap_spec2.sh \
  && cdk deploy McpInspectSpec2Stack --require-approval never \
  && python scripts/verify_spec2.py
```

末尾打印 `✅ Spec 2 verification passed`,退出码 0 = pass。

> 前置:`bash scripts/init_rds_user.sh`(王总在 RDS admin session)+ RDS admin session 跑 SQL 创建 `mcp_devops_ro` user。这是**人工前置**,不进 cdk 流程,Task 5 README 给完整指引。

**任务设计原则**:
- 每个任务有明确"完成判据"(可观测事实)
- 每个任务标注覆盖的 Requirement / Correctness Property / SHALL NOT 编号
- Task 1(framework Construct VPC 扩展 + alpha 探针)与 Task 2(boot script + init_rds_user 脚本)可并行
- 二次规则:同一问题修 2 次未解决,停下回去改 spec(沿用 Spec 1 实战经验)

**Spec 2 与 Spec 0/1 的边界**:
- 修改 1 个(`framework/runtime_construct.py` 增量加 vpc / subnets / security_groups 3 个 Optional 入参)
- 新增 7 个文件:`stacks/spec_two_stack.py` / `mcp_servers/rds_mysql/{main.py, Dockerfile, requirements.txt, .dockerignore}` / `scripts/{verify_spec2.py, destroy_spec2.sh, bootstrap_spec2.sh, init_rds_user.sh}`
- alpha 隔离 grep 仍仅命中 `framework/runtime_construct.py` + `framework/gateway_construct.py` 两个文件
- 不动 Spec 0/1 任何 stack 代码 / Construct / verify / destroy 脚本(只 framework Construct 增量扩展,Spec 0 不传新参数走旧路径)
- Spec 1 Gateway 复用,只新增第二个 target

## Tasks

- [x] 1. 探针 alpha L2 Gateway target 引用 + framework Construct VPC 增量扩展(可与 Task 2 并行)
  - **alpha 探针实测**(临时,跑完即删):用 `.venv/bin/python -c` 或一次性 `scripts/_probe_spec2.py` 打印以下类的真实 `inspect.signature` 与关键属性:
    * `agentcore.Gateway.from_gateway_id(...)` 静态工厂(本 spec 用它从 SSM 拿到的 GatewayId 反向构造 IGateway 引用)
    * `agentcore.Gateway` 实例的 `add_mcp_server_target(...)` 在 from_xxx_id 引用模式下是否仍可用(若不可用,落地需要 L1 escape hatch `CfnGatewayTarget`)
    * `agentcore.GatewayCredentialProvider.from_oauth_identity_arn(...)` 静态工厂(本 spec 通过 ARN 跨 stack 引用 Spec 1 OAuthProvider,不重建)
  - 把每个签名 + 属性名实测结果记到 `docs/development-trace.md` 的 Spec 2 实施期 trace section,作为后续 task 的事实依据;若发现 from_gateway_id 不存在或 add_mcp_server_target 不能用在 from_xxx_id 引用上,**立即停下,回头改 design.md Decision 5**(走 L1 CfnGatewayTarget),不做盲目尝试
  - 修改 `framework/runtime_construct.py`,增量加 3 个 Optional 入参(`vpc: ec2.IVpc | None = None` / `subnets: ec2.SubnetSelection | None = None` / `security_groups: list[ec2.ISecurityGroup] | None = None`)+ 暴露 `execution_role: iam.IRole` 属性(Spec 2 Stack 给 Runtime role 加 SecretsManager 读权限要用)
  - Construct 内部分支:三参数全 None 时不传 `network_configuration`(等价 Spec 0 行为);任一参数非 None 时调 `agentcore.RuntimeNetworkConfiguration.using_vpc(...)` 拼 VPC 模式
  - **不**修改 `stacks/spec_zero_stack.py` 与 `stacks/spec_one_stack.py`,Spec 0/1 不传新参数自然走旧路径
  - 验证 1:`.venv/bin/python -c "from framework.runtime_construct import McpInspectRuntime"` exit 0,无 ImportError
  - 验证 2:`grep -rn "aws_bedrock_agentcore_alpha" --include="*.py" --exclude-dir=.venv --exclude-dir=cdk.out .` 仅匹配 `framework/runtime_construct.py` 与 `framework/gateway_construct.py` 两个文件(Property 7)
  - 验证 3:`getDiagnostics framework/runtime_construct.py` 无错误
  - 验证 4:`PATH=".venv/bin:$PATH" npx --yes cdk@2.1122.0 synth McpInspectSpec0Stack` 与 Spec 1 Task 6 实测的模板 hash 等价(cdk diff 无变化,Spec 0 不被破坏);若 hash 变化,Construct 内部分支逻辑要修(传 None 不要走 using_public_network 显式路径,直接不传 network_configuration)
  - _Requirements: 8.1, 8.2, 8.3_
  - _Correctness Property: Property 7_
  - _SHALL NOT covered: #2(alpha 隔离)、#10(alpha 类型不外漏)、#11(优先 L2,无 Cfn*)_

- [x] 2. 实现 boot 脚本 + RDS user 一次性脚本(可与 Task 1 并行)
  - 创建 `scripts/bootstrap_spec2.sh`:把 Spec 1 GatewayId 写到 SSM Parameter `/mcp-inspect/spec1/gateway-id`,沿用 `scripts/bootstrap_spec1.sh` 的 bash 风格(`set -euo pipefail` / `cd REPO_ROOT` / 失败完整 stderr / 幂等 `--overwrite`)
  - 创建 `scripts/init_rds_user.sh`:王总在 RDS admin session 跑的一次性脚本,职责:
    * 用 `aws secretsmanager get-random-password` 生成 32 字符强随机密码(排除 `"`/`@`/`/`/`\`/`` ` ``)
    * 用 `aws secretsmanager put-secret-value` 写到 `mcp-inspect/rds-mysql/devops-readonly`(JSON `{"username":"mcp_devops_ro","password":"..."}`)
    * 把 `CREATE USER` + `GRANT SELECT, PROCESS ON *.*` + `GRANT SELECT ON information_schema.* / performance_schema.*` SQL 写到 `/tmp/spec2_init_rds_user.sql`(0600 权限)
    * 提示王总用 RDS admin 凭据登 jupiter-dev-slurm-db 执行 SQL 后跑 `shred -u /tmp/spec2_init_rds_user.sql`
  - **两个脚本都不能在 stdout 打印密码内容**(SHALL NOT #12 / 本 spec SHALL NOT #12);只可打印长度 / 字段名 / 提示信息
  - 用 `chmod +x` 给两个脚本 0755 权限
  - 验证 1:`bash -n scripts/bootstrap_spec2.sh` exit 0;`bash -n scripts/init_rds_user.sh` exit 0
  - 验证 2:`getDiagnostics scripts/bootstrap_spec2.sh` 无错误;同上 init_rds_user.sh
  - 验证 3(bootstrap_spec2.sh):Spec 1 Stack 在线时跑,exit 0,`aws ssm get-parameter --name /mcp-inspect/spec1/gateway-id --query Parameter.Value --output text` 返回 Spec 1 真实 GatewayId
  - 验证 4(init_rds_user.sh,Spec 2 Secret 不存在时):应 fail-fast 提示"先 cdk deploy McpInspectSpec2Stack 让 Stack 创建 PLACEHOLDER Secret"(Secret 必须存在才能 put-secret-value)
  - _Requirements: 5.2, 3.1_
  - _SHALL NOT covered: #9(不硬编码 ID)、#11(cdk 不管 RDS admin)、#12(secret 不在 cdk 模板硬编码)、#15(失败完整 trace)_

- [x] 3. 实现 stacks/spec_two_stack.py + 修改 app.py
  - 创建 `stacks/spec_two_stack.py`,严格按 design.md Component 2 落地
  - 实现细节:
    * 跨 Stack 引用 Spec 0 的 4 个 Output(`Fn.import_value` × 4):CognitoClientId / CognitoClientSecretArn / CognitoOAuthScope / CognitoTokenEndpoint
    * SSM 中转 × 2:`/mcp-inspect/spec0/cognito-user-pool-id`(Spec 1 Task 2 已建立)+ `/mcp-inspect/spec1/gateway-id`(Spec 2 Task 2 新建立)
    * 用 `cognito.UserPool.from_user_pool_id(...)` / `cognito.UserPoolClient.from_user_pool_client_id(...)` / `secretsmanager.Secret.from_secret_complete_arn(...)` 转 stable 类型
    * 用 `ec2.Vpc.from_lookup(self, ..., vpc_id=_TARGET_RDS_VPC_ID)` 引用 jupiter-dev VPC + 3 个 db 子网 by ID
    * 用 `ec2.SecurityGroup.from_security_group_id(self, ..., mutable=True)` 拿 jupiter-dev-slurm-db SG 引用(`mutable=True` 才能加 ingress)
    * 新建 `ec2.SecurityGroup` 给 Runtime 用,outbound 限定到 RDS SG 3306 + 0.0.0.0/0:443(NAT 出口)
    * `rds_sg.add_ingress_rule(ec2.Peer.security_group_id(runtime_sg.security_group_id), ec2.Port.tcp(3306), "Spec 2 Runtime → jupiter-dev-slurm-db")`(描述带 "Spec 2",destroy 反向扫描用)
    * 新建 Secret(name=`mcp-inspect/rds-mysql/devops-readonly`,默认 PLACEHOLDER 密码,RemovalPolicy.DESTROY)
    * 实例化 `McpInspectRuntime`:source_path=`mcp_servers/rds_mysql/`,runtime_name=`spec2_rds_mysql`,vpc + subnets + security_groups 3 个新参数,environment_variables 注入 `DEFAULT_CLUSTER_ENDPOINT` / `DB_SECRET_NAME` / `AWS_REGION`
    * `db_secret.grant_read(runtime.execution_role)` — 给 Runtime 角色加 IAM 限定到本 secret ARN(Property 10)
    * 用 Task 1 探针确认的方式(`agentcore.Gateway.from_gateway_id` + `add_mcp_server_target`,或 L1 fallback)在 Spec 1 Gateway 上挂第二个 target name=`rdsmysql`,endpoint=runtime.runtime_url,credential_provider 引用 Spec 1 OAuthProvider ARN(从 SSM 或 Spec 1 export 拿)
    * 5 个 CfnOutput(全带 export_name=`McpInspectSpec2Stack-{OutputKey}`)— RuntimeArn / RuntimeUrl / RuntimeSgId / DbSecretArn / Region
    * **严禁** `import aws_bedrock_agentcore_alpha`(SHALL NOT #10)
  - 修改 `app.py`:在现有 `SpecOneStack` 实例化下方加一行 `SpecTwoStack(app, "McpInspectSpec2Stack", env=env_us_east_1)`
  - 验证 1:alpha 隔离 grep 仍仅 2 个文件
  - 验证 2:`PATH=".venv/bin:$PATH" npx --yes cdk@2.1122.0 synth McpInspectSpec2Stack` exit 0,生成 `cdk.out/McpInspectSpec2Stack.template.json`
  - 验证 3:模板大小 < 50 KB
  - 验证 4:模板 Resources 段 `Fn::ImportValue` 计数 ≥ 4(对应 Spec 0 的 4 个 Output 引用)
  - 验证 5:模板 Outputs 段含 5 个 key(RuntimeArn / RuntimeUrl / RuntimeSgId / DbSecretArn / Region),每个有 Export.Name 字段
  - 验证 6:`cdk synth McpInspectSpec2Stack 2>&1 | grep -i 'secret'` 全部为 token 引用 / Fn::Join / IAM action 字段名,无 secret 明文(Property 6)
  - 验证 7:模板里**不**含 `AWS::Cognito::*` / `AWS::BedrockAgentCore::Gateway`(Spec 2 不重建 Gateway)/ `AWS::BedrockAgentCore::OAuth2CredentialProvider`(Spec 2 不重建 Provider)
  - 验证 8:模板里**含** `AWS::EC2::SecurityGroupIngress`(给 RDS SG 加白)、`AWS::SecretsManager::Secret`(DB 凭据壳)、`AWS::BedrockAgentCore::Runtime`(VPC 模式 Runtime)、`AWS::BedrockAgentCore::GatewayTarget`(挂到 Spec 1 Gateway)
  - _Requirements: 1.1-1.8, 2.x(容器对应 mcp_servers,不在本 task), 3.1-3.5, 4.1-4.6, 5.1-5.4, 7.1-7.3, 8.4_
  - _Correctness Property: Property 6, Property 7, Property 10_
  - _SHALL NOT covered: #1(不创新 Cognito)、#2(无新 Gateway)、#3(无新 OAuth Provider)、#5(RDS SG 加白限定 SG ref)、#9(不硬编码 ARN)、#10(alpha 不外漏)_

- [x] 4. 实现 mcp_servers/rds_mysql/ 容器(main.py + Dockerfile + requirements + .dockerignore)
  - 创建 `mcp_servers/rds_mysql/main.py`,严格按 design.md Component 3 落地
  - 关键实现要点:
    * 模块级常量 `_DEFAULT_THRESHOLD_SECONDS = 60` / `_CRITICAL_MULTIPLIER = 5` / `_QUERY_LIMIT = 100` / `_CONNECT_TIMEOUT_SECONDS = 5`
    * `FastMCP(host="0.0.0.0", port=8000, stateless_http=True)`(MCP 协议契约,SHALL NOT #17 / conventions 协议表)
    * `@mcp.custom_route("/ping", methods=["GET"])` 健康检查路由(沿用 Spec 0 hello_world)
    * `_fetch_db_credentials()` 每次 tool call 现拉,**不**缓存到全局(SHALL NOT #7);PLACEHOLDER 密码时 fail-fast 提示
    * 唯一 tool `rds_mysql_inspect_long_transactions(cluster_endpoint, threshold_seconds, database)`,固化 SQL 用 `%s` 参数化绑定 `threshold_seconds`(防 SQL injection,即使 LLM 传恶意值也无效)
    * 返回结构严格 conventions A8(status / findings / raw_data / recommendation 4 key)
    * `_classify_status` 阈值语义:`max_dur >= threshold * 5` → critical,`>= threshold` → warning,空 → ok
    * `mcp.run(transport="streamable-http")` 启动
  - 创建 `Dockerfile`:`FROM --platform=linux/arm64 ...`(SHALL NOT #7)+ EXPOSE 8000 + ENTRYPOINT 直接 exec python(SIGTERM 直达进程)
  - 创建 `requirements.txt`:`mcp~=1.27` + `pymysql>=1.1,<2.0` + `boto3>=1.34`(不引入 mysqlclient / mysql-connector-python,本 spec SHALL NOT #6)
  - 创建 `.dockerignore`:沿用 Spec 0 hello_world 同款模板,排除 `.env` / `.aws/` / `__pycache__/` / `.git/` / `.kiro/` 等
  - 验证 1:`getDiagnostics mcp_servers/rds_mysql/main.py` 无错误
  - 验证 2:`docker buildx build --platform linux/arm64 --load -t spec2-rds-mysql:smoke mcp_servers/rds_mysql/` exit 0(本机预构建,不推 ECR;cdk deploy 会自动 buildx + push)
  - 验证 3:本机起容器 + curl `/ping` 返回 200/ok(沿用 Spec 0 Task 2 smoke 模式)
  - 验证 4:本地用临时 smoke 脚本(用完即删)调 `tools/list` 返回仅 `rds_mysql_inspect_long_transactions`(严格 ==,容器内 Gateway 前缀还没挂上来)
  - _Requirements: 2.1-2.9_
  - _Correctness Property: Property 3, Property 4_
  - _SHALL NOT covered: #3(无通用 SQL 执行)、#4(arm64)、#6(用 pymysql)、#7(每次 tool call 拉 secret)、#9(无 admin 类 tool)、#13(.dockerignore 排凭据)_

- [x] 5. 实现 scripts/verify_spec2.py + scripts/destroy_spec2.sh + 写 README ## 8 章节
  - 创建 `scripts/verify_spec2.py`,严格按 design.md Component 4 落地
  - verify 关键要点:
    * 4 个模块级常量 `_SPEC0_STACK_NAME` / `_SPEC1_STACK_NAME` / `_SPEC2_STACK_NAME` / `_REGION` / `_TIMEOUT_HTTP=30`(VPC 模式 + RDS 慢一点,timeout 比 Spec 1 的 15s 放宽到 30s)
    * 2 个期望 tool 名常量(三下划线分隔符,SHALL NOT #21)
    * `_check_db_secret_not_placeholder()` 前置校验(Property 9 / 本 spec Requirement 6.2)
    * `_post_mcp` 沿用 Spec 1 的 SSE utf-8 修复(SHALL NOT #18)
    * `_assert_a8_structure` 严格断言 4 key + status 枚举值(Property 4)
    * tools/list 用 in 断言,**同时**包含 hello + rds 两个 tool(Property 2)
    * tools/call 摘要打印仅含 transaction count + max duration(本 spec SHALL NOT #10,不打 raw query SQL)
    * 顶层 try/except + traceback.print_exc + sys.exit(1)(SHALL NOT #15)
    * 严禁打印 access_token / DB password / Authorization header(SHALL NOT #12 / #17)
  - 创建 `scripts/destroy_spec2.sh`,严格按 design.md Component 5 落地:5 步残留扫描(Runtime / Spec 1 Gateway 上的 rdsmysql target / RDS SG ingress / Secret PendingDeletion / Spec 0/1 不受影响)
  - 在 `README.md` 的 Spec 1 章节后追加 **## 8 Spec 2** 章节,包含 6 个小节:
    * (a) 前置条件:Spec 0/1 已部署 + jupiter-dev-slurm-db 在线 + 王总有 RDS admin 凭据 + 工具链与 Spec 0/1 一致
    * (b) 部署步骤(顺序敏感,共 4 步):
      1. `bash scripts/bootstrap_spec2.sh`(写 SSM)
      2. `PATH=".venv/bin:$PATH" npx --yes cdk@2.1122.0 deploy McpInspectSpec2Stack --require-approval never`(创建 Runtime / Secret 壳 / SG / Target)
      3. `bash scripts/init_rds_user.sh`(王总跑,生成密码 + 灌 Secret + 输出 SQL)
      4. **王总用 RDS admin 凭据登 jupiter-dev-slurm-db 执行 `/tmp/spec2_init_rds_user.sql`**(创建 user)
    * (c) 验证步骤:`.venv/bin/python scripts/verify_spec2.py` 末尾打印 `✅ Spec 2 verification passed`
    * (d) 清理步骤(顺序敏感):**先 destroy Spec 2 后 Spec 1 后 Spec 0**;`bash scripts/destroy_spec2.sh` → `bash scripts/destroy_spec1.sh` → `bash scripts/destroy.sh`(可选)
    * (e) 故障排查:对应 design.md Error Handling 表 12 行,精确到命令 / 控制台路径,至少含 5 条(VPC 拓扑 / RDS SG / Secret PLACEHOLDER / DB user 权限 / Runtime 容器日志)
    * (f) DevOps Agent 端真实链路验证:Spec 1 已注册的 Gateway,部署 Spec 2 后**无需重新注册**,DevOps Agent 自动发现新 tool;触发会话调一次 `rdsmysql___rds_mysql_inspect_long_transactions` 验证人工链路
  - 验证 1:`bash -n scripts/destroy_spec2.sh` exit 0
  - 验证 2:`getDiagnostics scripts/verify_spec2.py` 与 `getDiagnostics README.md` 无错误
  - 验证 3:本地干跑 verify_spec2(Spec 2 stack 不存在场景)必须 exit 1 + 打印完整 botocore traceback(Stack `McpInspectSpec2Stack` does not exist)
  - _Requirements: 6.1-6.10, 9.1-9.5_
  - _Correctness Property: Property 4, Property 5, Property 9_
  - _SHALL NOT covered: #14(全 RemovalPolicy.DESTROY)、#15(完整 trace)、#16(destroy 不连带 Spec 0/1)、#17(不打 token)、本 spec #10(不打 raw SQL)_

- [x] 6. 端到端真实部署 + verify + destroy 收尾(Spec 2 收尾)
  - **前置 sanity check**:确认 Spec 0 + Spec 1 stack 在线(若离线,先 deploy Spec 0 → bootstrap_spec1 → deploy Spec 1)
  - **执行端到端**(完整 8 步):
    1. `time bash scripts/bootstrap_spec2.sh`,记录耗时(应 ≤ 5s)
    2. `time PATH=".venv/bin:$PATH" npx --yes cdk@2.1122.0 deploy McpInspectSpec2Stack --require-approval never`,记录耗时(VPC 模式应 ≤ 6 分钟)
    3. `time bash scripts/init_rds_user.sh`,记录耗时,确认 Secret 已更新 + SQL 文件 0600 权限
    4. **王总跑 RDS admin SQL**(本 task 由王总人工执行,把 SQL 输出复制粘贴);完成后 trace 归档"SQL 执行成功"或失败完整原文
    5. `time .venv/bin/python scripts/verify_spec2.py`,期望末尾 `✅ Spec 2 verification passed` + exit 0
    6. **回归 Spec 0**:`time .venv/bin/python scripts/verify.py` 仍 pass(Spec 2 没破坏)
    7. **回归 Spec 1**:`time .venv/bin/python scripts/verify_spec1.py` 仍 pass
    8. `time bash scripts/destroy_spec2.sh`,期望 exit 0 + 5 步残留扫描全过
    9. **destroy 后回归 Spec 0/1**:再次跑 verify.py / verify_spec1.py 仍 pass(Property 5)
  - **可选**:DevOps Agent 控制台触发会话调 `rdsmysql___rds_mysql_inspect_long_transactions`,验证真实业务链路(Requirement 6 之外的人工)
  - **trace 归档**:全部命令 stdout / 耗时 / 失败 traceback 完整追加到 `docs/development-trace.md` 的 Spec 2 实施期 trace section,Task 6 子节
  - **alpha 隔离最终回归 grep**:仅 2 个文件(framework/runtime_construct.py + framework/gateway_construct.py),Property 7
  - **Spec 2 复盘**:在 trace 末尾写"复盘结论",提炼可通用化的 SHALL NOT 候选项 / project-conventions 修订建议(VPC alpha L2 实测落地经验、单 secret 多库实战教训、Gateway 多 target 注册顺序等)
  - _Requirements: 4.5, 4.6, 5.5, 6.x, 9.1-9.5_
  - _Correctness Property: Property 1, Property 2, Property 3, Property 4, Property 5, Property 6, Property 7, Property 8, Property 9, Property 10, Property 11_
  - _SHALL NOT covered: #15(完整 trace)、#16(destroy 不连带)_

## Task Dependency Graph

```json
{
  "waves": [
    {
      "wave": 1,
      "tasks": ["1", "2"],
      "description": "Task 1(framework Construct VPC 扩展 + alpha 探针)与 Task 2(boot 脚本 + RDS user 一次性脚本)互不依赖,可并行"
    },
    {
      "wave": 2,
      "tasks": ["3"],
      "description": "Stack 拼装依赖 Construct(Task 1)与 SSM Parameter(Task 2 boot)就位"
    },
    {
      "wave": 3,
      "tasks": ["4"],
      "description": "MCP server 容器代码独立,但要在 cdk synth 拼装(Task 3)之后,因为 Task 3 验证 8 要求模板含 Runtime 资源(Task 4 容器 source_path 是 Runtime asset 来源)"
    },
    {
      "wave": 4,
      "tasks": ["5"],
      "description": "verify + destroy + README 依赖 Stack 结构(Task 3)+ 容器 tool 名(Task 4)"
    },
    {
      "wave": 5,
      "tasks": ["6"],
      "description": "端到端真实部署收尾,依赖前面全部就绪"
    }
  ]
}
```

文字版示意:

```
Task 1 (framework Construct VPC + alpha 探针)  ─┐
                                                 │
Task 2 (bootstrap_spec2 + init_rds_user)       ─┤
                                                 ▼
                                          Task 3 (Stack + app.py)
                                                 ▼
                                          Task 4 (mcp_servers/rds_mysql/)
                                                 ▼
                                          Task 5 (verify + destroy + README)
                                                 ▼
                                          Task 6 (端到端真实部署收尾)
```

Task 1 与 Task 2 互不依赖,可并行实施;其余任务严格按顺序。

## Notes

### Spec 2 整体完成判据

- [ ] 6 个 task 全部勾选完成
- [ ] `bash scripts/bootstrap_spec2.sh && cdk deploy McpInspectSpec2Stack --require-approval never` exit 0,deploy 耗时 ≤ 6 分钟
- [ ] `bash scripts/init_rds_user.sh` exit 0,Secret 已更新(password 长度 32),SQL 文件已生成(0600)
- [ ] 王总 RDS admin SQL 执行成功(`mcp_devops_ro` user 在 jupiter-dev-slurm-db 上 `SHOW GRANTS` 显示 SELECT + PROCESS)
- [ ] `python scripts/verify_spec2.py` 末尾打印 `✅ Spec 2 verification passed`,exit 0
- [ ] Spec 0 / Spec 1 verify 部署后 + destroy 后两次都 pass
- [ ] `cdk synth McpInspectSpec2Stack` 模板 < 50 KB,Resources 段 Fn::ImportValue ≥ 4,无 Cognito::* / 不重建 Gateway / 不重建 OAuthProvider
- [ ] alpha grep 仅 2 个文件
- [ ] `bash scripts/destroy_spec2.sh` exit 0,5 步残留扫描全过;Spec 0/1 资源未受影响;jupiter-dev-slurm-db SG 无 Spec 2 ingress 残留
- [ ] 新增代码 200-500 行,新增文件 ≤ 12 个

### 失败时的处置(meta-harness 二次规则)

- 同一问题修 2 次未解决 → 停下来,回头改 Spec(requirements 或 design),不做第 3 次尝试
- 失败必须留完整 trace(stderr / stack trace / CFN events / Runtime 容器日志 / pymysql 错误),归档到 `docs/development-trace.md`
- 永远不要只给 pass/fail

### 实施期高风险点(提前知会)

| 风险 | 触发任务 | 缓解 |
|------|---------|------|
| Spec 0 framework Construct 升级触发 Spec 0 Runtime UPDATE_REPLACE | Task 1 | cdk diff 验证 None 路径等价;若不等价改 Construct 兼容旧路径(传 None 不触 using_public_network) |
| `agentcore.Gateway.from_gateway_id` 不存在 / `add_mcp_server_target` 不能用在引用上 | Task 1 探针 | L1 fallback `CfnGatewayTarget` + 直接传 GatewayId 字符串;探针即停回头改 design |
| `ec2.Vpc.from_lookup` 在 cdk synth 时连 AWS 卡 | Task 3 | 备选 `ec2.Vpc.from_vpc_attributes` 直接传 ID,不查 AWS |
| jupiter-dev-slurm-db SG 上已有的某条规则与 Spec 2 加白冲突 | Task 3 deploy | 实测 SG 现有规则,加唯一描述 "Spec 2 Runtime → ..." 区分;destroy 反向按描述清 |
| Runtime VPC 模式 ENI 创建失败(IAM / SG 不足) | Task 6 deploy | 看 CFN events + Runtime 容器日志,按 Error Handling 表对照修 |
| pymysql 连接 RDS 401 / 1142 PROCESS denied | Task 6 verify | RDS admin 跑 `SHOW GRANTS FOR 'mcp_devops_ro'@'%';` 确认权限,补 PROCESS |
| Secret PendingDeletion 状态导致下次 deploy 名冲突 | Task 6 destroy 后再 deploy | `aws secretsmanager delete-secret --force-delete-without-recovery` 立即清,或等 7 天回收 |

### Out of Scope(再次强调)

- 第二个 RDS 实例 / Aurora 兼容 / 第二个巡检 tool → Spec 3
- Redis / DynamoDB / Postgres → Spec 4+
- RDS IAM Authentication 替代密码 → Spec 4+
- 巡检结果归档 / 通知 → Spec 5+
- 跨账号 / 跨 region → Spec 5+
- 框架抽象提炼 / cookiecutter → Spec 3+ 后端积累再做
