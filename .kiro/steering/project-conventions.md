# 项目约定（Project Conventions）

> 跨 spec 通用的项目级技术栈与架构决策。每条都是已经拍板过的结果，不在 spec 内重新论证。  
> 这份文件回答："这个项目是什么、用什么、怎么组织"。Spec 只关心"这一轮做什么"。  
> 与 `shall-not.md` 关系：本文件说"做什么"，shall-not 说"不做什么"，互补。

## 项目定位

**项目名**：AWS DevOps Agent MCP 框架

**目标**：为 AWS DevOps Agent 提供数据访问能力扩展的统一框架。DevOps Agent 因权限受限不能直查 RDS / Redis / DynamoDB 等数据源，本框架通过 MCP 协议把数据访问能力以 tool 形式注入 DevOps Agent。

**形态**：基础设施 + 框架代码 + 多个数据源 MCP server 的 monorepo。新增一类数据源的边际成本要尽可能低（理想 ≈ 写几个 tool 函数 + 一份配置）。

**第一阶段范围**：RDS MySQL（含 Aurora MySQL）。后续阶段按需扩展 PG / Redis / DynamoDB 等。

## 技术栈

### 核心栈

| 类别 | 选择 | 锁版本 |
|------|------|--------|
| IaC | AWS CDK | `aws-cdk-lib==2.254.0`（精确锁）|
| AgentCore Construct | CDK alpha 包 | `aws-cdk.aws-bedrock-agentcore-alpha==2.254.0a0`（精确锁，与主包配对）|
| 项目语言 | Python 3.13 | CDK app + MCP server 容器内一致 |
| MCP SDK | FastMCP（Python `mcp` 包）| `mcp ~= 1.27` |
| 容器平台 | linux/arm64 | AgentCore Runtime 硬要求 |
| MCP 传输 | Streamable HTTP + `stateless_http=True` | AgentCore Runtime 硬要求 |
| 部署 region | us-east-1（固定）| 多 region 留待 Spec 4+ |

### AgentCore 协议 / 容器 port / mount path 对照表

来源：AWS 官方 [Runtime service contract](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-service-contract.html)。**MCP 协议下 Runtime 服务契约硬编码**容器 port 与 mount path，不是 SDK 默认值，写错会导致 invocation read timeout 而非 4xx（Spec 0 Task 6 实测踩过；详见 `shall-not.md` #17）。

| 协议 | 容器 port | Mount path | 是否本框架使用 |
|------|-----------|------------|---------------|
| MCP  | **8000**  | `/mcp`     | ✅ Spec 0+ 定型 |
| HTTP | 8080      | `/invocations`(SSE) / `/ws`(WebSocket) | ❌ 不用 |
| A2A  | 9000      | `/`        | ❌ 暂不用 |
| AG-UI| 8080      | `/invocations` / `/ws` | ❌ 暂不用 |

本框架所有 MCP server 容器一律 `FastMCP(host="0.0.0.0", port=8000, stateless_http=True)` + `mcp.run(transport="streamable-http")`；Dockerfile 一律 `EXPOSE 8000`。


### 依赖管理规则

- `requirements.txt` 必须精确锁主包与 alpha 包，不用 `~=` / `^`
- 升级 cdk-lib 必须同步升级 alpha 包到对应 `aXX` 版本
- 数据源 SDK（pymysql / redis / boto3 子模块等）按数据源 spec 各自管理，不进根目录 requirements.txt

## 架构决策

### A1：AgentCore 架构定型

```
DevOps Agent (Agent Space)
        │ Streamable HTTP + JWT
        ▼
  AgentCore Gateway（聚合层，单实例）
        │ MCP target × N + OAuth Credential Provider
        ▼
  AgentCore Runtime × N（每类数据源一个，public 或 VPC 模式）
        │
        ▼
  目标数据源（RDS / Redis / DynamoDB ...）
```

- **DevOps Agent** 只看到一个 Gateway 端点
- **Gateway** 聚合所有 MCP Runtime target，统一认证、tool 发现、命名空间
- **Runtime** 按数据源类型一对一部署（多实例参数化通过 tool input 传 ARN）

### A2：单 Gateway 策略
默认用单一 Gateway 聚合所有 MCP target。拆分需触发条件：工具数 > 30 / 团队边界 / 权限差异显著。框架的 Gateway Construct 必须支持创建多个实例（不写死全局唯一），但 stack 内默认只创建一个。

### A3：Tool 设计哲学：100% 语义化
所有 tool 都是封装好的"巡检罐头"，例如 `inspect_long_transactions(cluster_arn, threshold_seconds)`。SQL/命令固化在 MCP server 代码里。**不暴露通用 SQL 执行 tool**（详见 `shall-not.md` 第 3 条）。

### A4：alpha 类型隔离边界
`aws_bedrock_agentcore_alpha` 的 import 只能出现在 `framework/` 目录下的 Construct 文件中。Stack / scripts / 测试代码均不感知 alpha。这是框架质量的硬指标（详见 `shall-not.md` 第 2 条）。

### A5：凭据管理统一通过 Secrets Manager
Cognito client secret、数据库密码、API key 等所有 secret 都存 Secrets Manager，对外只暴露 ARN。运行时按 ARN 拉取，不读环境变量。Secret 命名约定：

- **Cognito client secret**：CDK 自动命名（每个 spec 自己的 Construct 内）
- **数据源巡检凭据（Spec 2+，单 secret 多库）**：`mcp-inspect/{datasource}/devops-readonly`
  - 例：`mcp-inspect/rds-mysql/devops-readonly`、`mcp-inspect/redis/devops-readonly`
  - DB 用户名固定 `mcp_devops_ro`，密码强随机（≥32 字符），所有同类型实例共用一套凭据
  - **设计取舍**：100 个 RDS 实例不应该 100 个不同密码——巡检场景的运维成本远高于"一库一密"带来的爆炸半径收益；用户级最小权限（read-only）才是正确的安全控制点。详细论证见 Spec 2 design.md Decision X
  - **加密**：自动绑定 KMS（AWS 托管或自建 CMK 都可，A5 不强制）
  - **轮换**：手工 + Secrets Manager 自动轮换均可，密码变化不影响 tool 代码（每次拉取最新值）
  - **DB user 权限**（MySQL 8.0 实测）：`GRANT SELECT, PROCESS ON *.*` + `GRANT SELECT ON information_schema.*` + `GRANT SELECT ON performance_schema.*`
    - `PROCESS` 是 `information_schema.innodb_trx` / `processlist` 的硬要求（MySQL server-level 权限，无法 schema 级限制）
- **数据源 IAM 路径限权**：Runtime 执行角色 `secretsmanager:GetSecretValue` 资源段限定到 `arn:aws:secretsmanager:{region}:{account}:secret:mcp-inspect/{datasource}/*`，不允许通配整个前缀

### A6：Runtime 网络模式默认 Public Network
Spec 0 / Spec 1 用 Public Network。接真实数据源（Spec 2 起）切 VPC 模式。Construct API 通过可选参数 `vpc=` 增量扩展，不破坏已有调用方式。

### A7：Tool 命名规范
```
{datasource}_{action}_{object}
```
- `rds_mysql_inspect_long_transactions`
- `rds_mysql_inspect_lock_waits`
- `redis_check_memory_fragmentation`

**Gateway 暴露的 tool 全名**：AgentCore Gateway 自动给上游 tool 加 `{target_name}___{tool_name}` 前缀（**三下划线**，Spec 1 Task 6 实测；详见 `shall-not.md` #21）。例如 target=`spec0helloworld` + tool=`hello_world` → DevOps Agent 看到 `spec0helloworld___hello_world`（28 + 1 = 29 字符）。

长度上限：**target 名 + 3 个下划线 + tool 名 ≤ 64 字符**。命名时给 `{target_name}___` 前缀预留 ≤ 16 字符（含三下划线 = target 名本身 ≤ 13 字符比较安全；下表预留示例用 15 字符 target）。


### A8：Tool 返回格式统一
所有 tool 返回结构化 dict，字段统一：

```python
{
  "status": "ok" | "warning" | "critical",
  "findings": [
    {"severity": "...", "metric": "...", "value": "...", "threshold": "..."}
  ],
  "raw_data": {...},        # 原始数据，供 LLM 进一步推理
  "recommendation": "..."   # 可选，机器可读 + 人类可读的建议
}
```

DevOps Agent 容易聚合多个 tool 的结果做整体推断。

### A9：所有 CfnOutput 必须传 `export_name`
凡是后续 spec 可能跨 stack 引用的资源 ID（Cognito UserPoolId / Runtime ARN / Gateway URL 等），Spec N 的 `cdk.CfnOutput(...)` 调用**必须**显式传 `export_name=f"{StackName}-{OutputKey}"`。

```python
cdk.CfnOutput(
    self, "RuntimeArn",
    value=runtime.runtime_arn,
    description="...",
    export_name=f"McpInspectSpec0Stack-RuntimeArn",  # ★ 必传
)
```

理由：CDK 不会自动给手写 CfnOutput 生成 Export 字段（除非 cross-stack reference 触发隐式 export），等到下游 spec 用 `Fn.import_value("...")` 字面引用时会 deploy 失败 "No export named ... found"。补救方式是回头给上游 spec 补 `export_name=` 然后重 deploy 上游 stack 让元数据落地（Spec 1 Task 3 实测踩过）。

来源：Spec 1 Task 3 实施期发现（`docs/development-trace.md` Spec 1 Task 3 选型对比 / 路径 C）

### A10：AgentCore 资源命名字符集差异（Runtime vs Gateway）
| 资源 | 允许字符 | 长度 | 示例 |
|------|---------|------|------|
| AgentCore Runtime name | `[a-zA-Z][a-zA-Z0-9_]{0,47}` —— **允许下划线** | ≤48 | `spec0_hello_world` |
| AgentCore Gateway name | alphanumeric + hyphen，hyphen 只能出现在字符之间 —— **不允许下划线** | 实测约束 | `spec1-gateway` |
| MCP target name | alphanumeric（实测，无 hyphen 也无 underscore 限制，谨慎起见） | ≤15 | `spec0helloworld` |
| Cognito UserPool domain prefix | `[a-z0-9-]`，全局唯一（同 region） | ≤63 | `spec0-hello-world-bd467179` |

来源：Spec 1 Task 3 实测 alpha 服务端约束（`docs/development-trace.md` Spec 1 Task 3 实施期临时偏差）


## 项目目录组织

```
aws-devops-mcp-platform/
├─ .kiro/                          ← Kiro 规范目录
│   ├─ specs/spec-{N}-{name}/      ← 每个 spec 自己的目录
│   └─ steering/                   ← workspace 级 steering
│       ├─ shall-not.md
│       └─ project-conventions.md
├─ docs/
│   └─ development-trace.md        ← 跨 spec trace 归档
├─ app.py                          ← CDK app 入口
├─ cdk.json
├─ requirements.txt                ← 根 Python 依赖（CDK + 工具）
├─ requirements-dev.txt
├─ stacks/
│   ├─ spec_zero_stack.py          ← 每个 spec 独立 Stack 类
│   ├─ spec_one_stack.py
│   └─ ...
├─ framework/                      ← alpha 隔离层 + 通用 Construct
│   ├─ runtime_construct.py
│   ├─ gateway_construct.py        ← Spec 1 引入
│   └─ ...
├─ mcp_servers/                    ← MCP server 容器代码
│   ├─ hello_world/                ← Spec 0
│   ├─ rds_mysql/                  ← Spec 2 引入
│   └─ ...
└─ scripts/
    ├─ verify.py                   ← 端到端验证（每个 spec 一份或共享）
    └─ destroy.sh
```

## 命名约定

| 对象 | 约定 | 示例 |
|------|------|------|
| Spec 目录 | `spec-{N}-{kebab-name}`（N 从 0 递增）| `spec-0-runtime-skeleton` |
| CDK Stack | `McpInspectSpec{N}Stack`（PascalCase）| `McpInspectSpec0Stack` |
| Runtime name | `spec{N}_{descriptor}`（满足 `[a-zA-Z][a-zA-Z0-9_]{0,47}`）| `spec0_hello_world` |
| Cognito Resource Server | `mcp` | 单一 identifier |
| Cognito Scope | `invoke` | 单一 scope |
| MCP server 目录 | `mcp_servers/{kebab-name}/` | `mcp_servers/rds_mysql/` |
| Construct 类 | `McpInspect{Pascal}` | `McpInspectRuntime` / `McpInspectGateway` |
| Tool 函数名 | `{datasource}_{action}_{object}` | `rds_mysql_inspect_long_transactions` |
| Secret 命名前缀 | `mcp-inspect/{category}/{detail}` | `mcp-inspect/cognito/spec0` |

## 验证 / 部署 / 清理协议

每个 spec 必须提供完整三步：

1. **部署**：`pip install -r requirements.txt && cdk deploy --require-approval never`
2. **验证**：`python scripts/verify.py`，末尾必须打印 `✅ Spec {N} verification passed`，退出码 0
3. **清理**：`bash scripts/destroy.sh`，destroy 后用 AWS CLI 检查无残留

部署 + 验证总耗时上限 10 分钟（Spec 0），后续 spec 增加资源时可放宽到 15 分钟，但必须在 spec 的 Constraints 里写明。

## Spec 编排约定

- 每个 spec 改动量控制在 100-500 行新增代码、新增文件 ≤ 12 个、task 数 3-7
- 每个 spec 必须有"前置条件"章节，列出依赖的前序 spec 和外部前提
- 每个 spec 的 Out of Scope 章节必须明确列出推到后续 spec 的事项，防止边界蠕变
- Spec 序号永久不变，已发布 spec 的 requirements / design 不可重写。要修改通过新 spec 增量演进
- 每个 spec 完成后，把验证 trace（部署时长、verify 输出、destroy 输出）追加到 `docs/development-trace.md`

## 决策日志的归属

- 跨 spec 的架构决策 → 本文件
- spec 内部的设计决策 → 该 spec 的 `design.md` 的 Decisions and Tradeoffs 章节
- 实施期遇到的问题 → `docs/development-trace.md`
- 已知失败模式 → `shall-not.md`

四份文件分工不重叠，按"指令性 / 描述性 / 事实性 / 禁止性"分类。

## 维护规则

- 新增 convention 的来源：架构演进 / 跨 spec 复盘 / 工具链变化
- 修改既有 convention：必须在 `docs/development-trace.md` 留下变更说明（变更前 / 变更后 / 触发原因）
- 删除既有 convention：同上要求；优先打 deprecated 标记保留 1 个 spec 周期再删
