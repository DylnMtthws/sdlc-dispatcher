# Build with a context exported from the reviewed Deck Lab commit.
# No local .env, database, backups, worktrees, or production credentials enter it.
FROM node:22-bookworm-slim AS codex
ARG CODEX_VERSION=0.153.1
RUN npm install --global @openai/codex@${CODEX_VERSION}

FROM python:3.11-slim-bookworm
COPY --from=codex /usr/local/bin/node /usr/local/bin/node
COPY --from=codex /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/@openai/codex/bin/codex.js /usr/local/bin/codex \
    && apt-get update && apt-get install --no-install-recommends --yes git ca-certificates media-types \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/deck-lab
COPY pyproject.toml ./
COPY src ./src
# Match the existing CI extras. Install CPU PyTorch first to avoid CUDA downloads
# on x86 workers; no model weights are downloaded or used by the default tests.
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir ".[dev,postgres,legacy]"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/workspace/src
WORKDIR /workspace
USER 65532:65532
CMD ["python", "--version"]
