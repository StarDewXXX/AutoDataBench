# Environment for the failure-mode analyst agent (claude-code + our chosen model).
#
# Minimal, like the synthesis environment: the docker CLI, compose/buildx
# plugins, the host docker socket (DooD), and the conda `agent` env (harbor)
# are all bind-mounted at run time by the v2 harness driver at their host
# paths, not baked in. claude-code self-installs node/npm and the CLI at run
# time. It only needs a shell, git, curl, jq, flock, and time_left.
FROM python:3.12-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
        bash git curl ca-certificates procps coreutils util-linux jq \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# The one helper the agent is expected to use: how much budget is left.
COPY bin/ /usr/local/bin/
RUN chmod +x /usr/local/bin/time_left
