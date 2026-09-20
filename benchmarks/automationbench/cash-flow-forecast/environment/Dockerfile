FROM automationbench-harbor-base:2

# The seed lands in a root-owned 0700 directory. The world service loads it into
# memory at container start and then unlinks it, so once the agent is exec'd there is
# nothing on disk describing a part of the workspace it has no tool to read. The agent
# runs as the unprivileged `agent` user ([agent] user in task.toml), so it can neither
# read what is left nor inspect the service that holds it.
COPY task /srv/world/
RUN chmod 0600 /srv/world/*.json

WORKDIR /app

# Build-time gate, replacing the `abctl reset && abctl list` the CLI-interface image
# used: a malformed seed, a tool name that does not exist, or a schema that fails to
# generate breaks the build instead of surfacing as a dead service or -- worse -- a
# silently smaller tool set at run time.
RUN abctl-mcp --check

# ENTRYPOINT, not CMD: harbor's compose template overrides `command` but not
# ENTRYPOINT, so this is what gets a process running before the agent arrives.
ENTRYPOINT ["/usr/local/bin/ab-init"]

# The agent must not be able to read the grader: see /usr/local/bin/ab-harden.
RUN ab-harden
