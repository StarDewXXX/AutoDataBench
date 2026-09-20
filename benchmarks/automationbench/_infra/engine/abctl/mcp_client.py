# SPDX-License-Identifier: MIT
"""`abmcp` -- a minimal MCP client, for reference solutions.

Under the MCP interface the agent's tools are not reachable from a shell, and a task's
`solution/solve.sh` runs as the same unprivileged user the agent does. This gives the
reference solution the one thing it needs: a way to make the same gated tool calls.

It is deliberately not a hole and deliberately not advertised in instruction.md.
Reaching the endpoint *is* a tool call -- the server gates the tool list, validates
nothing the agent controls, and returns only the tool's own output -- so an agent that
found it would gain no capability. It stays out of the instruction because the model
already has these tools natively, and a redundant shell path is noise.

    abmcp list
    abmcp call salesforce_contact_update '{"id":"003001","phone":"+1-555-0101"}'
    abmcp call gmail_send_email --json @/tmp/mail.json
"""

from __future__ import annotations

import asyncio
import json
import os
import sys


async def _run(argv: list[str]) -> int:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    url = os.environ.get("AB_MCP_URL", "http://127.0.0.1:8900/mcp")
    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(read, write) as s:
            await s.initialize()
            if argv[0] == "list":
                tools = await s.list_tools()
                print(json.dumps(
                    [{"name": t.name, "inputSchema": t.inputSchema} for t in tools.tools],
                    indent=2))
                return 0
            if argv[0] != "call" or len(argv) < 2:
                print(__doc__, file=sys.stderr)
                return 2
            name = argv[1]
            rest = argv[2:]
            # Accept both `abmcp call NAME JSON` and `abmcp call NAME --json JSON`, so a
            # reference solution written against the abctl bridge ports over by
            # substituting the command name and nothing else.
            if rest and rest[0] == "--json":
                rest = rest[1:]
            raw = rest[0] if rest else "{}"
            if raw.startswith("@"):
                raw = open(raw[1:]).read()
            elif raw == "-":
                raw = sys.stdin.read()
            res = await s.call_tool(name, json.loads(raw))
            text = "\n".join(c.text for c in res.content if getattr(c, "text", None))
            print(text)
            # Exit 0 even when the tool's own response is an error. A tool that reports
            # a bad argument or a missing record is returning an *observation* -- that is
            # what upstream hands the model, and what `abctl call` does -- so treating it
            # as a shell failure would kill any `set -e` script that reads a tool and
            # then decides what to do. Only transport and usage errors are non-zero.
            return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    return asyncio.run(_run(argv))


if __name__ == "__main__":
    sys.exit(main())
