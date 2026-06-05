"""OpenSearch target 验证(IAM SigV4 形态)。

两级验证:
  1. 必验:Gateway initialize 200 → tools/list 含 opensearch___ tool(target 挂载成功)
  2. 可选(传 --opensearch-url 才做):tools/call ListIndexTool 真实巡检

设计:endpoint 不进部署。OpenSearch MCP(迁移自 opensearch-project)single 模式
支持 per-call `opensearch_url` override,所以真实 tool call 时传 endpoint。
不传 --opensearch-url 时只验挂载链路。

用法:
  python scripts/verify_opensearch.py
  python scripts/verify_opensearch.py --opensearch-url https://vpc-xxx.ap-southeast-1.es.amazonaws.com

末尾打印 `✅ OpenSearch target verification passed`,exit 0 = pass。

强约束:SHALL NOT #15(完整 traceback)/ #18(SSE utf-8)/ #21(三下划线)/ #25(tools/list 容忍漏)
"""
from __future__ import annotations

import argparse
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

_TARGET_PREFIX = "opensearch___"
# upstream single 模式默认启用 9 个工具(agentic_memory/skills 默认不开)
_MIN_EXPECTED_TOOLS = 5
_PROBE_TOOL = "opensearch___ListIndexTool"


def _read_outputs(stack: str) -> dict[str, str]:
    cf = boto3.client("cloudformation", region_name=_REGION)
    stacks = cf.describe_stacks(StackName=stack)["Stacks"]
    return {o["OutputKey"]: o["OutputValue"] for o in stacks[0]["Outputs"]}


def _sigv4_post_mcp(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(payload)
    aws_request = AWSRequest(
        method="POST", url=url, data=body,
        headers={"Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream"},
    )
    creds = botocore.session.Session().get_credentials().get_frozen_credentials()
    SigV4Auth(creds, _SERVICE, _REGION).add_auth(aws_request)
    prepared = aws_request.prepare()
    r = requests.post(prepared.url, headers=dict(prepared.headers), data=body,
                      timeout=_TIMEOUT_HTTP, stream=True)
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
    parser = argparse.ArgumentParser(description="OpenSearch target verifier")
    parser.add_argument(
        "--opensearch-url", default="",
        help="真实 OpenSearch domain endpoint(https://...);传了才做 tools/call",
    )
    args = parser.parse_args()

    gateway = _read_outputs(_GATEWAY_STACK)
    print(f"✓ {_GATEWAY_STACK} outputs read")

    caller = boto3.client("sts", region_name=_REGION).get_caller_identity()
    print(f"✓ Caller IAM identity: {caller['Arn']}")

    gw_url = gateway["GatewayUrl"]

    # 1) initialize
    init = _extract_result(_sigv4_post_mcp(gw_url, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": _MCP_PROTOCOL, "capabilities": {},
                   "clientInfo": {"name": "verify-opensearch", "version": "0"}},
    }), "initialize")
    print(f"✓ Gateway initialize OK (protocolVersion={init.get('protocolVersion')!r})")

    # 2) tools/list — 期望若干 opensearch___ tool(SHALL NOT #25:容忍 Gateway 漏)
    lst = _extract_result(_sigv4_post_mcp(gw_url, {
        "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {},
    }), "tools/list")
    tool_names = sorted(t["name"] for t in lst.get("tools", []))
    os_tools = [t for t in tool_names if t.startswith(_TARGET_PREFIX)]
    print(f"✓ tools/list OK — {len(os_tools)} OpenSearch tools (target 挂载成功)")
    for t in os_tools:
        print(f"    - {t}")
    assert len(os_tools) >= _MIN_EXPECTED_TOOLS, (
        f"OpenSearch tool 数异常:期望 ≥ {_MIN_EXPECTED_TOOLS},实际 {len(os_tools)}\n{os_tools}"
    )

    # 3) 可选 tools/call(传 --opensearch-url 才做)
    if not args.opensearch_url:
        print("⊘ tools/call 跳过(未传 --opensearch-url)。endpoint 不进部署,"
              "真实巡检需调用方传 opensearch_url。")
    else:
        url = args.opensearch_url
        print(f"→ tools/call {_PROBE_TOOL} (opensearch_url={url})")
        res = _extract_result(_sigv4_post_mcp(gw_url, {
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": _PROBE_TOOL,
                       "arguments": {"opensearch_url": url, "include_detail": False}},
        }), "tools/call OpenSearch")
        if res.get("isError"):
            err = next((c.get("text", "") for c in res.get("content", [])
                        if c.get("type") == "text"), "")
            raise RuntimeError(
                f"OpenSearch tool {_PROBE_TOOL} 失败:{err!r}\n"
                "可能:mcp_devops_ro user 未配 / 密码未灌 / 网络不通(SG 443) / endpoint 错误"
            )
        texts = [c.get("text", "") for c in res.get("content", []) if c.get("type") == "text"]
        joined = " | ".join(t[:200] for t in texts)
        print(f"✓ OpenSearch tool OK — {joined[:400]}")

    print("\n✅ OpenSearch target verification passed")
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
