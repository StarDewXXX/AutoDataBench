# SPDX-License-Identifier: MIT
"""Tool resolution and dispatch for the three AutomationBench toolsets.

Everything here delegates to the upstream package: the tool functions, their
BM25 discovery indexes and their JSON-schema generation are the originals, so a
tool call made through ``abctl`` mutates the world exactly as it would in the
upstream function-calling harness.  The only thing this module adds is the
per-task gating that ``AutomationBenchEnv.setup_state`` normally performs on
``state["tool_defs"]``.
"""

from __future__ import annotations

import inspect
import json
from typing import Any, Callable

from automationbench.schema.world import WorldState

TOOLSETS = ("limited_zapier", "zapier", "api")


def _registry():
    """Upstream ToolRegistry over ALL_TOOLS (lazily built, ~550 tools)."""
    from automationbench.tools.zapier.meta import _get_registry

    return _get_registry()


def all_tool_map() -> dict[str, Callable]:
    from automationbench.tools import ALL_TOOLS

    return {t.__name__: t for t in ALL_TOOLS}


def api_tool_map() -> dict[str, Callable]:
    from automationbench.tools.api import API_TOOLS

    return {t.__name__: t for t in API_TOOLS}


def tool_map_for(toolset: str) -> dict[str, Callable]:
    """The callables reachable by name for a toolset.

    ``api`` exposes only the three generic REST tools; the underlying service
    mutators are reached through ``api_fetch``'s routers, not by name.  ``zapier``
    exposes the two meta-tools plus (as a convenience for a shell agent) the
    concrete tools, because ``execute_tool`` is just a dispatcher over them.
    """
    if toolset == "api":
        return api_tool_map()
    return all_tool_map()


def permitted_names(cfg) -> set[str] | None:
    """Concrete tool names the task allows, or None for "no per-task filter"."""
    if cfg.toolset == "limited_zapier":
        return set(cfg.zapier_tools)
    return None


def tool_schema(func: Callable) -> dict[str, Any]:
    """JSON schema for a tool's parameters, with ``world`` hidden.

    Uses the upstream generator so the parameter names, types and descriptions
    are byte-identical to what a function-calling model would receive.
    """
    reg = _registry()
    return reg._get_parameter_schema(func)


def tool_description(func: Callable) -> str:
    reg = _registry()
    return reg._get_full_description(func)


def describe_tool(name: str, func: Callable) -> dict[str, Any]:
    return {
        "name": name,
        "description": tool_description(func),
        "parameters": tool_schema(func),
    }


def search(toolset: str, query: str, top_k: int) -> str:
    """Tool/endpoint discovery for the toolset."""
    if toolset == "api":
        from automationbench.tools.api import api_search

        return api_search(query=query, top_k=top_k)
    from automationbench.tools.zapier.meta import search_tools

    return search_tools(query=query, top_k=top_k)


def call(
    world: WorldState,
    toolset: str,
    name: str,
    arguments: dict[str, Any],
    permitted: set[str] | None,
) -> str:
    """Run one tool against ``world`` (mutated in place) and return its JSON."""
    tmap = tool_map_for(toolset)

    # The zapier meta-tools exist only in the `zapier` toolset -- upstream sets
    # use_meta_tools = (toolset == "zapier") and registers them nowhere else. They
    # must stay unavailable in `limited_zapier`, where pre-selecting the tools is
    # the whole point: search_tools would hand over the names, descriptions and
    # full schemas of all ~550 tools, which is information an upstream agent on the
    # same task never receives and cannot derive from the world file.
    if name in ("execute_tool", "search_tools"):
        if toolset != "zapier":
            raise ToolNotFound(name, toolset)
        if name == "search_tools":
            return search(
                toolset, str(arguments.get("query", "")), int(arguments.get("top_k", 5))
            )
        # execute_tool is transparently unwrapped so the permitted-name check
        # applies to the tool actually being run, not to the dispatcher.
        inner = arguments.get("tool_name")
        raw = arguments.get("arguments", "{}")
        inner_args = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
        return call(world, toolset, str(inner), inner_args, permitted)

    func = tmap.get(name)
    if func is None:
        raise ToolNotFound(name, toolset)
    if permitted is not None and name not in permitted:
        raise ToolNotPermitted(name, sorted(permitted))

    sig = inspect.signature(func)
    kwargs = dict(arguments)
    if "world" in sig.parameters:
        kwargs["world"] = world
    else:
        kwargs.pop("world", None)

    # Upstream update_tool_args treats {} as "argument omitted" because some
    # models emit an empty object instead of null; mirror that so a shell agent
    # copying a schema default behaves the same way.
    kwargs = {k: v for k, v in kwargs.items() if not (isinstance(v, dict) and len(v) == 0 and k != "world")}

    result = func(**kwargs)
    return result if isinstance(result, str) else json.dumps(result)


class ToolNotFound(Exception):
    def __init__(self, name: str, toolset: str):
        self.name, self.toolset = name, toolset
        # Point at the discovery route this toolset actually has: limited_zapier
        # has none by design, so telling its agent to run `abctl search` would send
        # it at a command that refuses.
        hint = {
            "limited_zapier": "run `abctl list --full` to see the tools you may use",
            "zapier": "use `abctl search <query>` to discover tools",
            "api": "use `abctl search <query>` to discover endpoints, then `abctl fetch`",
        }.get(toolset, "use `abctl list` to see what is available")
        super().__init__(f"unknown tool {name!r} for toolset {toolset!r}; {hint}")


class ToolNotPermitted(Exception):
    def __init__(self, name: str, allowed: list[str]):
        self.name, self.allowed = name, allowed
        super().__init__(
            f"tool {name!r} is not available for this task; allowed: {', '.join(allowed)}"
        )
