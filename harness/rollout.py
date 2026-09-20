#!/usr/bin/env python3
"""Customer-model rollouts, and the sanitized digest built from them.

Two things live here because both are needed twice:

  run_rollouts()   K attempts at one task with the customer model. Used on the
                   ORIGINAL tasks during prep (to learn how the customer fails)
                   and on each SYNTHESIZED task during scoring (to measure
                   difficulty and to give the judge something to read).

  write_digest()   Turns one attempt's harbor output into a readable markdown
                   transcript with secrets stripped.

The digest is not a convenience. v2 hands rollout traces to models we are
evaluating (the researcher sees the original task's traces; the judge sees the
synthesized task's), so what they read is built from agent/trajectory.json, the
claude-code session log and the verifier's stdout, and is then re-scrubbed by
pattern. Mount the digest, not a raw attempt directory: the digest is the surface
we have actually reviewed.

RAW OUTPUT IS NOW KEPT (2026-09-13). It used to be deleted once the digest was
built, on two grounds. One was size, which is affordable: an attempt's raw tree is
0.8 MB on automationbench and up to 64 MB on tb-science. The other was that it
"carries the resolved gateway key" -- that is no longer true and may never have
been on this harbor. Under 0.20.0 every config.json records the credential as the
literal string `${ANTHROPIC_API_KEY}`, even for a value passed on the command line
with --ae, and a search for the key across the whole runs/ tree matches zero
files. Deleting it cost us the only authoritative record of HOW a run ended:
harbor's exception_info and claude-code's terminal result line both live there and
neither survives into the digest, so a gateway 429 that killed an attempt was
indistinguishable from a model that tried and failed. See harness/terminal.py.
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import os
import re
import shutil
from pathlib import Path

from common import (Harbor, agent_timeout_sec, log, newest_first, read_reward,
                    rollout_dirs, task_pass_threshold, task_timeout_sec)
from terminal import classify

# Fewer usable attempts than this and the difficulty band is not measurable: with
# two attempts pass_rate can only be 0, 0.5 or 1, and with one it can only be 0 or
# 1 -- so 0.125..0.75 is either unreachable or hit by a coin flip. Such an episode
# is recorded as unscorable rather than given a confident 0.
MIN_USABLE_ATTEMPTS = 3

# Belt and braces over building the digest from the clean files only: anything
# shaped like a credential is masked even if it reaches the transcript some other
# way (an agent echoing its own env, a verifier printing a header).
_SECRET_PATTERNS = [
    re.compile(r"\b(univ|sk|sk-ant|hf)-[A-Za-z0-9_\-]{12,}\b"),
    re.compile(r"(?i)\b(ANTHROPIC|OPENAI|HF|HUGGINGFACE)[A-Z_]*(KEY|TOKEN)\s*[=:]\s*\S+"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{16,}"),
    re.compile(r"(?i)\bx-api-key\s*[=:]\s*\S+"),
]


def scrub(text: str) -> str:
    for pat in _SECRET_PATTERNS:
        text = pat.sub(lambda m: m.group(0).split("=")[0].split(":")[0] + "=<redacted>"
                       if ("=" in m.group(0) or ":" in m.group(0)) else "<redacted>", text)
    return text


# --------------------------------------------------------------------------- #
# digest
# --------------------------------------------------------------------------- #

def _trajectory(attempt_dir: Path) -> dict | None:
    # Newest first: a killed run's job dir may sit beside the rerun's (see
    # common.newest_first), and pairing the old trace with the new reward would
    # misrepresent the attempt.
    for p in newest_first(attempt_dir.rglob("agent/trajectory.json")):
        try:
            return json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            continue
    return None


def _session_records(attempt_dir: Path) -> list[dict]:
    """The agent's real turn-by-turn transcript.

    harbor's agent/trajectory.json is NOT it: for claude-code runs its per-step
    `message` field is empty for every step except the first prompt and the final
    reply, so a digest built from it looks complete and contains nothing. The
    content lives in the claude-code session log at
    agent/sessions/projects/<cwd>/<session-id>.jsonl -- 380 KB of thinking blocks,
    tool calls and tool results for a run whose trajectory.json message fields
    summed to 3.8 KB.

    Prefers the session whose id matches trajectory.json (the main agent) and
    ignores the rest, which are subagent sessions sharing the directory. Falls
    back to the largest file when there is no id to match on.
    """
    files = [p for p in attempt_dir.rglob("agent/sessions/**/*.jsonl") if p.is_file()]
    if not files:
        return []
    chosen = None
    traj = _trajectory(attempt_dir)
    sid = (traj or {}).get("session_id")
    if sid:
        # Match the session id of the trajectory we actually used, so trace and
        # reward describe the same run even when several jobs share the dir.
        for p in newest_first(files):
            if p.stem == sid:
                chosen = p
                break
    if chosen is None:
        chosen = max(files, key=lambda p: p.stat().st_size)
    recs: list[dict] = []
    try:
        with chosen.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    recs.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return recs


def _elide(s: str, limit: int) -> str:
    """Shorten in the MIDDLE, never at the end.

    The tail of a tool result -- the error, the final numbers -- is usually the
    part that explains a failure, so truncating there throws away the evidence.
    """
    if len(s) <= limit:
        return s
    half = limit // 2
    return s[:half] + f"\n\n[... {len(s) - limit} characters elided ...]\n\n" + s[-half:]


def _fit_middle(turns: list[str], budget: int) -> list[str]:
    """Keep the start and the END of a transcript, elide the middle.

    Splits the budget evenly and fills from both ends, so the last turns survive
    whatever the length. The gap says how many steps went and which ones, because
    a reader citing "step N" needs to know N is still the same step it was.
    """
    total = sum(len(t) for t in turns)
    if total <= budget:
        return turns
    half = budget // 2
    head: list[str] = []
    used = 0
    for t in turns:
        if used + len(t) > half:
            break
        head.append(t)
        used += len(t)
    tail: list[str] = []
    used = 0
    for t in reversed(turns[len(head):]):
        if used + len(t) > budget - sum(len(x) for x in head):
            break
        tail.insert(0, t)
        used += len(t)
    dropped = len(turns) - len(head) - len(tail)
    if dropped <= 0:                     # one turn alone blows the budget
        return turns[:1] + turns[-1:] if len(turns) > 1 else turns
    gap = (f"\n[... {dropped} step(s) elided from the middle "
           f"(steps {len(head) + 1}–{len(head) + dropped}), "
           f"{total - budget} characters over budget; step numbering is unchanged "
           f"and the final steps are below ...]\n")
    return head + [gap] + tail


def _render_blocks(content, max_chars: int) -> list[str]:
    """One claude-code message's content blocks as markdown."""
    if isinstance(content, str):
        return [_elide(content, max_chars)] if content.strip() else []
    if not isinstance(content, list):
        return []
    out: list[str] = []
    for b in content:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text":
            txt = (b.get("text") or "").strip()
            if txt:
                out.append(_elide(txt, max_chars))
        elif t == "thinking":
            txt = (b.get("thinking") or "").strip()
            if txt:
                # Kept: the reasoning is where a wrong turn is visible before it
                # shows up in an action, which is exactly what the analyst needs.
                out.append("**thinking**\n\n" + _elide(txt, max_chars))
        elif t == "tool_use":
            args = json.dumps(b.get("input"), ensure_ascii=False, indent=2)
            out.append(f"**tool call: {b.get('name')}**\n\n```json\n{_elide(args, max_chars)}\n```")
        elif t == "tool_result":
            c = b.get("content")
            s = c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)
            flag = " (error)" if b.get("is_error") else ""
            out.append(f"**tool result{flag}**\n\n```\n{_elide(s or '', max_chars)}\n```")
    return out


def _agent_label(traj: dict | None) -> str:
    a = (traj or {}).get("agent")
    if isinstance(a, dict):
        name = a.get("name") or "?"
        ver = a.get("version")
        model = a.get("model_name")
        bits = [name] + ([str(ver)] if ver else []) + ([f"model={model}"] if model else [])
        return " ".join(bits)
    return str(a or "unknown")


def _verifier_stdout(attempt_dir: Path) -> str:
    """The most recent verifier output only -- not every job's concatenated."""
    for p in newest_first(attempt_dir.rglob("verifier/test-stdout.txt")):
        try:
            return p.read_text()
        except (OSError, UnicodeDecodeError):
            continue
    return ""


def write_digest(attempt_dir: Path, dst: Path, index: int, reward: float | None,
                 solved: bool | None, max_block_chars: int = 6000,
                 max_total_chars: int = 500_000) -> Path:
    """Write one attempt as a readable, secret-free markdown transcript.

    Steps are numbered 1..N over the turns actually rendered, and that numbering is
    the one the analyst and the judge cite as "step N" -- so it has to be stable
    for a given attempt, which it is: it counts rendered turns in file order.
    """
    traj = _trajectory(attempt_dir)
    recs = _session_records(attempt_dir)
    term = classify(attempt_dir)
    lines = [
        f"# attempt {index}",
        "",
        f"- reward: {'(no verdict)' if reward is None else reward}",
        f"- counted as solved: {'unknown' if solved is None else ('yes' if solved else 'no')}",
        # How the run ENDED, stated up front. Without it a reader cannot tell a
        # model that failed from a run the gateway cut off, and every reader of
        # this file -- the researcher, the judge, us -- was making that mistake.
        f"- how it ended: {term['state']} ({term['reason']})"
        + (f" — {term['detail'].splitlines()[0][:200]}" if term.get("detail") else ""),
        f"- agent: {_agent_label(traj)}",
    ]
    if traj:
        fm = traj.get("final_metrics") or {}
        lines.append(f"- tokens in/out: {fm.get('total_prompt_tokens', '?')}"
                     f"/{fm.get('total_completion_tokens', '?')}")
    if term["state"] != "ok":
        lines += ["", f"> This attempt did not finish cleanly ({term['state']}). "
                      + ("It used up the time the task allows, so whatever it had "
                         "written by then is what the verifier graded."
                         if term["state"] == "timeout" else
                         "The cause is infrastructure, not the task, so its reward "
                         "does not measure the model against this task.")]
    lines += ["", "## verifier output", "",
              "```", _verifier_stdout(attempt_dir).strip() or "(none captured)", "```", ""]

    lines += ["## transcript", ""]
    # Render EVERY turn first, then drop from the middle if the whole thing is
    # over budget. The old code stopped at the budget and threw the tail away,
    # which cost us the most valuable part twice over: the end of a transcript is
    # where the solver writes its deliverable, states its conclusion and checks
    # itself, and it is also where a fatal API error appears. 55 of 1884 attempts
    # across four researcher models were cut this way, 10% of two of our own
    # batches, and on those the judge was grading `quality` from a record that
    # stopped before the model finished.
    turns: list[str] = []
    step = 0
    for r in recs:
        if r.get("type") not in ("user", "assistant"):
            continue                    # harness bookkeeping, not conversation
        msg = r.get("message") or {}
        blocks = _render_blocks(msg.get("content"), max_block_chars)
        if not blocks:
            continue
        step += 1
        body = "\n\n".join(blocks)
        turns.append(f"### step {step} — {r['type']}\n\n{body}\n")

    if turns:
        lines += _fit_middle(turns, max_total_chars)
    elif traj and (traj.get("steps") or []):
        # No session log (a non-claude-code harness, or one that did not ship it).
        # trajectory.json is the fallback and its per-step message is often empty,
        # so say that plainly rather than presenting a hollow transcript as full.
        lines += ["(no agent session log found; falling back to trajectory.json, "
                  "whose per-step messages are frequently empty)", ""]
        for s in traj.get("steps") or []:
            m = s.get("message")
            m = m if isinstance(m, str) else json.dumps(m, ensure_ascii=False)
            if not (m or "").strip():
                continue
            lines.append(f"### step {s.get('step_id', '?')} — {s.get('source') or '?'}\n\n"
                         f"{_elide(m, max_block_chars)}\n")
    else:
        lines += ["(this attempt produced no agent transcript at all: it most likely "
                  "died in setup or was killed before the agent got control)", ""]

    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(scrub("\n".join(lines)) + "\n")
    return dst


def build_rollout_bundle(raw_root: Path, task_dir: Path, bundle: Path,
                         thresh: float | None = None) -> dict:
    """Turn a directory of raw attempts into a mountable bundle + summary.

    The bundle is what every downstream model reads:

        <bundle>/README.md         what this is and how it was produced
        <bundle>/summary.json      rewards, pass rate, solved flags
        <bundle>/attempt-0.md ...  one sanitized transcript per attempt
    """
    if thresh is None:
        thresh = task_pass_threshold(task_dir)
    attempts = rollout_dirs(raw_root)
    if not attempts:
        # No raw output. Either nothing ran, or a previous driver already built
        # this bundle and cleared the raw tree. Overwriting a good summary with an
        # empty one in the second case silently erases a task's whole result set,
        # which is exactly what happened when a retry driver and the main driver
        # both finished the same task.
        existing = bundle / "summary.json"
        if existing.is_file():
            try:
                prev = json.loads(existing.read_text())
            except json.JSONDecodeError:
                prev = {}
            if prev.get("attempts"):
                log("bundle", f"{task_dir.name}: raw output already cleared and a bundle with "
                              f"{prev['attempts']} attempt(s) exists; keeping it")
                return prev
        log("bundle", f"{task_dir.name}: no raw attempt output found; writing an empty summary")
    # An attempt is identified by the number in its own directory name, never by
    # its position in this list. harness/retry.py moves a faulted attempt aside on
    # purpose, so the set can have a hole in it -- and numbering by position would
    # silently renumber everything after the hole, turning the surviving
    # `attempt-5` into `attempt-4.md`. Any earlier citation of "attempt-4" would
    # then point at different content, in a bundle the judge has already read.
    idx = [int(re.sub(r"\D", "", a.name) or 0) for a in attempts]
    rewards: list[float | None] = []
    solved: list[bool | None] = []
    terminal: list[dict] = []
    bundle.mkdir(parents=True, exist_ok=True)
    for a, i in zip(attempts, idx):
        r = read_reward(a)
        s = None if r is None else (r >= thresh)
        rewards.append(r)
        solved.append(s)
        terminal.append(classify(a))
        write_digest(a, bundle / f"attempt-{i}.md", i, r, s)

    # `rewards` / `solved` / `terminal` stay parallel to attempt-<i>.md for every
    # attempt, faults included: an index into them is an attempt number and must
    # keep meaning that. What the exclusion changes is the ARITHMETIC -- n_solved
    # and pass_rate are computed over the attempts that actually measured the
    # model, so a 429 that scored 0.0 no longer manufactures a solve-count.
    # ANY attempt that did not end cleanly is out of the arithmetic, timeouts
    # included. harbor records a reward even when it raised: the clearest specimen
    # is a customer attempt that burned its whole quota -- claude-code retried the
    # 429 ten times over 197 seconds, gave up, reported terminal_reason api_error
    # with zero input tokens, harbor raised ApiUsageLimitError, and then ran the
    # verifier anyway and stored reward 0.0. Nothing about that number describes
    # the task. A timeout is excluded for the same reason from a different
    # direction: the limit it hit is OURS, not the task's -- run_rollouts rescales
    # every task's declared [agent] timeout to one budget per benchmark, which cuts
    # terminal-bench from its declared 8 hours to 2.
    #
    # Excluded is not the same as retryable. retry.py asks terminal.is_retryable,
    # which is false for a timeout, so a timed-out attempt is dropped from the score
    # and NOT re-run: a rerun gets the same budget and hits the same wall.
    excluded = [i for i, t in zip(idx, terminal) if t["state"] != "ok"]
    no_verdict = [i for i, r in zip(idx, rewards) if r is None and i not in excluded]
    usable = [i for i, s in zip(idx, solved) if i not in excluded and s is not None]
    by_idx = dict(zip(idx, solved))
    n_solved = sum(1 for i in usable if by_idx[i])
    summary = {
        "task": task_dir.name,
        "task_name": task_dir.name,
        "attempts": len(attempts),
        "attempt_ids": idx,
        "attempts_no_verdict": len(no_verdict),
        "attempts_excluded": excluded,
        "excluded_reasons": {str(i): f"{t['state']}:{t['reason']}"
                             for i, t in zip(idx, terminal) if i in excluded},
        "terminal": terminal,
        "pass_threshold": thresh,
        "rewards": rewards,
        "solved": solved,
        "n_usable": len(usable),
        "n_solved": n_solved,
        "pass_rate": (n_solved / len(usable)) if usable else None,
    }
    if len(usable) < MIN_USABLE_ATTEMPTS:
        summary["unscorable"] = (
            f"only {len(usable)} of {len(attempts)} attempts measured the model "
            f"(excluded {excluded or 'none'}, no verdict {no_verdict or 'none'}); "
            f"the difficulty band needs at least {MIN_USABLE_ATTEMPTS}")
        summary["pass_rate"] = None
    (bundle / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")

    # A navigation index, not decoration. A full bundle is a couple of megabytes of
    # transcript -- more than fits comfortably in one context -- so whoever reads it
    # needs to know up front which attempts are worth opening and roughly how big
    # each one is, rather than discovering it by reading the first file to the end.
    rows = []
    for i, s, r, t in zip(idx, solved, rewards, terminal):
        p = bundle / f"attempt-{i}.md"
        try:
            text = p.read_text()
            size, steps = len(text), text.count("### step ")
        except OSError:
            size, steps = 0, 0
        rows.append(f"| attempt-{i}.md | {'-' if r is None else r} | "
                    f"{'unknown' if s is None else ('solved' if s else 'failed')} | "
                    f"{t['state']}{' ← excluded' if i in excluded else ''} | "
                    f"{steps} | {size // 1000} kB |")

    excl_note = ""
    if excluded:
        excl_note = (
            f"\n**{len(excluded)} attempt(s) are excluded from the arithmetic** "
            f"({', '.join('attempt-%d (%s)' % (i, summary['excluded_reasons'][str(i)]) for i in excluded)}). "
            "They stopped for a reason that has nothing to do with this task — the gateway "
            "cut them off, the container never came up, the process was killed — so their "
            "reward does not say anything about how hard the task is. They are still here to "
            "read; just do not count them.\n")

    (bundle / "README.md").write_text(
        "# Customer-model rollout bundle\n\n"
        f"The customer model attempted `{task_dir.name}` {len(attempts)} times, "
        "independently, with no memory carried between attempts. Each attempt is one file "
        "here: the verifier's own output for that attempt, then the agent's transcript — "
        "its reasoning, every tool call, and every tool result, in order.\n\n"
        f"An attempt counts as solved when its reward reaches {thresh}. "
        f"{summary['n_solved']} of {summary['n_usable']} attempts that measured the model "
        "solved it"
        + (f" ({summary['pass_rate']:.3f}).\n" if summary["pass_rate"] is not None else ".\n")
        + excl_note
        + "\n| file | reward | outcome | how it ended | steps | size |\n"
          "|---|---|---|---|---|---|\n"
        + "\n".join(rows) + "\n\n"
        + "These files are large. A workable reading order: the verifier output of a failed "
          "attempt (it names the check that failed), then that attempt's last few steps "
          "(where the answer was produced), then backwards to where the decision that caused "
          "it was made. Compare against a solved attempt if there is one — the difference "
          "between a run that worked and one that did not is usually the clearest statement "
          "of where the difficulty sits.\n\n"
          "Each file's header says how that attempt ended. `ok` means the agent finished and "
          "said so; `timeout` means it used up the time the task allows, so what it had "
          "written by then is what got graded; anything else means the run died on us and its "
          "reward is not evidence about the task.\n\n"
          "Credentials are masked. Individual tool outputs longer than a few thousand "
          "characters are elided in the middle, keeping both ends, and if the whole transcript "
          "is over budget the MIDDLE steps are dropped rather than the end — so the last steps "
          "are always present and step numbers never shift.\n"
    )
    return summary


# --------------------------------------------------------------------------- #
# running the rollouts
# --------------------------------------------------------------------------- #

def run_rollouts(hb: Harbor, task_dir: Path, model: str, raw_root: Path,
                 attempts: int, cfg: dict, benchmark: str, agent: str,
                 closed_book: bool, pool: cf.Executor | None = None,
                 tag: str = "rollout", wait: bool = True):
    """K independent attempts at one task. Attempts are the unit of parallelism.

    With a pool and wait=False, returns the futures instead of blocking on them.
    A caller rolling out several tasks must use that form and collect afterwards:
    blocking here per task caps the effective concurrency at K, whatever the pool
    size -- which is exactly what happened on the first v2 rollout (pool of 9,
    K = 6, six attempts live at a time).
    """
    budget = task_timeout_sec(cfg, benchmark)
    declared = agent_timeout_sec(task_dir, budget)
    # Scale the task's own [agent] timeout so every benchmark's attempts get the
    # same wall-clock budget regardless of what an individual task declared.
    mult = (budget / declared) if declared > 0 else 1.0
    outer = budget * float(cfg["score"]["outer_wall_task_multiplier"]) + 900

    def one(i: int) -> float | None:
        out = raw_root / f"attempt-{i}"
        if out.exists() and read_reward(out) is not None:
            log(tag, f"{task_dir.name} attempt-{i}: reusing existing result")
            return read_reward(out)
        log(tag, f"{task_dir.name} attempt-{i}: start")
        try:
            r = hb.customer(task_dir, model, out, outer, agent_timeout_mult=mult,
                            agent=agent, closed_book=closed_book)
        except Exception as e:                                  # noqa: BLE001
            log(tag, f"{task_dir.name} attempt-{i}: FAILED {type(e).__name__}: {e}")
            return None
        log(tag, f"{task_dir.name} attempt-{i}: reward={r}")
        return r

    if pool is None:
        return [one(i) for i in range(attempts)]
    futs = [pool.submit(one, i) for i in range(attempts)]
    if not wait:
        return futs
    return [f.result() for f in futs]


def clear_raw(raw_root: Path) -> None:
    """Drop raw attempt output once its bundle is built.

    Raw output is bulky and carries the gateway key; the bundle is the artifact
    worth keeping. Only call this when the bundle has been written.
    """
    if raw_root.exists():
        shutil.rmtree(raw_root)
