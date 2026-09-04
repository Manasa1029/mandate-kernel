# syntax=docker/dockerfile:1
# Two stages: wheels are built once, the runtime image carries no compiler.
# PyNaCl needs libsodium at build time, which is the only reason the split pays.

FROM python:3.12-slim AS build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libsodium-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /wheels
COPY requirements.txt .
RUN pip wheel -r requirements.txt -w /wheels


FROM python:3.12-slim AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    KERNEL_DB_PATH=/data/kernel.db \
    RAZORPAY_MODE=mock

RUN apt-get update && apt-get install -y --no-install-recommends libsodium23 curl \
    && rm -rf /var/lib/apt/lists/*

# Non-root. The process holds payment credentials; it has no business being root.
RUN useradd --create-home --uid 10001 kernel
WORKDIR /app

COPY --from=build /wheels /wheels
COPY requirements.txt .
RUN pip install --no-index --find-links=/wheels -r requirements.txt && rm -rf /wheels

COPY --chown=kernel:kernel . .

# The ledger lives on a volume. Losing it means losing the audit trail, which is
# the one thing this service exists to produce.
RUN mkdir -p /data && chown kernel:kernel /data
VOLUME ["/data"]

USER kernel
EXPOSE 8000

# The health check verifies the hash chain, not just liveness: a process that is
# up but whose ledger no longer verifies is not healthy.
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/healthz | grep -q '"ledger_intact":true'

CMD ["python", "-m", "uvicorn", "kernel.api:app", "--host", "0.0.0.0", "--port", "8000"]
