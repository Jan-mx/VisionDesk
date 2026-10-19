# 基础镜像与 PyPI 源可被 build-arg 覆盖（国内构建走镜像站），默认值与原行为一致
ARG NODE_IMAGE=node:22-alpine
ARG PYTHON_IMAGE=python:3.13-slim
ARG UV_IMAGE=ghcr.io/astral-sh/uv:latest

# 前端构建产物由后端同源托管，因此在同一镜像内完成构建
FROM ${NODE_IMAGE} AS frontend
WORKDIR /build
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE}
COPY --from=uv /uv /usr/local/bin/uv

RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-wqy-microhei \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app/backend
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONPATH=/app/backend

COPY backend/pyproject.toml backend/uv.lock ./
# 国内离线构建：先用 pip 把全部轮子下载到 .wheels/，再以 UV_SYNC_FLAGS="--no-index --find-links /tmp/wheels" 构建
COPY .wheels/ /tmp/wheels/
# 传入 UV_DEFAULT_INDEX（如腾讯源）时 uv 会把 lock 中的 pypi.org 映射到该镜像
ARG UV_DEFAULT_INDEX
ARG UV_SYNC_FLAGS=
RUN uv sync --frozen --all-extras --no-dev ${UV_SYNC_FLAGS}

COPY backend/ ./
COPY --from=frontend /build/dist /app/frontend/dist

ENV PATH="/app/backend/.venv/bin:$PATH"
EXPOSE 7302
