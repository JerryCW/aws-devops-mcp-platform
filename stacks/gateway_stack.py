"""GatewayStack — 第一步:AgentCore Gateway `aws-devops-mcp`(IAM 形态,无 target)。

职责:
- 1 个 Gateway(name=aws-devops-mcp,AWS_IAM 入站)
- 不挂任何 target — 第二步各 MCP server target stack 跨 stack 引用本 Gateway 自挂 target

跨 stack 契约(导出供第二步 target stack 用 Fn.import_value 引用):
- DevopsMcp-GatewayId
- DevopsMcp-GatewayUrl
- DevopsMcp-GatewayArn
- DevopsMcp-GatewayName
- DevopsMcp-GatewayRoleArn(target stack 引用,加 InvokeAgentRuntime 权限)
- DevopsMcp-Region
"""

from __future__ import annotations

import aws_cdk as cdk
from constructs import Construct

from framework.gateway_construct import DevopsMcpGateway


_EXPORT_PREFIX: str = "DevopsMcp-"
_GATEWAY_NAME: str = "aws-devops-mcp"  # alphanumeric + hyphen ✓(不允许下划线,A10)


class GatewayStack(cdk.Stack):
    """AgentCore Gateway 单 stack(无 target,无 Cognito)。"""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        gateway = DevopsMcpGateway(
            self, "Gateway",
            gateway_name=_GATEWAY_NAME,
        )

        cdk.CfnOutput(
            self, "GatewayId",
            value=gateway.gateway_id,
            description="AgentCore Gateway ID",
            export_name=f"{_EXPORT_PREFIX}GatewayId",
        )
        cdk.CfnOutput(
            self, "GatewayArn",
            value=gateway.gateway_arn,
            description="AgentCore Gateway ARN",
            export_name=f"{_EXPORT_PREFIX}GatewayArn",
        )
        cdk.CfnOutput(
            self, "GatewayName",
            value=gateway.gateway_name,
            description="AgentCore Gateway name",
            export_name=f"{_EXPORT_PREFIX}GatewayName",
        )
        cdk.CfnOutput(
            self, "GatewayUrl",
            value=gateway.gateway_url,
            description="AgentCore Gateway invocation URL(用 SigV4 签名调,service=bedrock-agentcore)",
            export_name=f"{_EXPORT_PREFIX}GatewayUrl",
        )
        cdk.CfnOutput(
            self, "GatewayRoleArn",
            value=gateway.gateway_role.role_arn,
            description="Gateway 执行角色 ARN(第二步 target stack 跨 stack 引用,加 InvokeAgentRuntime 权限)",
            export_name=f"{_EXPORT_PREFIX}GatewayRoleArn",
        )
        cdk.CfnOutput(
            self, "Region",
            value=self.region,
            description="AWS region",
            export_name=f"{_EXPORT_PREFIX}Region",
        )
