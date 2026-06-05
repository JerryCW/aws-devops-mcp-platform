# RDS / Aurora MySQL 巡检 MCP server 容器(linux/arm64,SHALL NOT #7)。

FROM --platform=linux/arm64 public.ecr.aws/docker/library/python:3.13-slim

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY main.py /app/main.py

# AgentCore Runtime MCP 协议契约硬编码:port=8000,mount=/mcp(不是 8080,SHALL NOT #17)
EXPOSE 8000

ENTRYPOINT ["python", "main.py"]
