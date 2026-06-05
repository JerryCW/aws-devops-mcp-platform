#!/usr/bin/env bash
# 第一步:部署 AgentCore Gateway(IAM 入站,无 target)。
#
# 这一步只创建 Gateway 本体,不挂任何 MCP server target。
# 第二步(按需注册 target)是独立部署单元。
#
# 前提:
#   - 当前 shell 配置好 AWS 凭据(`aws sts get-caller-identity` 能跑)
#   - Python 3.13 + Node 16+ 已装
#   - Gateway 第一步不 build 容器,不需要 Docker
#
# 用法:
#   bash scripts/deploy_gateway.sh                 # deploy + verify
#   bash scripts/deploy_gateway.sh --skip-verify   # 只 deploy
#
# 退出码:0 = 成功,非 0 = 失败

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

SKIP_VERIFY=0
for arg in "$@"; do
  case "$arg" in
    --skip-verify) SKIP_VERIFY=1 ;;
    -h|--help) grep '^# ' "$0" | head -20; exit 0 ;;
    *) echo >&2 "❌ unknown arg: $arg"; exit 2 ;;
  esac
done

if [[ -d "$ROOT_DIR/.venv/bin" ]]; then
  VENV_BIN="$ROOT_DIR/.venv/bin"
else
  echo "==> 创建 venv $ROOT_DIR/.venv"
  python3.13 -m venv "$ROOT_DIR/.venv"
  VENV_BIN="$ROOT_DIR/.venv/bin"
fi
cd "$ROOT_DIR"
export PATH="$VENV_BIN:$PATH"

CDK_VERSION="${CDK_VERSION:-2.1124.1}"
CDK_RUN=(npx --yes "cdk@${CDK_VERSION}")

echo "==> [0/4] 前置检查"
if ! aws sts get-caller-identity >/dev/null 2>&1; then
  echo >&2 "  ❌ AWS 凭据不可用(aws sts get-caller-identity 失败)"
  exit 1
fi
echo "  ✓ AWS 凭据可用"

echo
echo "==> [1/4] pip install -r requirements.txt"
"$VENV_BIN/pip" install -q -r requirements.txt
echo "✓ deps installed"

echo
echo "==> [2/4] AWS account / region"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
# region 跟随当前配置(AWS_REGION > AWS_DEFAULT_REGION > aws configure 的 region)
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-$(aws configure get region)}}"
if [[ -z "$REGION" ]]; then
  echo >&2 "  ❌ 无法确定 region。请设 AWS_REGION 或 aws configure 配置 region。"
  exit 1
fi
export CDK_DEFAULT_REGION="$REGION"
export CDK_DEFAULT_ACCOUNT="$ACCOUNT"
echo "    account: ${ACCOUNT}"
echo "    region:  ${REGION}"

echo
echo "==> [3/4] CDK bootstrap ${REGION}(幂等)"
"${CDK_RUN[@]}" bootstrap "aws://${ACCOUNT}/${REGION}" --require-approval never

echo
echo "==> [4/4] cdk deploy DevopsMcpGatewayStack"
"${CDK_RUN[@]}" deploy DevopsMcpGatewayStack --require-approval never

if [[ $SKIP_VERIFY -eq 0 ]]; then
  echo
  echo "==> 验证 Gateway SigV4 链路"
  "$VENV_BIN/python" "$SCRIPT_DIR/verify_gateway.py"
fi

echo
echo "✅ 第一步(Gateway)部署完成"
echo
echo "Gateway 已就绪,下一步:按需注册 MCP server target(第二步)。"
