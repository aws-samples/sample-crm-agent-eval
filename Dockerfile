# SPDX-License-Identifier: MIT
#
# AgentCore Runtime container image for the Strands supervisor (src/agents/runtime_server.py).
# Built by the CodeBuild project in infra/modules/agentcore/ecr_codebuild.tf from agent-source.zip.
# linux/arm64, as the AgentCore Runtime container contract requires. Dependencies are installed
# from requirements.txt verbatim, dev pins included. Runs as a non-root user.

# ECR Public mirror of the Docker Official Image. Tag must match .python-version; pip must match
# .gitlab-ci.yml's PIP_VERSION (tests/unit/test_dockerfile_pins.py).
FROM --platform=linux/arm64 public.ecr.aws/docker/library/python:3.13.18-slim AS base

# Fixed UID/GID for the non-root runtime user.
ARG APP_UID=10001
ARG APP_GID=10001

RUN groupadd --gid "${APP_GID}" app \
    && useradd --uid "${APP_UID}" --gid "${APP_GID}" --create-home --shell /usr/sbin/nologin app

WORKDIR /app

# Dependency layer, cached across source-only changes.
COPY requirements.txt ./
RUN pip install --no-cache-dir --no-input pip==26.2.2 \
    && pip install --no-cache-dir --no-input --no-deps -r requirements.txt

# src/providers/capabilities.py reads config/ relative to /app.
COPY pyproject.toml ./
COPY config/ ./config/
COPY src/ ./src/

RUN chown -R app:app /app
USER app

ENV PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1

# AgentCore Runtime container contract: host 0.0.0.0, port 8080.
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/ping', timeout=3)" || exit 1

CMD ["python", "-m", "uvicorn", "agents.runtime_server:app", "--host", "0.0.0.0", "--port", "8080"]
