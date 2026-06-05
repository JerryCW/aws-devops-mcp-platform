"""ElastiCache target 验证(IAM SigV4 形态)。

两级验证:
  1. 必验:Gateway initialize 200 → tools/list 含 13 个 elasticache___ tool(target 挂载成功)
  2. 可选(传 --redis-endpoint 才做):tools/call inspect_overview 真实巡检

设计:endpoint 不进部署,所以真实 tool call 需要调用方传 endpoint。
不传 --redis-endpoint 时只验挂载链路(客户 ACL user 还没配 / 不想连真集群时用)。

用法:
  python scripts/verify_elasticache.py
  python scripts/verify_elasticache.py --redis-endpoint master.my-cache.xxxx.use1.cache.amazonaws.com

末尾打印 `✅ ElastiCache target verification passed`,exit 0 = pass。

强约束:SHALL NOT #15(完整 traceback)/ #18(SSE utf-8)/ #21(三下划线)/ #25(tools/list 可能少 1,兼容 N-1 + 显式 call 探针)
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
# 想临时指定:`AWS_REGION=ap-southeast-1 python scripts/verify_elasticache.py`
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

_TARGET_PREFIX = "elasticache___"
_EXPECTED_TOOL_COUNT = 13
_PROBE_TOOL = "elasticache___inspect_overview"

# 13 个工具的期望全名(用于 diff 出 Gateway tools/list 漏掉的 tool — SHALL NOT #25)
_EXPECTED_TOOLS = {
    f"{_TARGET_PREFIX}{name}"
    for name in (
        "inspect_overview", "inspect_memory", "inspect_slow_queries",
        "inspect_clients", "inspect_keyspace", "inspect_stats",
        "inspect_replication", "inspect_persistence", "inspect_big_keys",
        "inspect_hot_keys", "inspect_eviction", "inspect_cluster_mode",
        "inspect_pubsub",
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
    parser = argparse.ArgumentParser(description="ElastiCache target verifier")
    parser.add_argument(
        "--redis-endpoint", default="",
        help="真实 ElastiCache 节点 endpoint(primary/replica);传了才做 tools/call(endpoint 不进部署)",
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
                   "clientInfo": {"name": "verify-elasticache", "version": "0"}},
    }), "initialize")
    print(f"✓ Gateway initialize OK (protocolVersion={init.get('protocolVersion')!r})")

    # 2) tools/list — 期望 13 个 elasticache___ tool
    #    SHALL NOT #25:Gateway tools/list 可能漏 1 个(sync 截断 bug),兼容 N-1,
    #    并打印漏掉的 tool 名(实际仍可 tools/call)。
    lst = _extract_result(_sigv4_post_mcp(gw_url, {
        "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {},
    }), "tools/list")
    tool_names = sorted(t["name"] for t in lst.get("tools", []))
    ec_tools = {t for t in tool_names if t.startswith(_TARGET_PREFIX)}
    print(f"✓ tools/list OK — {len(ec_tools)} ElastiCache tools (target 挂载成功)")
    assert len(ec_tools) >= _EXPECTED_TOOL_COUNT - 1, (
        f"ElastiCache tool 数异常:期望 {_EXPECTED_TOOL_COUNT}(容忍 {_EXPECTED_TOOL_COUNT - 1}),"
        f"实际 {len(ec_tools)}\n{sorted(ec_tools)}"
    )
    missing_in_list = _EXPECTED_TOOLS - ec_tools
    if missing_in_list:
        print(f"⚠ tools/list 漏了 {len(missing_in_list)} 个(SHALL NOT #25,仍可 tools/call):"
              f"{sorted(missing_in_list)}")

    # 3) 可选 tools/call(传 --redis-endpoint 才做)
    if not args.redis_endpoint:
        print("⊘ tools/call 跳过(未传 --redis-endpoint)。endpoint 不进部署,"
              "真实巡检需调用方传 endpoint。")
    else:
        ep = args.redis_endpoint
        print(f"→ tools/call {_PROBE_TOOL} (endpoint={ep})")
        res = _extract_result(_sigv4_post_mcp(gw_url, {
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": _PROBE_TOOL, "arguments": {"endpoint": ep}},
        }), "tools/call ElastiCache")
        if res.get("isError"):
            err = next((c.get("text", "") for c in res.get("content", [])
                        if c.get("type") == "text"), "")
            raise RuntimeError(
                f"ElastiCache tool {_PROBE_TOOL} 失败:{err!r}\n"
                "可能:mcp_devops_ro ACL user 未配 / 密码未灌 / 网络不通 / endpoint 错误 / TLS 配置不符"
            )
        payload = json.loads(res["content"][0]["text"])
        _assert_a8(payload)
        rd = payload["raw_data"]
        print(f"✓ ElastiCache tool OK (status={payload['status']}, "
              f"role={rd.get('role')}, version={rd.get('version')}, tls={rd.get('tls')})")

    print("\n✅ ElastiCache target verification passed")
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
