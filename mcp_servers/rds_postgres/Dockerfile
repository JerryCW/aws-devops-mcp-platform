# RDS PostgreSQL 巡检 MCP server 容器。
#
# 平台硬约束:
#   - linux/arm64 单平台镜像(SHALL NOT #7,AgentCore Runtime 硬约束)
#   - python:3.13-slim 基础镜像
#   - psycopg[binary] 自带预编译 wheel,arm64 / linux 装包 < 5 秒,无 C 扩展编译

FROM --platform=linux/arm64 public.ecr.aws/docker/library/python:3.13-slim

WORKDIR /app

# 先复制 requirements,最大化 layer 缓存命中率(后续只改 main.py 不需要重装依赖)
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY main.py /app/main.py

# AgentCore Runtime MCP 协议契约硬编码:port=8000,mount=/mcp。
# 不是 8080(HTTP 协议端口)— 详见 .kiro/steering/shall-not.md #17。
EXPOSE 8000

# 数组形式 exec,SIGTERM 直达 PID=1 Python 进程,不经 shell 中转,
# 容器停止能优雅关闭 FastMCP 与 psycopg 连接
ENTRYPOINT ["python", "main.py"]
