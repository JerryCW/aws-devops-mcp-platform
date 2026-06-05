# OpenSearch 巡检 MCP server 容器(linux/arm64,SHALL NOT #7)。
# 迁移自 opensearch-project/opensearch-mcp-server-py(Apache-2.0)。
#
# 该 server 原生支持 streamable-http(--transport stream)+ /mcp mount + stateless,
# 与 AgentCore Runtime MCP 契约吻合。仅需:监听 0.0.0.0:8000(契约硬要求,SHALL NOT #17)。

FROM --platform=linux/arm64 public.ecr.aws/docker/library/python:3.13-slim

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# 三个包平铺到 /app(upstream 用绝对 import:tools. / opensearch. / mcp_server_opensearch.)
COPY mcp_server_opensearch /app/mcp_server_opensearch
COPY opensearch /app/opensearch
COPY tools /app/tools
COPY entrypoint.py /app/entrypoint.py

# AgentCore Runtime MCP 协议契约硬编码:port=8000,mount=/mcp(不是 8080,SHALL NOT #17)
EXPOSE 8000

# 引导层:先从 DB_SECRET_NAME 拉 basic auth 凭据注入 env(SHALL NOT #12),
# 再启动 upstream streamable-http server(监听 0.0.0.0:8000,single 模式)。
ENTRYPOINT ["python", "entrypoint.py"]
