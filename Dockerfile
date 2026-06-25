# syntax=docker/dockerfile:1.6
# =====================================================================
# Semantic Video Replication Workflow
# 多阶段构建：builder 编译依赖，runtime 仅保留运行时必需
# =====================================================================

FROM python:3.10-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

# 编译依赖（pillow / numpy / oss2 / pydantic-core 等需要 gcc）
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libffi-dev \
        libssl-dev \
        libjpeg-dev \
        zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --prefix=/install --no-warn-script-location -r requirements.txt

# =====================================================================
FROM python:3.10-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PIP_NO_CACHE_DIR=1

# 运行时系统依赖：FFmpeg（必需）+ libGL（matplotlib 渲染）+ 字体 fallback
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        libgl1 \
        libglib2.0-0 \
        libjpeg62-turbo \
        zlib1g \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

# 拷贝已编译的 Python 依赖
COPY --from=builder /install /usr/local

WORKDIR /app

# 拷贝应用代码（.dockerignore 已剔除 .env / tmp / __pycache__ 等）
COPY . /app

# 创建运行时目录
RUN mkdir -p /app/tmp /app/static/frames /app/scripts/output \
    && chmod -R 755 /app/tmp /app/static /app/scripts/output

# 非 root 用户运行
RUN useradd -u 1000 -m appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# 就绪检查：同时验证配置、FFmpeg 和运行目录
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://localhost:8000/ready > /dev/null || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
