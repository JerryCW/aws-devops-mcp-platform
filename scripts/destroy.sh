#!/usr/bin/env bash
# destroy。
#
# destroy 顺序:先 destroy 所有 target stack(DevopsMcp-<server>),
# 再 destroy Gateway stack(DevopsMcpGatewayStack)。
#
# 注意:
#   - AgentCore Runtime delete 偶发慢(VPC ENI 异步释放),SG 可能卡几分钟到十几分钟,
#     甚至 DELETE_FAILED;等 ENI 释放后重跑即可
#   - Secret 是 RemovalPolicy.DESTROY,会随 target stack 一起删
#
# 用法:
#   bash scripts/destroy.sh                          # 只 destroy Gateway
#   bash scripts/destroy.sh DevopsMcp-rdspostgres    # 指定 target stack(可多个),最后再 Gateway

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

CDK_VERSION="${CDK_VERSION:-2.1124.1}"
CDK_RUN=(npx --yes "cdk@${CDK_VERSION}")

if [[ -d "$ROOT_DIR/.venv/bin" ]]; then
  export PATH="$ROOT_DIR/.venv/bin:$PATH"
fi

# 先 destroy 传入的 target stack(若有)
if [[ $# -gt 0 ]]; then
  echo "==> destroy target stacks: $*"
  "${CDK_RUN[@]}" destroy --force "$@"
fi

echo "==> destroy DevopsMcpGatewayStack"
"${CDK_RUN[@]}" destroy --force DevopsMcpGatewayStack

echo
echo "✅ destroy 完成"
