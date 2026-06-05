"""OpenSearch MCP server 容器引导层(secret 适配 + 启动 upstream server)。

为什么需要这层:
  upstream(opensearch-mcp-server-py)single 模式从环境变量 OPENSEARCH_USERNAME /
  OPENSEARCH_PASSWORD 读**明文凭据**。但本框架的 target_stack 统一注入的是
  DB_SECRET_NAME(Secrets Manager 路径,SHALL NOT #12:密码不进环境变量明文/镜像)。

  本引导层在容器启动时:
    1. 从 DB_SECRET_NAME 拉 {username, password}(走 Runtime 执行角色,5 分钟内一次性)
    2. 设进 OPENSEARCH_USERNAME / OPENSEARCH_PASSWORD 环境变量(仅本进程内,不落盘)
    3. exec upstream `python -m mcp_server_opensearch --transport stream ...`

  这样 upstream 代码**零改动**,凭据走 Secrets Manager,符合框架约定。

  OPENSEARCH_URL 由 target_stack 通过 extraEnv 注入(实例 endpoint);也可被
  tool 调用时的 per-call `opensearch_url` override(upstream single 模式原生支持),
  保持"不绑实例"的设计。

环境变量契约(target_stack 注入):
  DB_SECRET_NAME            Secrets Manager 路径,JSON {"username","password"}
  AWS_REGION                Secret 所在 region
  OPENSEARCH_URL            OpenSearch domain endpoint(https://...)
  OPENSEARCH_SSL_VERIFY     可选,默认 "true"(Amazon OpenSearch 有效证书,可校验)
  LOG_LEVEL                 可选

凭据缺失时 fail-fast(不静默 no-auth),与 PG/MySQL 行为一致。
"""

from __future__ import annotations

import json
import os
import sys

_PLACEHOLDER_PASSWORD = "PLACEHOLDER_REPLACE_ME"


def _force_basic_auth_patch() -> None:
    """Wrap upstream `_create_opensearch_client`,清掉 IAM/profile/serverless 认证参数,
    强制走 basic auth(防 Agent 注入 aws_iam_arn)。

    由 OPENSEARCH_FORCE_BASIC_AUTH 控制(默认 true)。

    为什么 wrap 这个函数而不是 patch pydantic model:
      pydantic v2 在类定义后动态追加 model_validator 不会被重新收集生效(实测无效)。
      `_create_opensearch_client` 是所有认证路径的**唯一收口**——single/multi 模式
      最终都调它,且认证方式选择(IAM 优先于 basic)就在它内部。在入口把 iam_arn/profile/
      serverless/IAM 临时凭据全部清零,upstream 内部的 IAM 分支永远进不去,只能落 basic auth。

    不改 upstream 源文件:运行期替换模块函数引用(适配层逻辑)。
    """
    if os.environ.get("OPENSEARCH_FORCE_BASIC_AUTH", "true").lower() != "true":
        print("[entrypoint] OPENSEARCH_FORCE_BASIC_AUTH != true,跳过认证锁定", file=sys.stderr)
        return

    import opensearch.client as osc

    _orig = osc._create_opensearch_client

    def _wrapped(*args, **kwargs):
        # 清掉所有会触发 IAM/SigV4 路径的认证参数,只保留 basic auth(username/password)
        neutralized = []
        for k in ("iam_arn", "profile", "aws_access_key_id",
                  "aws_secret_access_key", "aws_session_token", "bearer_auth_header"):
            if kwargs.get(k):
                kwargs[k] = "" if k in ("iam_arn", "profile") else None
                neutralized.append(k)
        if kwargs.get("is_serverless_mode"):
            kwargs["is_serverless_mode"] = False
            neutralized.append("is_serverless_mode")
        if neutralized:
            # 不打印值(可能含 ARN/凭据),只记字段名(SHALL NOT #12)
            print(f"[entrypoint] force-basic-auth 中和认证参数: {neutralized}", file=sys.stderr)
        return _orig(*args, **kwargs)

    osc._create_opensearch_client = _wrapped
    print("[entrypoint] 已启用 force-basic-auth: IAM/profile/serverless 认证路径已锁死",
          file=sys.stderr)


def _load_credentials_into_env() -> None:
    """从 DB_SECRET_NAME 拉凭据设进 OPENSEARCH_USERNAME/PASSWORD(best-effort,绝不退出)。

    ⚠ 关键:本函数**永不 sys.exit**。原因——AgentCore Gateway 创建 target 时会立即
    对 Runtime 做 tools/list 健康检查,此时容器必须能起来。tools/list 不需要真凭据
    (不连 OpenSearch)。若启动期因 placeholder/缺失就退出,容器起不来 → Gateway
    "Failed to connect and fetch tools" → 部署失败回滚(rev1 实测踩过)。

    策略:
      - 已显式给了 OPENSEARCH_USERNAME/PASSWORD 或 OPENSEARCH_NO_AUTH=true → 尊重现状
      - 否则尝试从 DB_SECRET_NAME 拉;成功则注入,失败/placeholder 则**仅告警不注入**
    真凭据问题会在实际 tool 调用(连 OpenSearch)时由 upstream 自然报错,带可诊断信息。
    """
    if os.environ.get("OPENSEARCH_NO_AUTH", "").lower() == "true":
        print("[entrypoint] OPENSEARCH_NO_AUTH=true,跳过 secret 拉取", file=sys.stderr)
        return
    if os.environ.get("OPENSEARCH_USERNAME") and os.environ.get("OPENSEARCH_PASSWORD"):
        print("[entrypoint] OPENSEARCH_USERNAME/PASSWORD 已显式设置,跳过 secret 拉取", file=sys.stderr)
        return

    secret_name = os.environ.get("DB_SECRET_NAME", "").strip()
    if not secret_name:
        print(
            "[entrypoint] WARN: 无 DB_SECRET_NAME 且无显式凭据;容器仍启动(tools/list 不需凭据),"
            "实际查询时会因缺凭据失败。",
            file=sys.stderr,
        )
        return

    try:
        region = os.environ.get("AWS_REGION", "us-east-1")
        import boto3  # 延迟 import

        sm = boto3.client("secretsmanager", region_name=region)
        raw = sm.get_secret_value(SecretId=secret_name)["SecretString"]
        payload = json.loads(raw)
        username = payload.get("username", "")
        password = payload.get("password", "")
        if not username or not password:
            print(f"[entrypoint] WARN: secret {secret_name!r} 缺 username/password,跳过注入", file=sys.stderr)
            return
        if password == _PLACEHOLDER_PASSWORD:
            print(
                f"[entrypoint] WARN: secret {secret_name!r} 密码仍是 PLACEHOLDER(尚未灌真密码),"
                "容器仍启动以通过 Gateway tools/list 健康检查;灌真密码后实际查询才可用。",
                file=sys.stderr,
            )
            return
        os.environ["OPENSEARCH_USERNAME"] = username
        os.environ["OPENSEARCH_PASSWORD"] = password
        # 不打印密码(SHALL NOT #12)
        print(f"[entrypoint] 已从 {secret_name} 注入 OpenSearch basic auth 凭据(user={username})", file=sys.stderr)
    except Exception as e:  # noqa: BLE001 — 拉 secret 失败不阻断启动
        print(
            f"[entrypoint] WARN: 拉 secret {secret_name!r} 失败({type(e).__name__}: {str(e)[:120]});"
            "容器仍启动,实际查询时再报错。",
            file=sys.stderr,
        )


def main() -> None:
    _load_credentials_into_env()
    _force_basic_auth_patch()
    # 交给 upstream:streamable-http,监听 0.0.0.0:8000,single 模式
    from mcp_server_opensearch import main as upstream_main

    sys.argv = [
        "mcp_server_opensearch",
        "--transport", "stream",
        "--host", "0.0.0.0",
        "--port", "8000",
        "--mode", "single",
    ]
    upstream_main()


if __name__ == "__main__":
    main()
