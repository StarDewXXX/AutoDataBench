# SPDX-License-Identifier: MIT
"""Per-task configuration for the abctl bridge.

A harbor task ships a small JSON config into the agent container describing
which toolset it runs, which concrete tools are permitted, and where the
mutable world lives.  It deliberately does NOT contain the assertions: those
are the answer key and only ever reach the verifier container.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_PATH = "/app/task/task_config.json"


@dataclass
class TaskConfig:
    task_name: str
    example_id: int
    toolset: str = "limited_zapier"
    zapier_tools: list[str] = field(default_factory=list)
    allowed_services: list[str] = field(default_factory=list)
    world_path: str = "/app/world.json"
    seed_path: str = "/app/task/initial_state.json"

    @property
    def world(self) -> Path:
        return Path(self.world_path)

    @property
    def seed(self) -> Path:
        return Path(self.seed_path)


def config_path() -> Path:
    return Path(os.environ.get("ABCTL_TASK_CONFIG", DEFAULT_CONFIG_PATH))


def load_config() -> TaskConfig:
    p = config_path()
    if not p.exists():
        raise SystemExit(
            f"abctl: task config not found at {p}. "
            "Set ABCTL_TASK_CONFIG if the task lives elsewhere."
        )
    data = json.loads(p.read_text())
    known = {f for f in TaskConfig.__dataclass_fields__}
    return TaskConfig(**{k: v for k, v in data.items() if k in known})
