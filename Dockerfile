FROM python:3.11-slim@sha256:e031123e3d85762b141ad1cbc56452ba69c6e722ebf2f042cc0dc86c47c0d8b3 AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN pip install --no-cache-dir uv==0.11.15 \
    && useradd --create-home --uid 10001 gateway
WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --locked --no-dev

COPY config ./config
ENV PATH="/app/.venv/bin:$PATH"

USER 10001:10001
EXPOSE 8080

CMD ["uvicorn", "sovereign_gateway.app:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log", "--no-server-header", "--proxy-headers", "--forwarded-allow-ips=*"]
