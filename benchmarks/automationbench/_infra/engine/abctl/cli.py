# SPDX-License-Identifier: MIT
"""``abctl`` — the shell bridge to AutomationBench's simulated SaaS world.

harbor's agent lives in a terminal, while AutomationBench's tools are Python
functions that mutate an in-memory object.  ``abctl`` closes that gap: each
sub-command loads ``world.json``, performs one upstream operation, writes the
world back atomically, and prints the tool's own JSON response on stdout.  The
result is that a shell agent produces exactly the state a function-calling agent
would, in a file the verifier can grade.

    abctl list                        # tools this task allows, with schemas
    abctl search "update contact"     # BM25 discovery over tools / endpoints
    abctl call gmail_send_email --json '{"to":["a@b.c"],"subject":"hi","body":"yo"}'
    abctl fetch GET https://gmail.googleapis.com/gmail/v1/users/me/messages
    abctl state --path salesforce.contacts
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from abctl import tools as toolmod
from abctl.config import TaskConfig, load_config
from abctl.world import build_world, load_world, save_world


def _die(msg: str, code: int = 1) -> None:
    print(json.dumps({"error": msg}, indent=2), file=sys.stdout)
    raise SystemExit(code)


def _read_json_arg(raw: str | None) -> dict[str, Any]:
    """Parse ``--json``: literal JSON, ``@path`` to read a file, or ``-`` for stdin.

    The file/stdin forms exist because tool arguments here routinely contain
    multi-line email bodies with quotes and apostrophes, which are painful and
    error-prone to embed in a shell command.
    """
    if raw is None:
        return {}
    raw = raw.strip()
    if raw == "":
        return {}
    if raw == "-":
        raw = sys.stdin.read()
    elif raw.startswith("@"):
        raw = Path(raw[1:]).read_text()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        _die(f"--json is not valid JSON: {e}. Tip: use --json @file or --json - for long bodies.")
    if not isinstance(parsed, dict):
        _die("--json must be a JSON object of tool arguments")
    return parsed


def _ensure_world(cfg: TaskConfig):
    """Load the live world, materializing it from the seed on first use."""
    if not cfg.world.exists():
        if not cfg.seed.exists():
            _die(f"neither world ({cfg.world}) nor seed ({cfg.seed}) exists")
        seed = json.loads(cfg.seed.read_text())
        world = build_world(seed, cfg.allowed_services)
        save_world(cfg.world, world)
        return world
    return load_world(cfg.world)


# --------------------------------------------------------------------------- #
# sub-commands
# --------------------------------------------------------------------------- #

def cmd_list(cfg: TaskConfig, args) -> int:
    tmap = toolmod.tool_map_for(cfg.toolset)
    permitted = toolmod.permitted_names(cfg)
    if permitted is not None:
        names = [n for n in cfg.zapier_tools if n in tmap]
        missing = [n for n in cfg.zapier_tools if n not in tmap]
    elif cfg.toolset == "api":
        names, missing = sorted(tmap), []
    else:
        # zapier meta-tool mode: only the discovery pair is "listed"; everything
        # else is meant to be found with `abctl search`.
        names, missing = ["search_tools", "execute_tool"], []

    out: list[dict[str, Any]] = []
    for n in names:
        func = tmap.get(n)
        if func is None:
            from automationbench.tools.zapier.meta import execute_tool, search_tools

            func = {"search_tools": search_tools, "execute_tool": execute_tool}.get(n)
        if func is None:
            continue
        out.append(
            toolmod.describe_tool(n, func)
            if args.full
            else {"name": n, "description": toolmod.tool_description(func).split("\n")[0]}
        )
    payload: dict[str, Any] = {"toolset": cfg.toolset, "tools": out}
    if missing:
        payload["unresolved"] = missing
    print(json.dumps(payload, indent=2))
    if missing:
        # A limited_zapier task whose zapier_tools names a tool that does not exist
        # is broken: upstream's setup_state raises ValueError on exactly this, so
        # the task never ran there either. Exiting non-zero makes each task image's
        # build-time `abctl list` catch it instead of the agent discovering a tool
        # it was promised is absent.
        print(
            f"abctl: {len(missing)} tool(s) in this task do not exist: {', '.join(missing)}",
            file=sys.stderr,
        )
        return 1
    return 0


def cmd_search(cfg: TaskConfig, args) -> int:
    # Discovery belongs to the toolsets that upstream gives a discovery tool to.
    # In limited_zapier the tools are pre-selected and listed in the instruction;
    # letting the agent search the full ~550-tool registry there would hand it
    # information an upstream agent on the same task never receives.
    if cfg.toolset == "limited_zapier":
        _die(
            "this task has a fixed tool list; `abctl search` is not available. "
            "Run `abctl list --full` to see the tools you may use."
        )
    print(toolmod.search(cfg.toolset, args.query, args.top_k))
    return 0


def cmd_call(cfg: TaskConfig, args) -> int:
    arguments = _read_json_arg(args.json)
    world = _ensure_world(cfg)
    try:
        result = toolmod.call(
            world, cfg.toolset, args.tool, arguments, toolmod.permitted_names(cfg)
        )
    except (toolmod.ToolNotFound, toolmod.ToolNotPermitted) as e:
        _die(str(e))
    except TypeError as e:
        # Wrong/missing arguments: report the schema so the agent can self-correct
        # instead of guessing, and leave the world untouched.
        func = toolmod.tool_map_for(cfg.toolset).get(args.tool)
        detail: dict[str, Any] = {"error": f"bad arguments for {args.tool}: {e}"}
        if func is not None:
            detail["parameters"] = toolmod.tool_schema(func)
        print(json.dumps(detail, indent=2))
        return 1
    except Exception as e:  # a tool raising is a failed call, not a broken bridge
        _die(f"{args.tool} raised {type(e).__name__}: {e}")
    save_world(cfg.world, world)
    print(result)
    return 0


def cmd_fetch(cfg: TaskConfig, args) -> int:
    if cfg.toolset != "api":
        _die(f"`abctl fetch` is only available in the api toolset (this task: {cfg.toolset})")
    world = _ensure_world(cfg)
    from automationbench.tools.api import api_fetch

    body = args.json
    if body is not None:
        body = json.dumps(_read_json_arg(body))
    params = args.params
    if params is not None:
        params = json.dumps(_read_json_arg(params))
    result = api_fetch(
        world=world, method=args.method.upper(), url=args.url, params=params, body=body
    )
    save_world(cfg.world, world)
    print(result)
    return 0


def cmd_state(cfg: TaskConfig, args) -> int:
    world = _ensure_world(cfg)
    data: Any = world.model_dump(mode="json")
    if args.path:
        for part in args.path.split("."):
            if isinstance(data, list):
                try:
                    data = data[int(part)]
                    continue
                except (ValueError, IndexError):
                    _die(f"no index {part!r} in list of {len(data)}")
            if not isinstance(data, dict) or part not in data:
                keys = sorted(data) if isinstance(data, dict) else type(data).__name__
                _die(f"no key {part!r}; available: {keys}")
            data = data[part]
    if args.non_empty and isinstance(data, dict):
        data = {k: v for k, v in data.items() if v not in ({}, [], None, "")}
    print(json.dumps(data, indent=2))
    return 0


def cmd_reset(cfg: TaskConfig, args) -> int:
    if not cfg.seed.exists():
        _die(f"seed not found at {cfg.seed}")
    world = build_world(json.loads(cfg.seed.read_text()), cfg.allowed_services)
    save_world(cfg.world, world)
    print(json.dumps({"ok": True, "reset": str(cfg.world)}, indent=2))
    return 0


def cmd_oracle(cfg: TaskConfig, args) -> int:
    """Constructive reference solution: see abctl.oracle."""
    from abctl.oracle import apply_assertions

    spec = json.loads(Path(args.spec).read_text())
    world = _ensure_world(cfg)
    report = apply_assertions(world, spec.get("assertions", []))
    save_world(cfg.world, world)
    print(json.dumps(report, indent=2))
    # 2 = this applier fell short; 3 = the task's own rubric refuses the change it
    # asked for, which is a defect in the task rather than in the applier.
    if report["refuted"]:
        return 3
    return 2 if report["unsupported"] else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="abctl",
        description="Operate the AutomationBench simulated workspace from the shell.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("list", help="list the tools this task allows")
    s.add_argument("--full", action="store_true", help="include full description + JSON schema")
    s.set_defaults(fn=cmd_list)

    s = sub.add_parser("search", help="find tools (zapier) or endpoints (api) by keyword")
    s.add_argument("query")
    s.add_argument("--top-k", type=int, default=5)
    s.set_defaults(fn=cmd_search)

    s = sub.add_parser("call", help="run one tool")
    s.add_argument("tool")
    s.add_argument("--json", help='arguments as JSON, or @file, or - for stdin')
    s.set_defaults(fn=cmd_call)

    s = sub.add_parser("fetch", help="call a REST endpoint (api toolset only)")
    s.add_argument("method")
    s.add_argument("url")
    s.add_argument("--json", help="request body as JSON, or @file, or -")
    s.add_argument("--params", help="query parameters as JSON, or @file, or -")
    s.set_defaults(fn=cmd_fetch)

    s = sub.add_parser("state", help="inspect the current world")
    s.add_argument("--path", help="dotted path, e.g. salesforce.contacts.0")
    s.add_argument("--non-empty", action="store_true", help="hide empty top-level collections")
    s.set_defaults(fn=cmd_state)

    s = sub.add_parser("reset", help="restore the world from the task seed")
    s.set_defaults(fn=cmd_reset)

    s = sub.add_parser("oracle", help="apply a spec's assertions to the world (verifier self-check)")
    s.add_argument("--spec", required=True, help="path to task_spec.json")
    s.set_defaults(fn=cmd_oracle)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config()
    return args.fn(cfg, args)


if __name__ == "__main__":
    raise SystemExit(main())
