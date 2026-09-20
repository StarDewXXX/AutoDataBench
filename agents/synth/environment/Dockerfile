# Researcher-phase agent environment for AutoDataBench v2.
#
# Deliberately minimal. Everything heavy is bind-mounted from the host at run
# time by harness/run_synth.py (same-path mounts), NOT baked in:
#   - the docker CLI binary            -> /usr/bin/docker
#   - the compose + buildx plugins     -> /usr/local/lib/docker/cli-plugins
#   - the host docker socket           -> /var/run/docker.sock   (DooD)
#   - the conda `agent` env (harbor)   -> same path, read-only
#   - the ONE original task            -> same path, read-only
#   - its customer rollout bundle      -> same path, read-only
#   - the host-aligned work dir        -> same path, read-write
#
# The container only needs a working shell, git, curl (claude-code self-installs
# node/npm and the CLI at run time), flock (util-linux, for the docker-slot
# semaphore), and the AutoDataBench helper scripts.
FROM python:3.12-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
        bash git curl ca-certificates procps coreutils util-linux jq \
    && rm -rf /var/lib/apt/lists/*

# AutoDataBench helper tools the agent is expected to use. Kept ahead of
# everything else on PATH so `harbor` resolves to our model-guard shim, not the
# real one.
# codex reads its config from $CODEX_HOME, which harbor fixes at /tmp/codex-home.
# harbor creates that directory and APPENDS to config.toml, so pre-placing the
# file here survives. World-writable because harbor appends as the agent user.
# See codex-config.toml for why this is needed at all.
COPY codex-config.toml /tmp/codex-home/config.toml
RUN chmod 777 /tmp/codex-home && chmod 666 /tmp/codex-home/config.toml

COPY bin/ /usr/local/bin/
RUN chmod +x /usr/local/bin/time_left \
             /usr/local/bin/adb-oracle \
             /usr/local/bin/adb-testrun \
             /usr/local/bin/adb-search \
             /usr/local/bin/harbor \
             /usr/local/bin/docker

WORKDIR /app
