# syntax=docker/dockerfile:1.7
# =============================================================================
# Polybot shared service base image.
#
# Multi-arch aware: BUILDPLATFORM is the platform of the build host, and
# TARGETPLATFORM is the platform the image is being built for. Declaring the
# ARGs (even unused) lets `docker buildx build --platform linux/amd64,linux/arm64`
# resolve correctly without warnings.
#
#   docker buildx build \
#       --platform linux/amd64,linux/arm64 \
#       -f services/_base/Dockerfile -t polybot/base .
# =============================================================================
ARG BUILDPLATFORM
ARG TARGETPLATFORM

FROM --platform=${TARGETPLATFORM:-linux/amd64} python:3.12.7-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app:/app/packages

WORKDIR /app

# System deps — build-essential & libpq-dev are needed for psycopg / scientific
# wheels that may compile from source on arm64.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libpq-dev curl && \
    rm -rf /var/lib/apt/lists/*

# Create a non-root user up-front so we can chown /app to it.
RUN groupadd --system --gid 1000 app && \
    useradd  --system --uid 1000 --gid app --home-dir /app --shell /sbin/nologin app

COPY pyproject.toml ./
# install just the runtime deps — services share one image.
# --root-user-action=ignore silences the pip warning emitted when installing
# system-wide as root during the build stage (we drop to USER app afterward).
RUN pip install --upgrade --root-user-action=ignore pip && \
    pip install --root-user-action=ignore \
      "httpx>=0.27" "websockets>=13" "tenacity>=9" \
      "sqlalchemy>=2.0" "psycopg[binary]>=3.2" "alembic>=1.13" \
      "redis>=5.0" "pandas>=2.2" "numpy>=2.0" "scipy>=1.13" "scikit-learn>=1.5" \
      "fastapi>=0.115" "uvicorn[standard]>=0.32" \
      "pydantic>=2.9" "pydantic-settings>=2.5" \
      "celery[redis]>=5.4" "web3>=7.0" "eth-account>=0.13" \
      "structlog>=24.4" "sentry-sdk[fastapi]>=2.0" "pyyaml>=6.0" "click>=8.1" \
      "py-clob-client-v2>=0.1.0" "polymarket-apis>=0.5.7"

COPY packages /app/packages
COPY services /app/services
COPY scripts  /app/scripts
COPY config   /app/config
COPY data     /app/data

# Hand /app to the unprivileged user. Dev compose files bind-mount source over
# these paths; the mount inherits host ownership, so this chown only affects
# the prod (immutable) image — it doesn't break the dev volume mounts.
RUN chown -R app:app /app

USER app

# default command is overridden in docker-compose per-service
CMD ["python", "-c", "print('set a command in docker-compose')"]
