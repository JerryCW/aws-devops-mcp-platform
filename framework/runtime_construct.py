"""DevopsMcpRuntime — 框架核心 Construct(IAM 形态)。

一站式封装:
- AgentCore Runtime(MCP 协议、AWS_IAM 入站、X-Ray、CloudWatch Logs)
- 从本地 Dockerfile 构建 ARM64 镜像(CDK asset → bootstrap ECR repo)
- VPC 模式可选(传 vpc + subnets + security_groups)

设计要点(对应 .kiro/steering/shall-not.md / project-conventions.md):
- 第 2 / 第 8 条 alpha 隔离:`aws_bedrock_agentcore_alpha` import **仅在 framework/ 出现**;
  Construct 对外只暴露 Python 原生类型与稳定 CDK 类型(str / IRole / IVpc 等)
- 第 6 条 强制 IAM 入站:authorizer 固定 `using_iam()`,不接受 NoAuth / Cognito JWT
- 第 7 条 优先 L2:用 `agentcore.Runtime` 而不是 CfnRuntime
- 第 10 条 最小权限:依赖 L2 自动创建的执行角色(仅 ECR pull / Logs 写 / X-Ray 写)
- 第 14 条 RemovalPolicy.DESTROY:LogGroup 全部 DESTROY

对外稳定 API(全 Python 原生 / stable CDK 类型):
    source_path:               MCP server 容器源码目录(含 Dockerfile)
    runtime_name:              AgentCore Runtime 名(满足 ^[a-zA-Z][a-zA-Z0-9_]{0,47}$)
    environment_variables:     容器环境变量
    vpc:                       ec2.IVpc | None;非 None 时 Runtime 切 VPC 网络模式
    subnets:                   ec2.SubnetSelection | None
    security_groups:           list[ec2.ISecurityGroup] | None

对外稳定属性:
    runtime_arn:    AgentCore Runtime ARN(token)
    runtime_url:    invocation URL(token)
    execution_role: iam.IRole;Stack 层用 grant_read 给 SecretsManager / 数据源加最小权限
"""

from __future__ import annotations

from pathlib import Path
from typing import Mapping

from aws_cdk import (
    Fn,
    RemovalPolicy,
    Stack,
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_logs as logs,
)
from aws_cdk import aws_bedrock_agentcore_alpha as agentcore  # !!! alpha 隔离唯一允许出现的位置之一
from constructs import Construct


class DevopsMcpRuntime(Construct):
    """AgentCore Runtime(IAM 入站)+ 容器镜像 + 日志的一站式封装。"""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        source_path: str | Path,
        runtime_name: str,
        environment_variables: Mapping[str, str] | None = None,
        vpc: ec2.IVpc | None = None,
        subnets: ec2.SubnetSelection | None = None,
        security_groups: list[ec2.ISecurityGroup] | None = None,
    ) -> None:
        super().__init__(scope, construct_id)

        # 防御性校验:VPC 三参不能"半给"
        if vpc is None and (subnets is not None or security_groups is not None):
            raise ValueError(
                "subnets / security_groups 非 None 时 vpc 必须非 None(VPC 模式硬约束)"
            )

        stack = Stack.of(self)

        # ---- 1) ARM64 镜像 asset(CDK 自动构建 + 上传到 bootstrap ECR repo) ----
        artifact = agentcore.AgentRuntimeArtifact.from_asset(
            str(Path(source_path).resolve()),
        )

        # ---- 2) Application logs LogGroup ----
        log_group = logs.LogGroup(
            self, "LogGroup",
            log_group_name=f"/aws/bedrock-agentcore/{runtime_name}",
            removal_policy=RemovalPolicy.DESTROY,
            retention=logs.RetentionDays.ONE_WEEK,
        )

        # ---- 3) Runtime(MCP + AWS_IAM 入站 + X-Ray + CW Logs) ----
        if vpc is not None:
            network_configuration = agentcore.RuntimeNetworkConfiguration.using_vpc(
                self, vpc=vpc, vpc_subnets=subnets, security_groups=security_groups,
            )
        else:
            network_configuration = None

        runtime = agentcore.Runtime(
            self, "Runtime",
            runtime_name=runtime_name,
            agent_runtime_artifact=artifact,
            protocol_configuration=agentcore.ProtocolType.MCP,
            authorizer_configuration=agentcore.RuntimeAuthorizerConfiguration.using_iam(),
            tracing_enabled=True,
            logging_configs=[
                agentcore.LoggingConfig(
                    log_type=agentcore.LogType.APPLICATION_LOGS,
                    destination=agentcore.LoggingDestination.cloud_watch_logs(log_group),
                ),
            ],
            network_configuration=network_configuration,
            environment_variables=(
                dict(environment_variables) if environment_variables else None
            ),
        )

        self.runtime_arn: str = runtime.agent_runtime_arn
        self.runtime_url: str = self._build_runtime_invocation_url(
            stack=stack, runtime_arn=runtime.agent_runtime_arn,
        )
        self.execution_role: iam.IRole = runtime.role

    @staticmethod
    def _build_runtime_invocation_url(*, stack: Stack, runtime_arn: str) -> str:
        """在 CFN 端拼接 AgentCore Runtime invocation URL(URL-encode ARN)。

        TODO:依赖 "ARN 6 段、末段单个 /" 的假设手工拼接。等 alpha 包暴露
        runtime.invocation_url(或等价属性)后,直接换成它,删掉本方法。
        """
        arn_parts = Fn.split(":", runtime_arn)
        last_path_segments = Fn.split("/", Fn.select(5, arn_parts))
        last_encoded = Fn.join(
            "%2F",
            [Fn.select(0, last_path_segments), Fn.select(1, last_path_segments)],
        )
        encoded_arn = Fn.join(
            "%3A",
            [
                Fn.select(0, arn_parts),
                Fn.select(1, arn_parts),
                Fn.select(2, arn_parts),
                Fn.select(3, arn_parts),
                Fn.select(4, arn_parts),
                last_encoded,
            ],
        )
        return Fn.join(
            "",
            [
                "https://bedrock-agentcore.",
                stack.region,
                ".amazonaws.com/runtimes/",
                encoded_arn,
                "/invocations?qualifier=DEFAULT",
            ],
        )
