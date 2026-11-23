# syntax=docker/dockerfile:1
FROM node:22-bookworm-slim
ARG CLAUDE_VERSION=stable
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git python3 python3-venv ripgrep \
    && rm -rf /var/lib/apt/lists/*
# Use the official native installer. Copy the actual executable out of its
# temporary installation home; user home stays data-only.
RUN curl --fail --show-error --silent --location https://claude.ai/install.sh -o /tmp/claude-install.sh \
    && mkdir -p /tmp/claude-install \
    && HOME=/tmp/claude-install bash /tmp/claude-install.sh "${CLAUDE_VERSION}" \
    && cp -L /tmp/claude-install/.local/bin/claude /usr/local/bin/claude \
    && chmod 0755 /usr/local/bin/claude \
    && /usr/local/bin/claude --version \
    && rm -rf /tmp/claude-install /tmp/claude-install.sh
RUN usermod -l claude -d /home/claude node \
    && groupmod -n claude node \
    && mkdir -p /workspace /home/claude /opt/claude-cloud \
    && chown 1000:1000 /workspace /home/claude \
    && chmod 0700 /workspace /home/claude
COPY backend/file_helper.py /opt/claude-cloud/file_helper.py
ENV HOME=/home/claude \
    TERM=xterm-256color \
    LANG=C.UTF-8 \
    DISABLE_AUTOUPDATER=1 \
    DISABLE_UPDATES=1 \
    HTTPS_PROXY=http://egress:3128 \
    HTTP_PROXY=http://egress:3128 \
    https_proxy=http://egress:3128 \
    http_proxy=http://egress:3128
USER 1000:1000
WORKDIR /workspace
CMD ["sleep", "infinity"]
