# SPDX-License-Identifier: MIT
"""World-state persistence.

AutomationBench keeps its simulated SaaS universe in a single in-memory
``WorldState`` pydantic model that the tool functions mutate in place.  harbor's
agent works in a shell across many short-lived processes, so the world has to
survive between them: every ``abctl`` invocation loads ``world.json``, runs one
tool, and writes it back.  The verifier then reads the same file (carried into a
clean verifier container as a harbor artifact) and grades it.

Writes are atomic (temp file + ``os.replace``) so a killed process can never
leave a truncated world behind.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from automationbench.schema.world import WorldState


def strip_none_values(obj):
    """Recursively drop ``None`` values from nested dicts/lists.

    Upstream (``automationbench.runner``) needs this because HuggingFace
    Dataset normalizes row schemas and fills missing keys with ``None``, which
    defeats pydantic's ``default_factory``.  Our task JSON is authored directly,
    but the same normalization can appear in hand-edited seeds, so keep parity
    with upstream rather than assuming clean input.
    """
    if isinstance(obj, dict):
        return {k: strip_none_values(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [strip_none_values(item) for item in obj if item is not None]
    return obj


def build_world(initial_state: dict, allowed_services: list[str] | None = None) -> WorldState:
    """Construct a fresh world from a task's ``initial_state`` seed."""
    world = WorldState(**strip_none_values(initial_state or {}))
    if allowed_services is not None:
        world.meta.allowed_services = list(allowed_services)
    return world


def load_world(path: Path) -> WorldState:
    data = json.loads(Path(path).read_text())
    return WorldState(**strip_none_values(data))


def save_world(path: Path, world: WorldState) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(world.model_dump(mode="json"), indent=1, sort_keys=False)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".world-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(payload)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp) if os.path.exists(tmp) else None
        raise
