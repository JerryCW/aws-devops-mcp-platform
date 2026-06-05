"""TAM DevOps MCP Platform — CDK 入口。

架构分两步:
  第一步:部署 AgentCore Gateway(IAM 入站,无 target)
          cdk deploy DevopsMcpGatewayStack
  第二步:按需注册 MCP server target(每个 server 一个独立 stack)
          cdk deploy DevopsMcp-rdspostgres                        # 只装 PG
          cdk deploy DevopsMcp-rdspostgres DevopsMcp-valkey    # 装多个

设计原则:
- Gateway 与 target 解耦:先有 Gateway,再按需挂 server
- IAM 形态:inbound AWS_IAM(DevOps Agent SigV4),零 Cognito
- endpoint 不注入容器:MCP server 是通用工具,实例 endpoint 由 tool 调用时传入
- 配置驱动:server 全部由 servers.json 声明,新增数据源加配置 + 写 tool,不写新 stack 类

servers.json 缺失时只合成 Gateway stack(第一步可独立部署,不依赖 servers.json)。
"""

import json
import os
from pathlib import Path

import aws_cdk as cdk

from stacks.gateway_stack import GatewayStack
from stacks.target_stack import ServerConfig, TargetStack


_TARGET_STACK_PREFIX = "DevopsMcp-"
_PLACEHOLDER_PREFIX = "<REPLACE_ME_"


def _is_placeholder(value: str) -> bool:
    return isinstance(value, str) and value.startswith(_PLACEHOLDER_PREFIX)


def _require(value: str, key: str) -> str:
    if not value or _is_placeholder(value):
        raise ValueError(
            f"servers.json 的 {key!r} 未填或仍是 <REPLACE_ME_*> 占位符。"
            f"请复制 servers.json.example 为 servers.json 并改占位符。"
        )
    return value


app = cdk.App()

# region / account 由 CDK CLI 从当前 AWS 凭据 / profile 自动注入
# (CDK_DEFAULT_REGION / CDK_DEFAULT_ACCOUNT),不硬编码,便于切换 region。
# 覆盖优先级:命令行 `-c region=xxx` context > CDK_DEFAULT_REGION 环境变量 > AWS_REGION。
# 想临时指定:`AWS_REGION=ap-southeast-1 cdk deploy ...` 或 `cdk deploy -c region=ap-southeast-1 ...`
_region = (
    app.node.try_get_context("region")
    or os.environ.get("CDK_DEFAULT_REGION")
    or os.environ.get("AWS_REGION")
)
_account = os.environ.get("CDK_DEFAULT_ACCOUNT")
env = cdk.Environment(account=_account, region=_region)

# 第一步:Gateway(始终合成,不依赖 servers.json)
GatewayStack(app, "DevopsMcpGatewayStack", env=env)

# 第二步:按 servers.json 声明合成 target stack(文件不存在则跳过,只留 Gateway)
_servers_file = Path(__file__).resolve().parent / "servers.json"
if _servers_file.exists():
    cfg = json.loads(_servers_file.read_text(encoding="utf-8"))
    runtime_cfg = cfg.get("runtime", {})
    servers = cfg.get("servers", {})

    for name, s in servers.items():
        if not s.get("enabled", False):
            continue
        # 校验网络必填(占位符未替换则 fail-fast,但只在该 server enabled 时)
        vpc_id = _require(runtime_cfg.get("vpcId", ""), "runtime.vpcId")
        subnet_ids = [
            _require(sn, "runtime.subnetIds[]")
            for sn in runtime_cfg.get("subnetIds", [])
        ]
        azs = list(runtime_cfg.get("availabilityZones", []))
        egress_cidr = _require(s.get("egressCidr", ""), f"servers.{name}.egressCidr")

        config = ServerConfig(
            name=name,
            runtime_name=s["runtimeName"],
            source_dir=s["sourceDir"],
            secret_name=s["secretName"],
            secret_username=s["secretUsername"],
            egress_cidr=egress_cidr,
            egress_port=int(s["egressPort"]),
            extra_env={k: str(v) for k, v in s.get("extraEnv", {}).items()},
            description=s.get("description", ""),
        )
        TargetStack(
            app, f"{_TARGET_STACK_PREFIX}{name}",
            config=config,
            runtime_vpc_id=vpc_id,
            runtime_subnet_ids=subnet_ids,
            runtime_azs=azs,
            env=env,
        )

app.synth()
