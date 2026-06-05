"""DevopsMcpGateway — 第一步:只创建 AgentCore Gateway(IAM 形态,无 target)。

架构分两步:
  第一步(本文件):部署 1 个 Gateway(AWS_IAM 入站),输出 Id / Url / Arn / RoleArn
  第二步:各 MCP server 作为独立 target stack,跨 stack 引用本 Gateway 自挂 target

设计要点(沉淀自实战教训,见 .kiro/steering/shall-not.md):
- alpha 隔离(SHALL NOT #2):`aws_bedrock_agentcore_alpha` import 只在 framework/ 出现,
  Construct 对外只暴露 Python 原生类型(str / IRole)
- IAM 入站(SHALL NOT #8):authorizer 固定 using_aws_iam(),不接受 NoAuth / Cognito
- supported_versions 必传(SHALL NOT #19):GatewayProtocol.mcp() 的 supported_versions
  alpha 标 Optional 但服务端要求非空,显式传
- Gateway name 不允许下划线(project-conventions A10):用 hyphen
- RemovalPolicy.DESTROY(SHALL NOT #14):验证性资源可完全清理

对外稳定属性:
    gateway_id / gateway_url / gateway_arn / gateway_name: str
    gateway_role: iam.IRole
    log_group: logs.ILogGroup
"""

from __future__ import annotations

from typing import Final

from aws_cdk import (
    RemovalPolicy,
    Stack,
    aws_iam as iam,
    aws_logs as logs,
)
from aws_cdk import aws_bedrock_agentcore_alpha as agentcore  # !!! alpha 隔离唯一允许出现的位置
from constructs import Construct


_TARGET_NAME_MAX_LEN: Final[int] = 15


class DevopsMcpGateway(Construct):
    """AgentCore Gateway(AWS_IAM 入站,无 target)。"""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        gateway_name: str,
    ) -> None:
        super().__init__(scope, construct_id)

        # ---- 1) LogGroup ----
        log_group = logs.LogGroup(
            self, "LogGroup",
            log_group_name=f"/aws/bedrock-agentcore/gateway/{gateway_name}",
            removal_policy=RemovalPolicy.DESTROY,
            retention=logs.RetentionDays.ONE_WEEK,
        )

        # ---- 2) Gateway(MCP + AWS_IAM 入站) ----
        # supported_versions alpha 标 Optional 但服务端要求非空(SHALL NOT #19)
        gateway = agentcore.Gateway(
            self, "Gateway",
            gateway_name=gateway_name,
            protocol_configuration=agentcore.GatewayProtocol.mcp(
                supported_versions=[agentcore.MCPProtocolVersion.MCP_2025_03_26],
            ),
            authorizer_configuration=agentcore.GatewayAuthorizer.using_aws_iam(),
        )
        gateway.apply_removal_policy(RemovalPolicy.DESTROY)

        # ---- 暴露 stable 属性 ----
        self.gateway_id: str = gateway.gateway_id
        self.gateway_url: str = gateway.gateway_url
        self.gateway_arn: str = gateway.gateway_arn
        self.gateway_name: str = gateway_name
        self.gateway_role: iam.IRole = gateway.role
        self.log_group: logs.ILogGroup = log_group

    # ====================================================================== #
    # 第二步:跨 stack 给已存在 Gateway 挂一个 MCP target(IAM SigV4 出站)    #
    # ====================================================================== #
    @staticmethod
    def add_mcp_target_to_existing(
        scope: Construct,
        construct_id: str,
        *,
        gateway_arn: str,
        gateway_id: str,
        gateway_name: str,
        gateway_role: iam.IRole,
        runtime_arn: str,
        runtime_url: str,
        target_name: str,
        description: str | None = None,
    ) -> str:
        """跨 stack 给已存在 Gateway 挂一个 IAM-SigV4 MCP target,返回 target ARN。

        把 alpha L2 调用 + L1 escape hatch 完整封装,Stack 层只调本方法
        (stable 入参 / stable 返回),alpha 类型不外漏(SHALL NOT #2)。

        关键步骤(每步对应实战教训):
        1. Gateway.from_gateway_attributes 反向构造 IGateway 引用(只读)
        2. GatewayCredentialProvider.from_iam_role() 生成 GATEWAY_IAM_ROLE 凭据配置
        3. GatewayTarget.for_mcp_server(...) 构造 GatewayTarget L2 资源
        4. L1 escape hatch 补 IamCredentialProvider.Service=bedrock-agentcore + Region
           (alpha L2 from_iam_role() 不生成此字段,服务端 schema 必填;SHALL NOT #23)
        """
        if len(target_name) > _TARGET_NAME_MAX_LEN:
            raise ValueError(
                f"target_name {target_name!r} 超过 {_TARGET_NAME_MAX_LEN} 字符上限"
            )

        stack = Stack.of(scope)

        # 1) 反向构造 IGateway
        imported_gateway = agentcore.Gateway.from_gateway_attributes(
            scope, f"{construct_id}ImportedGateway",
            gateway_arn=gateway_arn,
            gateway_id=gateway_id,
            gateway_name=gateway_name,
            role=gateway_role,
        )

        # 2) IAM role 凭据配置
        target_creds = agentcore.GatewayCredentialProvider.from_iam_role()

        # 3) GatewayTarget L2
        target = agentcore.GatewayTarget.for_mcp_server(
            scope, f"{construct_id}Target",
            gateway=imported_gateway,
            credential_provider_configurations=[target_creds],
            endpoint=runtime_url,
            gateway_target_name=target_name,
            description=description or f"MCP target {target_name}",
        )
        target.apply_removal_policy(RemovalPolicy.DESTROY)

        # 4) L1 escape hatch 补 IamCredentialProvider.Service / Region(SHALL NOT #23)
        cfn_target = target.node.default_child  # type: ignore[assignment]
        cfn_target.add_property_override(
            "CredentialProviderConfigurations.0.CredentialProvider.IamCredentialProvider.Service",
            "bedrock-agentcore",
        )
        cfn_target.add_property_override(
            "CredentialProviderConfigurations.0.CredentialProvider.IamCredentialProvider.Region",
            stack.region,
        )

        return target.target_arn
