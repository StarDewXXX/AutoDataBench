#!/usr/bin/env python3
"""Did one harbor run end normally, run out of its own time, or die on us?

Every retry and every score in v2 hangs off this distinction, so it lives in one
place with one vocabulary:

  ok        the agent finished and said so. The reward, whatever it is, measures
            the model against the task.
  timeout   the agent used up the time it was given. For a RESEARCHER run that is
            the budget working as designed; for a CUSTOMER attempt it is a real
            failure (the model did not produce a deliverable in the time the task
            allows). Either way there is nothing to retry -- a rerun would hit
            the same wall.
  abnormal  the run died for a reason that has nothing to do with the task: the
            gateway throttled us, a connection dropped, the container never came
            up, the process was killed. This is the only state worth retrying,
            and the only one that must be excluded from a score.
  unknown   nothing to classify from -- no harbor result at all.

WHY TWO SOURCES. harbor's own `exception_info` is not enough, and the gap is not
academic: it is the dominant failure of the 2026-09-11 four-model batch. When
claude-code hits a fatal API error it prints the error and exits **zero**, so
harbor sees a clean agent exit, runs the verifier anyway, and records
`reward: 0.0` with `exception_info: null`. 45 of glm-5.3's 188 zero-reward
attempts were 429s of exactly this shape -- indistinguishable from "the model
tried and failed" if you only read harbor. Conversely claude-code writes no
terminal record at all when it is killed mid-turn, and there only harbor knows
why. So:

  harbor      <run>/**/result.json  ->  exception_info.exception_type
  claude-code <run>/**/agent/claude-code.txt  ->  last {"type":"result"} line,
              which carries terminal_reason / is_error / subtype / num_turns

The claude-code side is only consulted when that file exists, so a codex or
mini-swe-agent run is judged by harbor alone rather than being called abnormal
for lacking a file it never writes.

Both sources are read newest-first (see common.newest_first): an attempt
directory can hold a dead job beside its rerun, and pairing the old verdict with
the new trace is how you get a confident wrong answer.
"""
from __future__ import annotations

import json
from pathlib import Path

from common import newest_first

# harbor raises this when the agent used up its own budget. It is the ONE
# exception that is not a fault. `EnvironmentStartTimeoutError` is also a timeout
# by name and must NOT be folded in here: it means the container never started,
# which is infrastructure and is worth retrying.
TIMEOUT_EXCEPTIONS = {"AgentTimeoutError"}

# Every terminal_reason claude-code reports that means "I finished the job".
# Anything else -- api_error above all -- is a fault even when the exit code is 0.
CLEAN_TERMINAL_REASONS = {"completed", "end_turn", "stop_sequence"}


def _harbor_exception(run_dir: Path) -> tuple[str | None, str, Path | None]:
    """(exception_type, message, the result.json it came from)."""
    for res in newest_first(run_dir.rglob("result.json")):
        try:
            data = json.loads(res.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict) or "exception_info" not in data:
            continue                     # a run-level result.json, not a trial's
        ei = data.get("exception_info")
        if isinstance(ei, dict):
            return (ei.get("exception_type") or "UnknownException",
                    (ei.get("exception_message") or "")[:400], res)
        return None, "", res
    return None, "", None


def _agent_name(run_dir: Path) -> str | None:
    """Which harness ran, from harbor's own trial config.

    Needed because "no claude-code.txt" means two opposite things: for an oracle
    or a codex run it is normal (they never write one), and for a claude-code run
    it means the log was never collected -- which we must not silently score as a
    clean finish. Read from config.json rather than trajectory.json because the
    config exists even when the agent produced nothing.
    """
    for cfg in newest_first(run_dir.rglob("config.json")):
        try:
            data = json.loads(cfg.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        ag = data.get("agent")
        if isinstance(ag, dict) and ag.get("name"):
            return str(ag["name"])
    return None


def _claude_terminal(run_dir: Path) -> tuple[bool, dict | None]:
    """(claude-code wrote a stdout log, its last result record if any).

    The log is newline-delimited JSON with one final `{"type":"result", ...}`.
    A missing record means claude never got to print it -- it was killed.
    """
    for log in newest_first(run_dir.rglob("agent/claude-code.txt")):
        last = None
        try:
            with log.open(errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line.startswith("{") or '"type"' not in line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("type") == "result":
                        last = obj
        except OSError:
            continue
        return True, last
    return False, None


def classify(run_dir: Path) -> dict:
    """Classify one harbor run directory (an attempt dir, or a job dir).

    Returns {state, reason, detail}. `reason` is a short stable token meant for
    grouping in a report; `detail` is free text for a human.
    """
    if not run_dir.exists():
        return {"state": "unknown", "reason": "no-run-dir", "detail": str(run_dir)}

    exc, msg, res = _harbor_exception(run_dir)
    if res is None:
        return {"state": "unknown", "reason": "no-result-json",
                "detail": f"no trial result.json under {run_dir}"}

    if exc is not None:
        if exc in TIMEOUT_EXCEPTIONS:
            return {"state": "timeout", "reason": exc, "detail": msg}
        return {"state": "abnormal", "reason": exc, "detail": msg}

    has_log, rec = _claude_terminal(run_dir)
    if not has_log:
        agent = _agent_name(run_dir)
        if agent is None or agent == "claude-code":
            # A claude-code run always writes this log, and harbor always writes a
            # config.json beside the result it just wrote -- so either gap means the
            # output tree is incomplete and we cannot see the one failure mode
            # harbor does not report (a clean exit after a fatal API error).
            # Calling that "ok" is exactly the silent pass this module exists to
            # prevent, so it is unknown: retryable, not scoreable.
            missing = "agent/claude-code.txt" if agent else "config.json (agent unknown)"
            return {"state": "unknown", "reason": "agent-log-missing",
                    "detail": f"no {missing} under {run_dir}"}
        # oracle / codex / mini-swe-agent never write one. harbor raised nothing,
        # so take its word for it.
        return {"state": "ok", "reason": f"harbor-clean:{agent}", "detail": ""}

    if rec is None:
        return {"state": "abnormal", "reason": "no-terminal-record",
                "detail": "claude-code wrote no final result record; killed mid-turn"}

    if rec.get("is_error"):
        return {"state": "abnormal", "reason": f"is_error:{rec.get('terminal_reason') or '?'}",
                "detail": str(rec.get("result") or "")[:400]}

    tr = rec.get("terminal_reason")
    if tr is not None and tr not in CLEAN_TERMINAL_REASONS:
        return {"state": "abnormal", "reason": f"terminal_reason:{tr}",
                "detail": str(rec.get("result") or "")[:400]}

    return {"state": "ok", "reason": tr or (rec.get("subtype") or "success"), "detail": ""}


def is_retryable(state: str) -> bool:
    """Only a fault is worth rerunning. A timeout would hit the same wall."""
    return state in ("abnormal", "unknown")


def _survey(roots: list[str]) -> None:
    """Classify every harbor run under the given roots and print a tally.

    Used to check the classifier against runs whose outcome we already know by
    hand, which is the only way to trust it before it starts deleting things.
    """
    import collections
    import sys
    tally: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for root in roots:
        for d in sorted(Path(root).glob("*")):
            if not d.is_dir():
                continue
            c = classify(d)
            tally[root][(c["state"], c["reason"])] += 1
    for root, cnt in tally.items():
        print(f"== {root}  ({sum(cnt.values())} run(s))", file=sys.stderr)
        for (state, reason), n in sorted(cnt.items(), key=lambda x: (-x[1], str(x[0]))):
            print(f"   {state:9} {reason:34} {n}", file=sys.stderr)


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        raise SystemExit("usage: terminal.py <dir-of-run-dirs> [...]   "
                         "(each child directory is classified)")
    _survey(sys.argv[1:])
