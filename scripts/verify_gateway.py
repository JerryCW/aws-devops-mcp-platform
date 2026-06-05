"""第一步验证:确认 Gateway 已部署且 SigV4 链路通(无 target 时 tools/list 为空)。

通过条件:
  当前 AWS 凭据 SigV4 签 Gateway → initialize 200 OK → tools/list 成功返回(0 个 tool)

末尾打印 `✅ Gateway verification passed`,exit 0 = pass。

用法:
  python scripts/verify_gateway.py

强约束:
  SHALL NOT #15  失败打印完整 traceback
  SHALL NOT #18  SSE 强制 utf-8 解析
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from typing import Any

import boto3
import botocore.session
import requests
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest


# region 跟随当前 AWS 凭据 / profile(AWS_REGION / AWS_DEFAULT_REGION),不硬编码;
# 想临时指定:`AWS_REGION=ap-southeast-1 python scripts/verify_gateway.py`
_REGION = (
    os.environ.get("AWS_REGION")
    or os.environ.get("AWS_DEFAULT_REGION")
    or boto3.session.Session().region_name
    or "us-east-1"
)
_SERVICE = "bedrock-agentcore"
_GATEWAY_STACK = "DevopsMcpGatewayStack"
_TIMEOUT_HTTP = 60
_MCP_PROTOCOL = "2025-03-26"


def _read_outputs(stack: str) -> dict[str, str]:
    cf = boto3.client("cloudformation", region_name=_REGION)
    stacks = cf.describe_stacks(StackName=stack)["Stacks"]
    return {o["OutputKey"]: o["OutputValue"] for o in stacks[0]["Outputs"]}


def _sigv4_post_mcp(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    """SigV4 签 + POST + 解析 SSE/JSON 响应(SSE 强制 utf-8,SHALL NOT #18)。"""
    body = json.dumps(payload)
    aws_request = AWSRequest(
        method="POST",
        url=url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
    )
    session = botocore.session.Session()
    credentials = session.get_credentials().get_frozen_credentials()
    SigV4Auth(credentials, _SERVICE, _REGION).add_auth(aws_request)

    prepared = aws_request.prepare()
    r = requests.post(
        prepared.url,
        headers=dict(prepared.headers),
        data=body,
        timeout=_TIMEOUT_HTTP,
        stream=True,
    )
    r.raise_for_status()
    ct = r.headers.get("Content-Type", "").lower()
    if "text/event-stream" in ct:
        for raw in r.iter_lines(decode_unicode=False):
            if not raw:
                continue
            line = raw.decode("utf-8").strip()
            if line.startswith("data:"):
                return json.loads(line[len("data:"):].strip())
        raise RuntimeError(f"SSE no data frame from {url!r}")
    if "application/json" in ct:
        return json.loads(r.content.decode("utf-8"))
    raise RuntimeError(f"unexpected Content-Type {ct!r}, body={r.content[:300]!r}")


def _extract_result(resp: dict[str, Any], step: str) -> dict[str, Any]:
    if "error" in resp:
        raise RuntimeError(f"MCP {step} JSON-RPC error: {resp['error']!r}")
    if "result" not in resp:
        raise RuntimeError(f"MCP {step} missing 'result': {resp!r}")
    return resp["result"]


def main() -> None:
    gateway = _read_outputs(_GATEWAY_STACK)
    print(f"✓ {_GATEWAY_STACK} outputs read")

    sts = boto3.client("sts", region_name=_REGION)
    caller = sts.get_caller_identity()
    print(f"✓ Caller IAM identity: {caller['Arn']}")

    gw_url = gateway["GatewayUrl"]
    print(f"✓ Gateway URL: {gw_url}")

    init_resp = _sigv4_post_mcp(gw_url, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": _MCP_PROTOCOL, "capabilities": {},
                   "clientInfo": {"name": "verify-gateway", "version": "0"}},
    })
    init_result = _extract_result(init_resp, "initialize")
    print(f"✓ Gateway initialize OK (server protocolVersion={init_result.get('protocolVersion')!r})")

    list_resp = _sigv4_post_mcp(gw_url, {
        "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {},
    })
    list_result = _extract_result(list_resp, "tools/list")
    tool_names = sorted(t["name"] for t in list_result.get("tools", []))
    print(f"✓ tools/list OK — {len(tool_names)} tools registered "
          f"{'(无 target,符合第一步预期)' if not tool_names else tool_names}")

    print("\n✅ Gateway verification passed")
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
