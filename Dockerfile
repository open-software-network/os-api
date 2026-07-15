FROM python:3.11-slim@sha256:e031123e3d85762b141ad1cbc56452ba69c6e722ebf2f042cc0dc86c47c0d8b3 AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

LABEL "tee.launch_policy.allow_env_override"="APP_ORIGIN,CONFIDENTIAL_SPACE_IMPERSONATE_SERVICE_ACCOUNT,CONFIDENTIAL_SPACE_SECRET_ENV,CONFIDENTIAL_SPACE_SECRET_FILES,CONFIDENTIAL_SPACE_WIP_PROVIDER,ENABLE_MANAGED_KEYS,FIRESTORE_COLLECTION_PREFIX,FIRESTORE_DATABASE_ID,GATEWAY_ATTESTATION_AUDIENCE,GATEWAY_REGION,GOOGLE_CLOUD_PROJECT,MODEL_CONFIG_PATH,OS_ACCOUNTS_API_URL,OS_ACCOUNTS_URL,REDIS_CA_CERT_PATH,REQUIRE_CONFIDENTIAL_SPACE,SITE_BASIC_AUTH_USERNAME,VENICE_COMPAT_ENABLED" \
      "tee.launch_policy.log_redirect"="always"

RUN pip install --no-cache-dir uv==0.11.15 \
    && useradd --create-home --uid 10001 gateway
WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --locked --no-dev

COPY config ./config
ENV PATH="/app/.venv/bin:$PATH"

USER 0:10001
EXPOSE 8080

ENTRYPOINT ["python", "-m", "sovereign_gateway.container_entrypoint"]
CMD ["uvicorn", "sovereign_gateway.app:app", "--host", "0.0.0.0", "--port", "8080", "--timeout-keep-alive", "620", "--no-access-log", "--no-server-header", "--proxy-headers", "--forwarded-allow-ips=*"]
