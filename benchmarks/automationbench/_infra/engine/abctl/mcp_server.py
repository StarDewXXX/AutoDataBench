# SPDX-License-Identifier: MIT
"""MCP server exposing a task's AutomationBench tools as native tool calls.

This is the faithful interface: upstream the model is a function-calling agent that
receives tool schemas and emits tool calls, and MCP is how a harbor agent gets the
same thing. The shell bridge (``abctl``) stays for authoring and grading.

The tool list, the schemas and the per-task gating all come from ``abctl.tools``, so
what a model sees here is what upstream's ``state["tool_defs"]`` would have given it:

    limited_zapier -> exactly info["zapier_tools"]
    zapier         -> only search_tools + execute_tool (upstream's use_meta_tools)
    api            -> only api_search + api_fetch + base64_encode

Two properties are load-bearing for the observation boundary and must not regress:

* **No verb dumps the world**, and **no argument names a path.** A privileged CLI
  fails here — `abctl state` prints the world and `--json @file` is a file-read
  primitive — which is exactly why the agent's interface is this server and not that
  CLI.
* The task config path is a module constant and neither ``argv`` nor the environment
  is consulted for it, so a privileged launch has nothing agent-controlled to chew
  on. ``ABCTL_TASK_CONFIG`` is honoured only when ``--unprivileged`` is passed, which
  exists for authoring and the Stage-0 probes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

TASK_CONFIG = Path("/srv/world/task_config.json")


def _load_config(unprivileged: bool):
    from abctl.config import TaskConfig

    path = TASK_CONFIG
    if unprivileged:
        path = Path(os.environ.get("ABCTL_TASK_CONFIG", str(TASK_CONFIG)))
    if not path.exists():
        raise SystemExit(f"abctl-mcp: task config not found at {path}")
    data = json.loads(path.read_text())
    known = {f for f in TaskConfig.__dataclass_fields__}
    return TaskConfig(**{k: v for k, v in data.items() if k in known})


def _exposed_tools(cfg) -> list[str]:
    """The names this task's toolset makes available, mirroring upstream."""
    from abctl import tools as toolmod

    if cfg.toolset == "limited_zapier":
        tmap = toolmod.all_tool_map()
        # Fail on a name that does not exist, as upstream does
        # (setup_state: "Unknown tools specified in task"). Silently dropping it would
        # hand the agent a smaller tool set than the task specifies and score the result
        # as if nothing were wrong -- exactly the defect that
        # hr.performance_feedback_logging (which names a nonexistent slack_find_user_by_id)
        # exists to catch.
        unknown = [n for n in cfg.zapier_tools if n not in tmap]
        if unknown:
            raise SystemExit(
                f"abctl-mcp: task {cfg.task_name} lists tool(s) that do not exist: "
                f"{', '.join(unknown)}"
            )
        return list(cfg.zapier_tools)
    if cfg.toolset == "api":
        return sorted(toolmod.api_tool_map())
    return ["search_tools", "execute_tool"]


def _resolve(name: str):
    from abctl import tools as toolmod
    from automationbench.tools.zapier.meta import execute_tool, search_tools

    meta = {"search_tools": search_tools, "execute_tool": execute_tool}
    if name in meta:
        return meta[name]
    return toolmod.all_tool_map().get(name) or toolmod.api_tool_map().get(name)


def build_server(cfg, world_holder: dict):
    """Wire an MCP server over a task config and an in-memory world."""
    from mcp.server import Server
    from mcp.types import TextContent, Tool

    from abctl import tools as toolmod
    from abctl.world import save_world

    names = _exposed_tools(cfg)
    schemas = {n: toolmod.describe_tool(n, _resolve(n)) for n in names}
    server = Server("automationbench")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name=n,
                description=schemas[n]["description"],
                inputSchema=schemas[n]["parameters"],
            )
            for n in names
        ]

    # validate_input=False on purpose. `agents.function_schema` lists every
    # parameter in `required` (the OpenAI strict-schema convention: optionals are
    # `anyOf [T, null]` but still required), and upstream tolerates omissions because
    # every one of those parameters has a Python default. The MCP SDK would instead
    # hard-reject a call that omits any of them -- making these tasks *harder* than
    # upstream. So: advertise the schema verbatim, enforce it as upstream does.
    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict[str, Any] | None) -> list[TextContent]:
        try:
            result = toolmod.call(
                world_holder["world"],
                cfg.toolset,
                name,
                dict(arguments or {}),
                set(names) if cfg.toolset == "limited_zapier" else None,
            )
        except (toolmod.ToolNotFound, toolmod.ToolNotPermitted) as e:
            return [TextContent(type="text", text=json.dumps({"error": str(e)}))]
        except TypeError as e:
            # Wrong arguments: hand back the schema so the model can self-correct,
            # and leave the world untouched -- same contract as the CLI.
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {"error": f"bad arguments for {name}: {e}",
                         "parameters": schemas.get(name, {}).get("parameters", {})}
                    ),
                )
            ]
        except Exception as e:
            return [
                TextContent(
                    type="text",
                    text=json.dumps({"error": f"{name} raised {type(e).__name__}: {e}"}),
                )
            ]
        # Flush after every call so a killed server still leaves a gradeable world.
        save_world(cfg.world, world_holder["world"])
        os.chmod(cfg.world, 0o600)
        return [TextContent(type="text", text=result)]

    return server


def load_world_and_forget_seed(cfg, delete_seed: bool) -> dict:
    """Materialize the world in memory, then remove the seed from disk.

    Deleting the seed is what lets one container be enough: once the world is in
    memory there is no file left that could reveal what the agent was not given a
    tool to read. Restart recovery reads ``world.json``, never the seed, so a
    supervised restart resumes rather than reverting the agent's work.
    """
    from abctl.world import build_world, load_world, save_world

    if cfg.world.exists():
        world = load_world(cfg.world)          # resume after a restart
    else:
        world = build_world(json.loads(cfg.seed.read_text()), cfg.allowed_services)
        save_world(cfg.world, world)
    os.chmod(cfg.world, 0o600)
    if delete_seed and cfg.seed.exists():
        cfg.seed.unlink()
    return {"world": world}


async def _serve_stdio(server) -> None:
    import mcp.server.stdio

    async with mcp.server.stdio.stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="abctl-mcp", description=__doc__)
    ap.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8900)
    ap.add_argument(
        "--unprivileged",
        action="store_true",
        help="honour ABCTL_TASK_CONFIG (authoring/probing only; never in a task image)",
    )
    ap.add_argument(
        "--check",
        action="store_true",
        help="validate the seed, the tool names and their schemas, then exit without "
             "serving or touching disk (build-time gate)",
    )
    ap.add_argument(
        "--keep-seed",
        action="store_true",
        help="do not delete the seed after loading (authoring/probing only)",
    )
    args = ap.parse_args(argv)

    cfg = _load_config(args.unprivileged)

    if args.check:
        # Everything a run depends on, checked at image-build time: the seed loads into a
        # WorldState, every permitted tool name resolves, and every schema generates.
        # Nothing is written and the seed is not removed.
        from abctl.world import build_world

        world = build_world(json.loads(cfg.seed.read_text()), cfg.allowed_services)
        server = build_server(cfg, {"world": world})
        n = len(_exposed_tools(cfg))
        print(f"abctl-mcp --check: {cfg.task_name} ok ({n} tool(s), seed loads)")
        return 0

    holder = load_world_and_forget_seed(cfg, delete_seed=not args.keep_seed)
    server = build_server(cfg, holder)

    if args.transport == "stdio":
        asyncio.run(_serve_stdio(server))
        return 0

    import uvicorn
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

    manager = StreamableHTTPSessionManager(app=server, stateless=True)

    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                msg = await receive()
                if msg["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif msg["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        else:
            await manager.handle_request(scope, receive, send)

    async def run() -> None:
        async with manager.run():
            cfg_u = uvicorn.Config(app, host=args.host, port=args.port, log_level="warning")
            await uvicorn.Server(cfg_u).serve()

    asyncio.run(run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
