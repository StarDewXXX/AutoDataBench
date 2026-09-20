#!/usr/bin/env python3
"""Bind mounts for our own agent tasks (researcher / analyst / judge).

Two rules run through all of this.

Same-path mounting. A host path is mounted at the identical path inside the
container. It is not cosmetic: these agents drive docker on the HOST daemon
(docker-out-of-docker), so when one asks the daemon to bind a directory into a
grandchild container, the daemon resolves that path on the host. A path that
exists only under some container-private prefix resolves to nothing, and the
grandchild silently gets an empty directory.

Read-only by default. Everything an agent should only look at is mounted
read-only, so a researcher cannot edit the original task it was shown and a judge
cannot touch the task it is grading. Exactly one directory is writable: the work
directory the researcher builds in.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path


def _docker_bits() -> tuple[str, str, str]:
    """(socket, docker binary, cli-plugins dir) on this host."""
    sock = os.environ.get("DOCKER_SOCK") or f"/run/user/{os.getuid()}/docker.sock"
    if not Path(sock).exists():
        for c in (f"/run/user/{os.getuid()}/docker.sock", "/var/run/docker.sock"):
            if Path(c).exists():
                sock = c
                break
    dbin = shutil.which("docker") or "/usr/bin/docker"
    plugins = os.environ.get("DOCKER_CLI_PLUGINS") or "/usr/local/lib/docker/cli-plugins"
    for c in (plugins, "/usr/libexec/docker/cli-plugins", str(Path.home() / ".docker/cli-plugins")):
        if Path(c).is_dir():
            plugins = c
            break
    return sock, dbin, plugins


def conda_env_root(harbor_bin: str) -> str:
    """The env directory holding the real harbor CLI, mounted so the shim can exec it."""
    p = Path(harbor_bin).resolve()
    return str(p.parent.parent) if p.parent.name == "bin" else str(p.parent)


def build(harbor_bin: str, ro: list[Path], rw: list[Path],
          with_docker: bool = True) -> str:
    """A harbor --mounts JSON array: DooD toolchain, then read-only, then writable.

    Duplicate and nested paths are dropped: harbor rejects a mount list with the
    same target twice, and a nested read-only mount inside a writable one would
    silently shadow part of the writable tree.
    """
    specs: list[dict] = []
    if with_docker:
        sock, dbin, plugins = _docker_bits()
        specs += [
            {"type": "bind", "source": sock, "target": "/var/run/docker.sock"},
            {"type": "bind", "source": dbin, "target": dbin, "read_only": True},
            {"type": "bind", "source": plugins,
             "target": "/usr/local/lib/docker/cli-plugins", "read_only": True},
            {"type": "bind", "source": conda_env_root(harbor_bin),
             "target": conda_env_root(harbor_bin), "read_only": True},
        ]

    seen = {s["target"] for s in specs}

    def add(p: Path, read_only: bool) -> None:
        rp = str(Path(p).resolve())
        if rp in seen:
            return
        if any(rp != o and rp.startswith(o.rstrip("/") + "/") for o in seen):
            return                      # nested inside something already mounted
        seen.add(rp)
        specs.append({"type": "bind", "source": rp, "target": rp, "read_only": read_only})

    for p in rw:                        # writable first, so a read-only path
        add(p, False)                   # nested inside it is skipped, not shadowing
    for p in ro:
        add(p, True)
    return json.dumps(specs)
