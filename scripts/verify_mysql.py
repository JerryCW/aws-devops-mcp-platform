"""RDS/Aurora MySQL target 验证(IAM SigV4 形态)。

两级验证:
  1. 必验:Gateway initialize 200 → tools/list 含 18 个 rdsmysql___ tool(target 挂载成功)
  2. 可选(传 --mysql-endpoint 才做):tools/call inspect_processlist 真实巡检

设计:endpoint 不进部署,所以真实 tool call 需要调用方传 endpoint。
不传 --mysql-endpoint 时只验挂载链路。

用法:
  python scripts/verify_mysql.py
  python scripts/verify_mysql.py --mysql-endpoint my-db.cluster-xxxx.us-east-1.rds.amazonaws.com

末尾打印 `✅ MySQL target verification passed`,exit 0 = pass。

强约束:SHALL NOT #15(完整 traceback)/ #18(SSE utf-8)/ #21(三下划线)/ #25(tools/list 容忍 N-1)
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

_TARGET_PREFIX = "rdsmysql___"
_EXPECTED_TOOL_COUNT = 18
_PROBE_TOOL = "rdsmysql___inspect_processlist"

_EXPECTED_TOOLS = {
    f"{_TARGET_PREFIX}{name}"
    for name in (
        "inspect_processlist", "inspect_long_transactions", "inspect_lock_waits",
        "inspect_metadata_locks", "inspect_connections", "inspect_active_clients",
        "inspect_buffer_pool", "inspect_slow_queries", "inspect_current_queries",
        "inspect_table_io", "inspect_temp_tables", "inspect_global_status",
        "inspect_table_sizes", "inspect_schema_objects", "inspect_index_usage",
        "inspect_auto_increment", "inspect_variables", "inspect_replica_status",
    )
}


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
    parser = argparse.ArgumentParser(description="MySQL target verifier")
    parser.add_argument(
        "--mysql-endpoint", default="",
        help="真实 MySQL cluster/instance endpoint;传了才做 tools/call(endpoint 不进部署)",
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
                   "clientInfo": {"name": "verify-mysql", "version": "0"}},
    }), "initialize")
    print(f"✓ Gateway initialize OK (protocolVersion={init.get('protocolVersion')!r})")

    # 2) tools/list — 期望 18 个 rdsmysql___ tool(SHALL NOT #25:容忍 N-1)
    lst = _extract_result(_sigv4_post_mcp(gw_url, {
        "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {},
    }), "tools/list")
    tool_names = sorted(t["name"] for t in lst.get("tools", []))
    my_tools = {t for t in tool_names if t.startswith(_TARGET_PREFIX)}
    print(f"✓ tools/list OK — {len(my_tools)} MySQL tools (target 挂载成功)")
    assert len(my_tools) >= _EXPECTED_TOOL_COUNT - 1, (
        f"MySQL tool 数异常:期望 {_EXPECTED_TOOL_COUNT}(容忍 {_EXPECTED_TOOL_COUNT - 1}),"
        f"实际 {len(my_tools)}\n{sorted(my_tools)}"
    )
    missing_in_list = _EXPECTED_TOOLS - my_tools
    if missing_in_list:
        print(f"⚠ tools/list 漏了 {len(missing_in_list)} 个(SHALL NOT #25,仍可 tools/call):"
              f"{sorted(missing_in_list)}")

    # 3) 可选 tools/call(传 --mysql-endpoint 才做)
    if not args.mysql_endpoint:
        print("⊘ tools/call 跳过(未传 --mysql-endpoint)。endpoint 不进部署,"
              "真实巡检需调用方传 endpoint。")
    else:
        ep = args.mysql_endpoint
        print(f"→ tools/call {_PROBE_TOOL} (cluster_endpoint={ep})")
        res = _extract_result(_sigv4_post_mcp(gw_url, {
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": _PROBE_TOOL, "arguments": {"cluster_endpoint": ep}},
        }), "tools/call MySQL")
        if res.get("isError"):
            err = next((c.get("text", "") for c in res.get("content", [])
                        if c.get("type") == "text"), "")
            raise RuntimeError(
                f"MySQL tool {_PROBE_TOOL} 失败:{err!r}\n"
                "可能:mcp_devops_ro user 未配 / 密码未灌 / 网络不通 / endpoint 错误"
            )
        payload = json.loads(res["content"][0]["text"])
        _assert_a8(payload)
        n_threads = payload["raw_data"].get("active_thread_count")
        print(f"✓ MySQL tool OK (status={payload['status']}, active_threads={n_threads})")

    print("\n✅ MySQL target verification passed")
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
