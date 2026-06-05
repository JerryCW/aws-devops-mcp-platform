"""TargetStack — 第二步:通用 MCP server target stack(配置驱动)。

一个 TargetStack 实例 = 一个 MCP server = 一个独立部署单元:
- 1 个 AgentCore Runtime(VPC 模式,AWS_IAM 入站)从 source_path 构建 ARM64 容器
- 1 个 SM Secret 空壳(客户灌真密码)
- 1 个 Gateway target(跨 stack 引用第一步的 Gateway,IAM SigV4 出站)
- Gateway role 加 InvokeAgentRuntime 权限(scoped 到本 Runtime)

新增数据源 = 加一份 ServerConfig + 写 mcp_servers/<name>/ 容器代码,**不写新 stack 类**。

设计要点:
- **不注入 endpoint**:容器只拿 DB_PORT / DB_SECRET_NAME / AWS_REGION,实例 endpoint 由
  tool 调用必传。SG egress 用网络层 CIDR(部署必需),不暴露任何具体实例。
- 独立 stack 名 `DevopsMcp-<server>`,可单独 deploy / destroy。
- 跨 stack 引用第一步 Gateway 的 export(DevopsMcp-Gateway*)。

═══════════════════════════════════════════════════════════════════════════
扩展指南(接更多数据源时看这里)
═══════════════════════════════════════════════════════════════════════════

【红线纪律:加 target 永远不碰 Gateway】
  依赖方向是单向的:Gateway stack(被引用) ← Fn.import_value ← Target stack(引用)。
  挂 target / 给 Gateway role 加 InvokeAgentRuntime policy 都发生在 **target stack 名下**,
  不改 Gateway stack 资源。所以 `cdk deploy DevopsMcp-<new>` 只动这一个 stack,
  Gateway 零接触。**永远不要把任何 target 相关逻辑写进 gateway_stack.py 或 app.py 的
  Gateway 那段** —— 守住这条,Gateway 永不重部署。

【当前 ServerConfig 的假设】
  本 stack 当前为「VPC 内、user/password 认证、单 TCP 端口」的数据源设计
  (PG / Valkey / MySQL / MQ 都符合)。每个 target 必建一个 {username,password} Secret,
  Runtime 角色只 grant_read 那个 Secret。

【接 IAM 认证类数据源(MSK / OpenSearch)时怎么扩展 —— 增量,framework / Gateway 不动】
  这类服务用 IAM(SASL/IAM 或 SigV4)而非 user/password,且常需多端口 + 额外数据源权限。
  按下面扩展 ServerConfig + 本 stack(改动 ~30 行,PG/Valkey 不受影响):
    1. secret_name / secret_username 改成 Optional;为 None 时跳过建 Secret 那步
    2. egress_port 扩成 egress_ports: list[int](MSK 多端口 9098/9094);SG egress 循环加
    3. 加 extra_iam_statements: list[dict];新增一步:
         for stmt in config.extra_iam_statements:
             runtime.execution_role.add_to_principal_policy(iam.PolicyStatement(**stmt))
       (MSK 拿 kafka-cluster:Connect/DescribeCluster/ReadData;OpenSearch 拿 es:ESHttpGet 等)
    4. mcp_servers/<name>/ 里写该数据源的巡检 tool,认证用 IAM signer,endpoint 仍 tool 必传
  framework/(runtime_construct 已暴露 execution_role 口子)与 Gateway 全程不动。

  ⚠ 不要提前为还没接的数据源抽象 —— 等真接时对着真实集群改,30 行的事,改对率更高。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import (
    RemovalPolicy,
    SecretValue,
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_secretsmanager as secretsmanager,
)
from constructs import Construct

from framework.gateway_construct import DevopsMcpGateway
from framework.runtime_construct import DevopsMcpRuntime


_GW_EXPORT_PREFIX: str = "DevopsMcp-"
_TARGET_NAME_MAX_LEN: int = 15  # 与 framework/gateway_construct 一致;Gateway 三下划线前缀长度约束


@dataclass(frozen=True)
class ServerConfig:
    """一个 MCP server target 的部署配置(配置驱动,新增数据源只加这个)。"""

    # 业务标识
    name: str                       # target 名(≤15 字符,alphanumeric),如 "rdspostgres"
    runtime_name: str               # AgentCore Runtime 名(≤48,允许下划线),如 "devops_mcp_rds_postgres"
    source_dir: str                 # 容器源码目录(相对项目根),如 "mcp_servers/rds_postgres"
    secret_name: str                # SM Secret 命名,如 "aws-devops-mcp/rds-postgres/devops-readonly"
    secret_username: str            # 凭据 username,如 "mcp_devops_ro"

    # 网络(部署必需 — 不含任何具体实例 endpoint)
    egress_cidr: str                # 数据源 VPC CIDR,SG egress 放行,如 "10.1.0.0/16"
    egress_port: int                # 数据源端口,如 5432 / 6379

    # 容器环境变量(不含 endpoint)。端口等容器特定变量在这里传,
    # 变量名由各容器自己约定(PG 用 DB_PORT,Valkey 用 REDIS_PORT),
    # target_stack 不预设任何端口变量名,保持通用。
    extra_env: dict[str, str] = field(default_factory=dict)

    description: str = ""            # Gateway target 描述


class TargetStack(cdk.Stack):
    """通用 MCP server target stack。"""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: ServerConfig,
        runtime_vpc_id: str,
        runtime_subnet_ids: list[str],
        runtime_azs: list[str],
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        if len(config.name) > _TARGET_NAME_MAX_LEN:
            raise ValueError(
                f"server name {config.name!r} 超过 {_TARGET_NAME_MAX_LEN} 字符上限"
                "(Gateway 给 tool 加 {target}___ 前缀后总长 ≤ 64 的约束)"
            )
        if len(runtime_subnet_ids) != len(runtime_azs):
            raise ValueError(
                f"subnet_ids 与 azs 数量不一致:{len(runtime_subnet_ids)} vs {len(runtime_azs)}"
            )

        export_prefix = f"{_GW_EXPORT_PREFIX}{config.name}-"
        source_path = Path(__file__).resolve().parent.parent / config.source_dir

        # 1) 跨 stack 引用第一步 Gateway
        gateway_arn = cdk.Fn.import_value(f"{_GW_EXPORT_PREFIX}GatewayArn")
        gateway_id = cdk.Fn.import_value(f"{_GW_EXPORT_PREFIX}GatewayId")
        gateway_name = cdk.Fn.import_value(f"{_GW_EXPORT_PREFIX}GatewayName")
        gateway_role_arn = cdk.Fn.import_value(f"{_GW_EXPORT_PREFIX}GatewayRoleArn")

        # mutable=True:本 stack 给 gateway role 加 InvokeAgentRuntime 权限。
        # construct id 带 server name 后缀,避免多 target stack 并行 deploy 时
        # IAM Policy logical id 冲突(SHALL NOT #24)。
        gateway_role = iam.Role.from_role_arn(
            self, f"ImportedGatewayRole{config.name}", gateway_role_arn, mutable=True,
        )

        # 2) VPC + subnet
        vpc = ec2.Vpc.from_vpc_attributes(
            self, "RuntimeVpc",
            vpc_id=runtime_vpc_id,
            availability_zones=runtime_azs,
        )
        runtime_subnets = ec2.SubnetSelection(
            subnets=[
                ec2.Subnet.from_subnet_id(self, f"PrivateSubnet{i}", sn)
                for i, sn in enumerate(runtime_subnet_ids)
            ],
        )

        # 3) Runtime SG — egress 到数据源网段 + NAT 443 + DNS 53
        runtime_sg = ec2.SecurityGroup(
            self, "RuntimeSg",
            vpc=vpc,
            description=f"TAM DevOps MCP / {config.name} Runtime SG",
            allow_all_outbound=False,
        )
        runtime_sg.add_egress_rule(
            ec2.Peer.ipv4(config.egress_cidr),
            ec2.Port.tcp(config.egress_port),
            f"{config.name} runtime to datasource VPC",
        )
        runtime_sg.add_egress_rule(
            ec2.Peer.any_ipv4(), ec2.Port.tcp(443),
            "runtime to ECR / SecretsManager / CloudWatch via NAT",
        )
        runtime_sg.add_egress_rule(
            ec2.Peer.any_ipv4(), ec2.Port.udp(53), "DNS UDP",
        )
        runtime_sg.add_egress_rule(
            ec2.Peer.any_ipv4(), ec2.Port.tcp(53), "DNS TCP fallback",
        )

        # 4) 凭据 Secret 空壳(客户灌真密码)
        secret = secretsmanager.Secret(
            self, "ReadOnlyCredentials",
            secret_name=config.secret_name,
            description=(
                f"{config.name} {config.secret_username} 凭据 — 由客户手工灌真密码"
            ),
            removal_policy=RemovalPolicy.DESTROY,
            secret_object_value={
                "username": SecretValue.unsafe_plain_text(config.secret_username),
                "password": SecretValue.unsafe_plain_text("PLACEHOLDER_REPLACE_ME"),
            },
        )

        # 5) Runtime(VPC 模式,IAM 入站)— 不注入 endpoint。
        # 端口等容器特定变量由 config.extra_env 传(变量名各容器自定,见 servers.json)。
        env_vars = {
            "DB_SECRET_NAME": config.secret_name,
            "AWS_REGION": self.region,
            **config.extra_env,
        }
        runtime = DevopsMcpRuntime(
            self, "Runtime",
            source_path=source_path,
            runtime_name=config.runtime_name,
            vpc=vpc,
            subnets=runtime_subnets,
            security_groups=[runtime_sg],
            environment_variables=env_vars,
        )

        # 6) Runtime 角色加 SM 读权限(限定本 Secret)
        secret.grant_read(runtime.execution_role)

        # 7) Gateway role 加 InvokeAgentRuntime 权限(scoped 本 Runtime)
        gateway_role.add_to_principal_policy(
            iam.PolicyStatement(
                actions=["bedrock-agentcore:InvokeAgentRuntime"],
                resources=[runtime.runtime_arn, f"{runtime.runtime_arn}/*"],
            )
        )

        # 8) 挂 target 到第一步 Gateway
        target_arn = DevopsMcpGateway.add_mcp_target_to_existing(
            self, f"{config.name}Tgt",
            gateway_arn=gateway_arn,
            gateway_id=gateway_id,
            gateway_name=gateway_name,
            gateway_role=gateway_role,
            runtime_arn=runtime.runtime_arn,
            runtime_url=runtime.runtime_url,
            target_name=config.name,
            description=config.description or f"MCP target {config.name}",
        )

        # ===== Outputs =====
        cdk.CfnOutput(
            self, "RuntimeArn", value=runtime.runtime_arn,
            description="AgentCore Runtime ARN",
            export_name=f"{export_prefix}RuntimeArn",
        )
        cdk.CfnOutput(
            self, "RuntimeUrl", value=runtime.runtime_url,
            description="Runtime invocation URL",
            export_name=f"{export_prefix}RuntimeUrl",
        )
        cdk.CfnOutput(
            self, "SecretArn", value=secret.secret_arn,
            description=f"{config.secret_username} Secret ARN(客户灌真密码)",
            export_name=f"{export_prefix}SecretArn",
        )
        cdk.CfnOutput(
            self, "SecretName", value=config.secret_name,
            description="Secret 命名(客户灌密码时用此 name)",
            export_name=f"{export_prefix}SecretName",
        )
        cdk.CfnOutput(
            self, "TargetArn", value=target_arn,
            description=f"Gateway MCP target ARN({config.name})",
            export_name=f"{export_prefix}TargetArn",
        )
