FROM python:3.12-slim@sha256:423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf

LABEL org.opencontainers.image.source="https://github.com/open-software-network/os-api" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY deploy/attestation-agent/agent.py /agent.py
COPY AUTHORS.md LICENSE NOTICE THIRD_PARTY_NOTICES.md /licenses/

# The agent must run privileged as root to reach /sys/kernel/config/tsm; the
# DaemonSet manifest, not this image, is where that is declared. configfs is
# not mounted by default in the container namespace, so mount it first.
ENTRYPOINT ["sh", "-c", "mountpoint -q /sys/kernel/config || mount -t configfs configfs /sys/kernel/config; exec python3 /agent.py"]
