# 国内网络构建示例（基础镜像与 pip 都可换源）：
#   docker pull docker.m.daocloud.io/library/python:3.12-slim
#   docker tag docker.m.daocloud.io/library/python:3.12-slim python:3.12-slim
#   docker compose --env-file .env.docker build \
#     --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple admin
ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}

WORKDIR /app

ARG PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_EXTRA_ARGS=""

COPY requirements.txt .
RUN pip install --no-cache-dir -i ${PIP_INDEX_URL} ${PIP_EXTRA_ARGS} -r requirements.txt

COPY converter.py responses_adapter.py responses_projection.py anthropic_adapter.py desensitize.py upstream_compat.py repository_instructions.py model_metadata.py wb_install.py wb_asar.py wb_envcheck.py turing_helper.js ./
COPY admin ./admin
COPY desktop/__init__.py desktop/common.py desktop/runtime.py desktop/updates.py desktop/version.json ./desktop/
COPY desktop/assets ./desktop/assets

EXPOSE 8790 8787

# 入口统一走单 worker 的 managed runner（admin/managed_worker.py）。
# 为什么不再直接 uvicorn：计划升级/重启要先**封口**（拒绝新业务）、等已受理的
# 请求与后台任务归零，再让 Uvicorn 正常关闭；直接跑 uvicorn 的默认
# graceful shutdown 秒表会在长流没跑完时强断。
# --control-dir：容器内的私有控制目录，供同容器内的升级 CLI / 平台脚本访问 IPC
# （放在容器本地临时目录，不挂卷、不进镜像层；状态报告 can_restart=false，
#  由平台执行进程替换）。
CMD ["python3", "-m", "admin.managed_worker", \
     "--host", "0.0.0.0", "--port", "8790", \
     "--proxy-headers", "--control-dir", "/tmp/wb2api-lifecycle"]
