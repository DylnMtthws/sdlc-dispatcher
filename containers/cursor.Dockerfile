# Extend a trusted project development image; runtime has no installer network access.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
USER root
ARG CURSOR_SHA256
COPY agent-cli-package.tar.gz /tmp/cursor-package.tar.gz
RUN test -n "$CURSOR_SHA256" \
    && echo "$CURSOR_SHA256  /tmp/cursor-package.tar.gz" | sha256sum -c - \
    && mkdir -p /opt/cursor-agent \
    && tar --strip-components=1 -xzf /tmp/cursor-package.tar.gz -C /opt/cursor-agent \
    && rm /tmp/cursor-package.tar.gz \
    && ln -s /opt/cursor-agent/cursor-agent /usr/local/bin/agent \
    && ln -s /opt/cursor-agent/cursor-agent /usr/local/bin/cursor-agent
ENV AGENT_CLI_CREDENTIAL_STORE=file
USER 65532:65532
WORKDIR /workspace
CMD ["agent", "--version"]
