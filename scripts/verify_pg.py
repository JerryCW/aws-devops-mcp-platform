"""PG target 验证(IAM SigV4 形态)。

两级验证:
  1. 必验:Gateway initialize 200 → tools/list 含 28 个 rdspostgres___ tool(target 挂载成功)
  2. 可选(传 --pg-endpoint 才做):tools/call inspect_long_transactions 真实巡检

设计:endpoint 不进部署,所以真实 tool call 需要调用方传 endpoint。
不传 --pg-endpoint 时只验挂载链路(客户数据源 user 还没配 / 不想连真库时用)。

用法:
  python scripts/verify_pg.py
  python scripts/verify_pg.py --pg-endpoint my-cluster.cluster-xxx.us-east-1.rds.amazonaws.com

末尾打印 `✅ PG target verification passed`,exit 0 = pass。

强约束:SHALL NOT #15(完整 traceback)/ #18(SSE utf-8)/ #21(三下划线)
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


# region 跟随当前 AWS 凭据 / profile(AWS_REGION / AWS_DEFAULT_REGION),不硬编码;
# 想临时指定:`AWS_REGION=ap-southeast-1 python scripts/verify_pg.py`
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

_TARGET_PREFIX = "rdspostgres___"
_EXPECTED_PG_TOOL_COUNT = 28
_PG_PROBE_TOOL = "rdspostgres___inspect_long_transactions"


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


def _assert_a8(payload: dict[str, Any]) -> None:
    required = {"status", "findings", "raw_data", "recommendation"}
    missing = required - set(payload.keys())
    assert not missing, f"A8 缺字段 {sorted(missing)},实际 {sorted(payload.keys())}"
    assert payload["status"] in ("ok", "warning", "critical"), f"status 异常:{payload['status']!r}"


def main() -> None:
    parser = argparse.ArgumentParser(description="PG target verifier")
    parser.add_argument(
        "--pg-endpoint", default="",
        help="真实 PG cluster endpoint;传了才做 tools/call(endpoint 不进部署)",
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
                   "clientInfo": {"name": "verify-pg", "version": "0"}},
    }), "initialize")
    print(f"✓ Gateway initialize OK (protocolVersion={init.get('protocolVersion')!r})")

    # 2) tools/list — 期望 20 个 rdspostgres___ tool
    lst = _extract_result(_sigv4_post_mcp(gw_url, {
        "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {},
    }), "tools/list")
    tool_names = sorted(t["name"] for t in lst.get("tools", []))
    pg_tools = [t for t in tool_names if t.startswith(_TARGET_PREFIX)]
    print(f"✓ tools/list OK — {len(pg_tools)} PG tools (target 挂载成功)")
    assert len(pg_tools) == _EXPECTED_PG_TOOL_COUNT, \
        f"PG tool 数异常:期望 {_EXPECTED_PG_TOOL_COUNT},实际 {len(pg_tools)}\n{pg_tools}"

    # 3) 可选 tools/call(传 --pg-endpoint 才做)
    if not args.pg_endpoint:
        print("⊘ tools/call 跳过(未传 --pg-endpoint)。endpoint 不进部署,"
              "真实巡检需调用方传 endpoint。")
    else:
        ep = args.pg_endpoint
        print(f"→ tools/call {_PG_PROBE_TOOL} (cluster_endpoint={ep})")
        res = _extract_result(_sigv4_post_mcp(gw_url, {
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": _PG_PROBE_TOOL,
                       "arguments": {"cluster_endpoint": ep, "threshold_seconds": 60}},
        }), "tools/call PG")
        if res.get("isError"):
            err = next((c.get("text", "") for c in res.get("content", [])
                        if c.get("type") == "text"), "")
            raise RuntimeError(
                f"PG tool {_PG_PROBE_TOOL} 失败:{err!r}\n"
                "可能:mcp_devops_ro user 未配 / 密码未灌 / 网络不通 / endpoint 错误"
            )
        payload = json.loads(res["content"][0]["text"])
        _assert_a8(payload)
        n_trx = len(payload["raw_data"].get("transactions", []))
        print(f"✓ PG tool OK (status={payload['status']}, transactions={n_trx})")

    print("\n✅ PG target verification passed")
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
