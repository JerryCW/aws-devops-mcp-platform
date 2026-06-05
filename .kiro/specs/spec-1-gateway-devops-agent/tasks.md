# Implementation Plan

## Overview

Spec 1 拆 6 个任务,严格按 meta-harness "3-7 task / 100-500 行 / 一个 PR" 原则。

**最终验证条件**(不变):
```bash
cdk deploy McpInspectSpec1Stack --require-approval never && python scripts/verify_spec1.py
```
末尾打印 `✅ Spec 1 verification passed`,退出码 0 = pass。

**任务设计原则**:
- 每个任务有明确"完成判据"(可观测事实)
- 每个任务标注覆盖的 Requirement / Correctness Property / SHALL NOT 编号,形成可追溯链路
- Task 1(实证 alpha 签名)与 Task 2(boot script 解决 UserPoolId)可并行;后续严格顺序
- 二次规则:同一问题修 2 次未解决,停下回去改 spec,不做第 3 次尝试

**Spec 1 与 Spec 0 的边界**:
- 只新增 4 个文件 + 改 1 个(`app.py`)
- 不动 `mcp_servers/` / `framework/runtime_construct.py` / `stacks/spec_zero_stack.py` / `scripts/verify.py` / `scripts/destroy.sh`
- alpha 隔离边界扩到 `framework/gateway_construct.py`,grep 命中文件由 1 个变 2 个

## Tasks

- [x] 1. 探针 alpha API 实测签名,落地 framework/gateway_construct.py 草稿(可与 Task 2 并行)
  - 探针脚本(临时,验证完即删):用 `.venv/bin/python -c` 或一次性 `scripts/_probe_alpha.py` 打印以下类的真实 `inspect.signature` 与关键属性:
    * `agentcore.Gateway` 的 `__init__` keyword args、实例的 `gateway_id` / `gateway_url` / `gateway_arn` 等可读属性(对应 design.md "风险与未决项" 第 1/3 行)
    * `agentcore.OAuth2CredentialProvider.using_cognito` 工厂的完整 keyword args
    * `agentcore.OAuth2CredentialProvider` 实例可读属性(`credential_provider_arn` 等)
    * `agentcore.GatewayAuthorizer.using_cognito` 工厂签名
    * `agentcore.GatewayProtocol.mcp` 工厂签名
    * `agentcore.Gateway.add_mcp_server_target` 实例方法签名,以及返回的 `GatewayTarget` 实例可读属性
    * `agentcore.GatewayCredentialProvider.from_oauth_identity` 工厂签名
  - 把每个签名 + 属性名的实测结果记到 `docs/development-trace.md` 的 Spec 1 实施期 trace section,作为后续 task 的事实依据
  - 创建 `framework/gateway_construct.py` 草稿:严格按 design.md Component 1 代码骨架落地;**alpha 包的 import 仅在本文件出现**(满足 SHALL NOT #2/#10)
  - 把 design 中的 4 个属性名占位(`gateway_id` / `gateway_url` / `credential_provider_arn` / `gateway.add_mcp_server_target` 返回值上的 `target_arn`)替换为实测属性名;若实测属性名与 design 推测不一致,在 docstring 注释里标注"实测发现 X 而非 Y"以备后续 spec 复盘
  - **不**实例化 Construct 跑 cdk synth(那是 Task 4 的事),本任务的交付仅是文件本身能被 import 不报错
  - 验证 1:`.venv/bin/python -c "from framework.gateway_construct import McpInspectGateway"` exit 0,无 ImportError
  - 验证 2:`grep -rn "aws_bedrock_agentcore_alpha" --include="*.py" --exclude-dir=.venv --exclude-dir=cdk.out .` 仅匹配 `framework/runtime_construct.py` 与 `framework/gateway_construct.py` 两个文件(Property 7 / SHALL NOT #2 / Requirement 8.4)
  - 验证 3:`getDiagnostics framework/gateway_construct.py` 无错误
  - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5_
  - _Correctness Property: Property 7_
  - _SHALL NOT covered: #2(alpha 隔离)、#7(强制 Cognito JWT,Construct 内固定 using_cognito)、#10(alpha 类型不外漏)、#11(优先 L2,无 Cfn* 用法)、#14(全资源 RemovalPolicy.DESTROY)_

- [x] 2. 实现 boot script 把 Spec 0 UserPoolId 写到 SSM Parameter(可与 Task 1 并行)
  - 创建 `scripts/bootstrap_spec1.sh`:`#!/usr/bin/env bash`,`set -euo pipefail`,cd 到仓库根
  - 脚本职责:(1) 读 Spec 0 Stack outputs 的 `CognitoClientId`(已 export),(2) 用 `aws cognito-idp list-user-pool-clients --region us-east-1 --user-pool-id <每个 pool>` 反查持有该 client 的 user pool;或者直接用 `aws cognito-idp list-user-pools --region us-east-1 --max-results 60` 找到 Name 以 `McpInspectSpec0Stack/HelloWorldRuntime/UserPool` 开头的 pool,提取 `Id`,(3) `aws ssm put-parameter --name /mcp-inspect/spec0/cognito-user-pool-id --value <pool-id> --type String --overwrite --region us-east-1`
  - 脚本对幂等性的承诺:多次执行结果一致(始终 `--overwrite`),只要 Spec 0 还在,就能被反复调
  - 失败必须打印完整 AWS CLI stderr(SHALL NOT #15);找不到 user pool 时退出码非零并提示"先 deploy Spec 0"
  - 用 `chmod +x scripts/bootstrap_spec1.sh` 赋执行权限
  - 验证 1:在 Spec 0 已部署的前提下执行 `bash scripts/bootstrap_spec1.sh`,exit 0,且 `aws ssm get-parameter --name /mcp-inspect/spec0/cognito-user-pool-id --region us-east-1 --query Parameter.Value --output text` 返回类似 `us-east-1_XXXXXXXXX` 的 user pool id
  - 验证 2:在 Spec 0 未部署 / 错的 region 跑,脚本必须非零退出 + 打印明确诊断信息(对照 design.md Error Handling)
  - _Requirements: 4.1, 4.2_
  - _SHALL NOT covered: #9(不硬编码 user pool id 在 Spec 1 代码里;走 SSM Parameter 中转)_

- [x] 3. 实现 stacks/spec_one_stack.py + 修改 app.py
  - 创建 `stacks/spec_one_stack.py`:`class SpecOneStack(cdk.Stack)`,严格按 design.md Component 2 落地
  - 实现细节:
    * 用 `Fn.import_value` 跨 stack 引用 Spec 0 已 export 的 5 个 Output(`RuntimeArn` / `RuntimeUrl` / `CognitoTokenEndpoint` / `CognitoClientId` / `CognitoClientSecretArn` / `CognitoOAuthScope`);**第一步实施前先 cdk synth Spec 0 看 cdk.out 里 Output 的真实 Export.Name 字段**,如果 CDK 没自动 export(需要显式 `export_name=`),改用 `Stack.from_stack_attributes` 路径或 SSM Parameter 中转(把这个发现归档到 trace,作为 Task 1/2 之后的关键 fact)
    * 用 `cognito.UserPool.from_user_pool_id(...)` + `ssm.StringParameter.value_for_string_parameter(self, "/mcp-inspect/spec0/cognito-user-pool-id")` 拿 user pool 引用
    * 用 `cognito.UserPoolClient.from_user_pool_client_id(...)` + `Fn.import_value(...)` 拿 client 引用
    * 用 `secretsmanager.Secret.from_secret_complete_arn(...)` + `Fn.import_value(...)` 拿 ISecret 引用
    * 实例化 `McpInspectGateway`,传上面 6 个 stable 类型参数
    * 添加 4 个 `CfnOutput`:`GatewayId` / `GatewayUrl` / `GatewayCredentialProviderArn` / `Region`
    * 严禁 `import aws_bedrock_agentcore_alpha`(SHALL NOT #10 / Requirement 8.5)
  - 修改 `app.py`:在现有 `SpecZeroStack` 实例化下方加一行 `SpecOneStack(app, "McpInspectSpec1Stack", env=env_us_east_1)`;把 region 提取为局部变量 `env_us_east_1 = cdk.Environment(region="us-east-1")` 给两个 Stack 共用
  - 验证 1:`grep -rn "aws_bedrock_agentcore_alpha" --include="*.py" --exclude-dir=.venv --exclude-dir=cdk.out .` 仍仅命中 `framework/runtime_construct.py` 与 `framework/gateway_construct.py` 两个文件(Property 7)
  - 验证 2:`PATH=".venv/bin:$PATH" npx --yes cdk@2.1122.0 synth McpInspectSpec1Stack` exit 0,生成 `cdk.out/McpInspectSpec1Stack.template.json`
  - 验证 3:模板大小 < 50 KB(沿用 Spec 0 体量约束)
  - 验证 4:模板中 Resources 段实际包含 ≥ 4 个 `Fn::ImportValue` 引用(Requirement 4.1):`cat cdk.out/McpInspectSpec1Stack.template.json | python -c "import json,sys; t=json.load(sys.stdin); s=json.dumps(t.get('Resources',{})); print('ImportValue count:', s.count('Fn::ImportValue'))"` ≥ 4
  - 验证 5:模板 Outputs 段含 4 个 key(`GatewayId` / `GatewayUrl` / `GatewayCredentialProviderArn` / `Region`)
  - 验证 6:`cdk synth McpInspectSpec1Stack | grep -i 'secret'` 仅匹配 ARN/Ref/Fn::GetAtt 引用,无 secret 明文(Property 6)
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 2.1, 2.2, 2.3, 2.6, 3.1-3.6, 4.1, 4.2, 4.3_
  - _Correctness Property: Property 6, Property 7_
  - _SHALL NOT covered: #1(不创建新 Cognito 资源)、#9(凭据零泄露)、#10(alpha 类型不外漏)、#12(无硬编码 ARN)_

- [x] 4. 实现 scripts/verify_spec1.py 端到端验证脚本
  - 创建 `scripts/verify_spec1.py`,严格按 design.md Component 4 落地
  - 关键实现要求:
    * 模块级常量 `_SPEC0_STACK_NAME = "McpInspectSpec0Stack"` / `_SPEC1_STACK_NAME = "McpInspectSpec1Stack"` / `_REGION = "us-east-1"` / `_TIMEOUT_HTTP = 15`
    * `_post_mcp(...)` 必须复用 Spec 0 verify.py 的 SSE utf-8 修复:`r.iter_lines(decode_unicode=False)` + 手动 `decode("utf-8")`,以及 JSON 路径用 `json.loads(r.content.decode("utf-8"))`,杜绝中文等非 ASCII 字段乱码(对应 docs/development-trace.md "Spec 0 跑通后的本机集成"一节)
    * `main()` 顺序:read outputs(两个 stack)→ get_secret_value(Spec 0)→ Cognito client_credentials → Gateway initialize → Gateway tools/list → Gateway tools/call
    * 断言:`tools/list` 返回**包含**(不是严格 ==,因为 Gateway 可能有 internal tool)`spec0helloworld__hello_world`;`tools/call name=Spec1` 返回 `text == "Hello, Spec1!"`(Property 4)
    * 全部通过后打印 `✅ Spec 1 verification passed` + `sys.exit(0)`
    * 顶层 `try/except Exception` + `traceback.print_exc()` + `sys.exit(1)`,严禁只打印 pass/fail(SHALL NOT #15)
    * print 仅含 token 长度、status code、tool 名,严禁打印完整 token / secret / Authorization 值(SHALL NOT #17 / Requirement 5.11)
  - 验证 1:本地干跑(stack 不存在场景,Spec 1 还没 deploy)`python scripts/verify_spec1.py` 必须 exit 1 + 打印完整 botocore traceback(stack `McpInspectSpec1Stack` does not exist),不允许只输出 "Failed"(meta-harness 完整 trace 验证)
  - 验证 2:`getDiagnostics scripts/verify_spec1.py` 无错误
  - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 5.8, 5.9, 5.10, 5.11_
  - _Correctness Property: Property 1, Property 3, Property 4, Property 6_
  - _SHALL NOT covered: #15(完整 traceback)、#17(不打印 token/secret 明文)_

- [x] 5. 实现 scripts/destroy_spec1.sh + 写 README Spec 1 章节
  - 创建 `scripts/destroy_spec1.sh`,严格按 design.md Component 5 落地:`set -euo pipefail`,cd 仓库根,`PATH` 加入 `.venv/bin`,`npx --yes cdk@2.1122.0 destroy --force McpInspectSpec1Stack`,然后用 AWS CLI 检查无 Gateway / OAuth2CredentialProvider 残留;`chmod +x`
  - **AWS CLI 子命令名实证**:在写脚本前先跑 `aws bedrock-agentcore-control help 2>&1 | grep -E "list-(gateway|provider|oauth)"`,以实测为准修脚本里的子命令名(对应 design.md 风险表第 6 行)
  - 在 `README.md` 的 Spec 0 部分之后追加 **Spec 1 章节**,包含 5 个 section:
    * (a) 前置条件:Spec 0 已部署,SSM Parameter 已写(指向 bootstrap_spec1.sh)
    * (b) 部署步骤:
      1. `bash scripts/bootstrap_spec1.sh`(写 SSM)
      2. `PATH=".venv/bin:$PATH" npx --yes cdk@2.1122.0 deploy McpInspectSpec1Stack --require-approval never`
    * (c) 验证步骤:`.venv/bin/python scripts/verify_spec1.py` 末尾 `✅ Spec 1 verification passed`
    * (d) 清理步骤:**先 destroy Spec 1 后 destroy Spec 0** 的顺序明确写出来(对应 SHALL NOT #16 / Property 5);`bash scripts/destroy_spec1.sh` → `bash scripts/destroy.sh`
    * (e) 故障排查 checklist:对应 design.md Error Handling 表 8 行,每条给信号 / 直觉 / 排查命令,至少包含 3 条具体可执行的入口(命令 / 控制台路径)以满足 Requirement 6.5
  - 在同一章节追加 **Agent Space 注册人工 checklist**(Requirement 6.1-6.4):描述如何在 AWS Agent Space 控制台用 `GatewayUrl` + Spec 0 `CognitoClientId` + token endpoint + scope `mcp/invoke` 注册;明确这是**可选人工验证**,不阻塞自动化 verify
  - 验证:`bash -n scripts/destroy_spec1.sh`(语法检查)exit 0;`getDiagnostics README.md` 无错误
  - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 9.1, 9.2, 9.3_
  - _Correctness Property: Property 5_
  - _SHALL NOT covered: #14(全 RemovalPolicy.DESTROY)、#15(失败要 trace)、#16(destroy 不连带 Spec 0)_

- [x] 6. 端到端真实部署 + verify + destroy 收尾(Spec 1 收尾)
  - **前置 sanity check**:确认 Spec 0 当前状态可用 — `aws cloudformation describe-stacks --stack-name McpInspectSpec0Stack --region us-east-1 --query 'Stacks[0].StackStatus' --output text` 返回 `CREATE_COMPLETE` 或 `UPDATE_COMPLETE`;若 Spec 0 已 destroy,先 `cdk deploy McpInspectSpec0Stack` 重 deploy(沿用 Spec 0 的 README)
  - **执行端到端**:
    1. `bash scripts/bootstrap_spec1.sh`,记录耗时与 SSM Parameter 写入结果
    2. `time PATH=".venv/bin:$PATH" npx --yes cdk@2.1122.0 deploy McpInspectSpec1Stack --require-approval never`,记录耗时(应 ≤ 5 分钟,与 Spec 0 + Spec 1 总和 ≤ 15 分钟一致)
    3. `time .venv/bin/python scripts/verify_spec1.py`,期望末尾打印 `✅ Spec 1 verification passed`,exit 0
    4. **回归 Spec 0**:`time .venv/bin/python scripts/verify.py`,期望仍打印 `✅ Spec 0 verification passed`,exit 0(Requirement 4.6)
    5. `bash scripts/destroy_spec1.sh`,记录耗时,期望 exit 0 + 残留扫描通过
    6. **destroy 后回归 Spec 0**:再次跑 `python scripts/verify.py`,期望仍 pass(证明 destroy Spec 1 没破坏 Spec 0)
  - **可选**:在 Agent Space 控制台手动注册 Gateway 跑一次 DevOps Agent 真实链路(Requirement 6 人工 checklist);成功 / 失败都归档到 trace
  - **trace 归档**:把 boot 输出 / deploy 时长 / verify_spec1 输出全文 / 回归 verify 输出 / destroy 输出 / 残留扫描结果完整追加到 `docs/development-trace.md` 的 Spec 1 实施期 trace section;若任何一步失败,完整 stack trace + 修复尝试 #1/#2 也归档(SHALL NOT #15 / 二次规则)
  - **alpha 隔离最终回归 grep**:`grep -rn "aws_bedrock_agentcore_alpha" --include="*.py" --exclude-dir=.venv --exclude-dir=cdk.out .` 仅 2 个文件 = framework/runtime_construct.py + framework/gateway_construct.py(Property 7)
  - **Spec 1 复盘**:在 trace 末尾写一段"复盘结论":提炼可通用化的 SHALL NOT 候选项(如 Cognito UserPoolId 应在 Spec 0 时就 export 这种回看,但已发布 spec 不可重写,作为 project-conventions.md 的修订建议)
  - _Requirements: 4.5, 4.6, 5.9, 5.10, 9.1, 9.2, 9.3_
  - _Correctness Property: Property 1, Property 2, Property 3, Property 4, Property 5, Property 6, Property 7, Property 8_
  - _SHALL NOT covered: #15(完整 trace)、#16(destroy 不连带 Spec 0)_

## Task Dependency Graph

任务依赖与并行波次:

```json
{
  "waves": [
    {
      "wave": 1,
      "tasks": ["1", "2"],
      "description": "Task 1(alpha API 实证 + Construct 草稿)与 Task 2(SSM boot script)互不依赖,可并行"
    },
    {
      "wave": 2,
      "tasks": ["3"],
      "description": "Stack 拼装依赖 Construct(Task 1)与 SSM Parameter(Task 2)就位"
    },
    {
      "wave": 3,
      "tasks": ["4"],
      "description": "verify_spec1.py 依赖 Stack Output 结构已定(Task 3)"
    },
    {
      "wave": 4,
      "tasks": ["5"],
      "description": "destroy + README 依赖 verify 思路与 Stack 结构稳定"
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
Task 1 (alpha API + framework/gateway_construct.py)  ─┐
                                                       │
Task 2 (bootstrap_spec1.sh + SSM)                    ─┤
                                                       │
                                                       ▼
                                              Task 3 (Stack + app.py)
                                                       │
                                                       ▼
                                              Task 4 (verify_spec1.py)
                                                       │
                                                       ▼
                                              Task 5 (destroy + README)
                                                       │
                                                       ▼
                                              Task 6 (端到端真实部署收尾)
```

Task 1 与 Task 2 互不依赖,可并行实施;其余任务严格按顺序。

## Notes

### Spec 1 整体完成判据

- [x] 6 个 task 全部勾选完成
- [x] `cdk deploy McpInspectSpec1Stack --require-approval never` exit 0,耗时 ≤ 5 分钟(Spec 0 + 1 总和 ≤ 15 分钟)
- [x] `bash scripts/bootstrap_spec1.sh` exit 0,SSM Parameter `/mcp-inspect/spec0/cognito-user-pool-id` 写入正确 user pool id
- [x] `python scripts/verify_spec1.py` 末尾打印 `✅ Spec 1 verification passed`,exit 0
- [x] `python scripts/verify.py`(Spec 0 回归)仍打印 `✅ Spec 0 verification passed`,exit 0
- [x] `cdk synth McpInspectSpec1Stack` 模板 < 50 KB,Resources 段含 ≥ 4 个 `Fn::ImportValue`,无 Cognito 资源类型
- [x] `grep -rn "aws_bedrock_agentcore_alpha" --include="*.py"` 仅命中 framework/runtime_construct.py + framework/gateway_construct.py
- [x] `bash scripts/destroy_spec1.sh` exit 0,AWS 账户中无 spec1_gateway / 其 Provider 残留;Spec 0 资源未受影响
- [x] 新增代码 100-500 行,新增文件 ≤ 12 个(Constraints 一致)

### 失败时的处置(meta-harness 二次规则)

- 同一问题修 2 次未解决 → 停下来,回头改 Spec(requirements 或 design),不做第 3 次尝试
- 失败必须留完整 trace(stderr / stack trace / CFN events / Gateway log),归档到 `docs/development-trace.md`
- 永远不要只给 pass/fail

### 实施期高风险点(提前知会)

| 风险 | 触发任务 | 缓解 |
|------|---------|------|
| `OAuth2CredentialProvider.using_cognito` / `Gateway.add_mcp_server_target` 等 alpha API 实测签名与 design 推测有差异 | Task 1 | Task 1 第一步即"探针实测",以 jsii 实际签名修代码;Spec 0 已有 using_cognito 位置参数偏差的先例 |
| `Fn.import_value` 自动 export 名格式不可靠 | Task 3 | Task 3 第一步先 cdk synth Spec 0 看真实 Export.Name;实在拿不到稳定 export 走 SSM Parameter 中转 |
| Gateway L2 不支持 logging_configs,application logs 没自动 delivery | Task 3 | tasks 阶段补 L1 escape hatch,在 Construct 内增加 `AWS::Logs::DeliverySource` / `DeliveryDestination` |
| AWS CLI `bedrock-agentcore-control` 子命令名 | Task 5 | Task 5 写 destroy.sh 前先 `aws bedrock-agentcore-control help` 实测,以实际命令名修脚本 |
| Spec 0 Cognito hosted domain 删除异步,destroy 后重 deploy 撞名(Spec 0 也有同型风险) | Task 6 | gateway_name 加 sha hash 后缀;destroy 后重 deploy 失败时等几分钟再试 |
| Agent Space UI 注册流程(Requirement 6 checklist) | Task 5 README | 截图不进 git;只放 step-by-step 文字;明确"以 AWS 控制台实际界面为准" |

### Out of Scope(再次强调)

下列**不在 Spec 1 任务范围**:

- Runtime VPC 模式 → Spec 2
- 第二个 MCP target(指向 RDS MySQL Runtime 等)→ Spec 2
- RDS MySQL 连接 / 数据库凭据 → Spec 2
- 第一个真实巡检 tool(`inspect_long_transactions` 等)→ Spec 2
- 框架抽象提炼 / cookiecutter → Spec 3+
- 多数据源 Runtime / Cognito 联邦 / 跨账户多 region → Spec 4+
- DevOps Agent 端 cdk 自动注册(Agent Space 暂无公开 IaC API)→ 后续按需
- 端到端真实链路验证纳入自动化 verify(当前停留在 README 人工 checklist)→ 等 Agent Space 开放程序化测试入口
