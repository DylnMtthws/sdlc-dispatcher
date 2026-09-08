# Operator-reviewed base image. Build once, resolve its immutable ID for every job.
FROM node:22-bookworm-slim
ARG CODEX_VERSION=0.153.1
RUN npm install --global @openai/codex@${CODEX_VERSION} \
    && apt-get update && apt-get install --no-install-recommends --yes python3 git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /workspace
USER 65532:65532
CMD ["codex", "--version"]
