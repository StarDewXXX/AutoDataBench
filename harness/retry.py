#!/usr/bin/env python3
"""Find the runs in a batch that died on us, and redo exactly those.

Not an automatic retry. A run that failed on the gateway usually failed because
the gateway was unhappy for a while, so retrying inside the driver just burns the
budget faster; and an automatic retry is the thing that produced the worst bug in
this project's history (see "double delivery" below). So this is a separate pass
you run after a batch: it reports what is broken, and only with --apply does it
touch anything.

WHAT COUNTS AS BROKEN is harness/terminal.py's job, not this file's. Its three
states map onto three actions:

  ok        leave alone.
  timeout   leave alone. The agent used up the time it was given; a rerun hits the
            same wall. For a researcher that is the budget working; for a customer
            attempt it is a real failure and the reward stands.
  abnormal  redo. Gateway throttling, a dropped connection, a container that never
  unknown   came up, a killed process, or an output tree too incomplete to judge.

THREE STAGES, THREE UNITS OF WORK:

  synth       the whole episode. A researcher session cannot be resumed, so the
              episode directory is moved aside and re-authored from scratch.
  difficulty  ONE customer attempt. score/.raw/attempt-<i> is moved aside and only
              that index is re-run; run_rollouts already reuses the attempts that
              have a reward. The bundle and score.json go with it because both are
              derived from the old attempt set.
  judge       the judge run. score/judge is moved aside and the episode re-scored.

DOUBLE DELIVERY, the trap this script exists to avoid. When harbor's own retry
re-ran a researcher session it did not clear work/out/, so the first session's
half-finished task was still sitting there when the second one wrote its task
beside it. score_episode then saw two directories, and "the delivery is not one
task" is gate 0 by definition -- the episode was destroyed by the retry, not by
the researcher. It happened to glm-5.3 on retro-console-soc/ep01, react-lead-form
/ep01 and geometric-pharmacophore-alignment/ep01 on 2026-09-11. The lesson is that
partial cleanup of an episode is not safe, so a synth retry moves the ENTIRE
episode directory and never picks pieces out of it.

Both automatic paths into that trap are now closed, which is why this script is the
only way a researcher session gets redone. Harbor.run() passes --max-retries 0
explicitly, and run_synth.synthesize() refuses to re-author an episode that already
holds a harbor job, raising with the retry.py command line to use instead. So an
episode that needs re-authoring waits here for a human, deliberately.

NOTHING IS DELETED. Everything goes to runs/_failed/<run>/<bench>/<task>/... The
name matters: every glob in this codebase and in our analysis scripts looks for
`runs/2026*` or `<task>/ep*`, and `_failed` matches neither, so a moved-aside
carcass is invisible to all of them while staying readable for an audit. Renaming
in place (ep01.failed-...) would be matched by `ep*` and is exactly the kind of
thing that silently corrupts a later analysis.

Usage:
    python3 harness/retry.py --run-id 20260913-x                    # report only
    python3 harness/retry.py --run-id 20260913-x --apply            # move + rerun
    python3 harness/retry.py --run-id 20260913-x --apply --no-rerun  # move only
    python3 harness/retry.py --run-id 20260913-x --stage difficulty --apply
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT, log, read_reward, rollout_dirs
from terminal import classify, is_retryable

RUNS = ROOT / "runs"
FAILED = RUNS / "_failed"
EP_RE = re.compile(r"^ep(\d+)$")


# --------------------------------------------------------------------------- #
# survey
# --------------------------------------------------------------------------- #

def episodes(run_id: str, benchmark: str | None, tasks: list[str] | None,
             episode: int | None) -> list[Path]:
    root = RUNS / run_id
    if not root.is_dir():
        raise SystemExit(f"no such run: {root}")
    out = []
    for ep in sorted(root.glob("*/*/ep*")):
        if not ep.is_dir() or not EP_RE.match(ep.name):
            continue
        if benchmark and ep.parts[-3] != benchmark:
            continue
        if tasks and ep.parts[-2] not in tasks:
            continue
        if episode is not None and int(EP_RE.match(ep.name).group(1)) != episode:
            continue
        out.append(ep)
    return out


def newest_job(job_root: Path) -> Path | None:
    """The latest harbor job under this root, by its NAME.

    Job directories are named `2026-09-11__02-24-27`, which sorts lexicographically
    in time order and cannot drift. mtime can: a directory's mtime moves whenever
    anything is written under it, so an older job whose files were touched later
    sorts first -- which made a session that harbor retried successfully look like
    the failure it retried away from.
    """
    jobs = sorted((p for p in job_root.glob("2026-*") if p.is_dir()),
                  key=lambda p: p.name)
    return jobs[-1] if jobs else None


def survey_episode(ep: Path, want_attempts: int | None = None) -> dict:
    """Everything wrong with one episode, with no side effects."""
    rec: dict = {"episode": ep, "synth": None, "difficulty": [], "judge": None,
                 "flags": [], "incomplete": []}

    # --- synth
    job = newest_job(ep / "job")
    if job is None:
        rec["synth"] = {"state": "unknown", "reason": "no-job-dir", "detail": ""}
    else:
        rec["synth"] = classify(job)
    n_jobs = len([p for p in (ep / "job").glob("2026-*") if p.is_dir()]) if (ep / "job").is_dir() else 0
    if n_jobs > 1:
        rec["flags"].append(f"{n_jobs} job dirs: harbor retried this session itself, "
                            f"so work/out/ may hold a stale half-delivery")

    # --- delivery shape, the double-delivery check
    out = ep / "work" / "out"
    delivered = sorted(p for p in out.glob("*") if p.is_dir()) if out.is_dir() else []
    rec["n_delivered"] = len(delivered)
    if rec["synth"]["state"] == "ok" and len(delivered) != 1:
        rec["flags"].append(f"delivered {len(delivered)} task dirs, not 1"
                            + (f": {[p.name for p in delivered]}" if delivered else ""))

    # --- difficulty attempts
    raw = ep / "score" / ".raw"
    for a in rollout_dirs(raw) if raw.is_dir() else []:
        c = classify(a)
        rec["difficulty"].append({"dir": a, "index": int(re.sub(r"\D", "", a.name) or 0),
                                  "reward": read_reward(a), **c})
    if not raw.is_dir() and (ep / "score" / "rollout" / "summary.json").is_file():
        rec["flags"].append("rollout bundle exists but raw output is gone (pre-2026-09-13 run); "
                            "attempt-level retry is not possible, only a full re-score")

    # --- judge
    jjob = newest_job(ep / "score" / "judge" / "job")
    if jjob is not None:
        rec["judge"] = classify(jjob)

    # --- work left half-done, by us. An earlier --no-rerun pass moves an attempt
    # aside and leaves a gap; a re-score is what fills it. Without this check the
    # gap is invisible on the next pass -- there is nothing left to classify -- and
    # the episode sits at five attempts with no score, looking clean. The whole
    # point of this script is that a batch can be checked for completeness, so it
    # has to notice work it started and did not finish.
    if rec["synth"]["state"] == "ok" and rec["n_delivered"] == 1:
        if raw.is_dir() and want_attempts and len(rec["difficulty"]) < want_attempts:
            rec["incomplete"].append(
                f"{len(rec['difficulty'])} of {want_attempts} attempts present")
        if raw.is_dir() and not (ep / "score" / "rollout" / "summary.json").is_file():
            rec["incomplete"].append("raw attempts present but no rollout bundle")
        if not (ep / "score.json").is_file() and (
                raw.is_dir() or (ep / "score" / "rollout" / "summary.json").is_file()):
            rec["incomplete"].append("rolled out but never scored")

    return rec


def actions(rec: dict, stage: str, suspect_zeros: bool = False) -> list[dict]:
    """The moves this episode needs. One dict per path to move aside."""
    ep = rec["episode"]
    acts: list[dict] = []
    # A timeout is never retried, whatever it left behind: the researcher had its
    # whole budget and a rerun gets the same budget. An empty delivery after a full
    # 45 minutes is a result, not a fault. What DOES need re-authoring is a delivery
    # whose shape is wrong for some other reason -- above all the two-task delivery
    # harbor's own retry produces, where the session itself reports success.
    # The one exception to "a timeout is never retried": if the episode also has
    # more than one job dir, the delivery was corrupted by harbor's own retry
    # leaving residue in work/out, and that is a fault whatever the final session
    # then did with its budget. glm-5.3's geometric-pharmacophore-alignment/ep01 is
    # this shape -- rate-limited, retried, then timed out, with two tasks delivered.
    timed_out = rec["synth"]["state"] == "timeout"
    retried_by_harbor = any("job dirs" in f for f in rec["flags"])
    synth_broken = is_retryable(rec["synth"]["state"]) or (
        rec["n_delivered"] != 1 and (not timed_out or retried_by_harbor))
    if stage in ("all", "synth") and synth_broken:
        # The whole episode: a researcher session is not resumable, and picking
        # pieces out of work/out is how the double delivery happened.
        why = (rec["synth"]["reason"] if is_retryable(rec["synth"]["state"])
               else f"delivered {rec['n_delivered']} task dirs, not 1")
        acts.append({"kind": "synth", "path": ep, "why": why})
        return acts                      # re-authoring redoes everything downstream

    if stage in ("all", "difficulty"):
        bad = [d for d in rec["difficulty"] if is_retryable(d["state"])]
        if suspect_zeros:
            # Treat EVERY zero-reward attempt as suspect, not just the ones we can
            # prove were faults. Read the warning in actions()'s caller before using
            # this: resampling only the attempts that scored zero, while keeping the
            # ones that scored, raises pass_rate on any task the model does not
            # always solve, and pushes real band landings out on the easy side.
            for d in rec["difficulty"]:
                if d["reward"] == 0.0 and d not in bad:
                    bad.append(dict(d, reason=f"reward 0 treated as suspect "
                                             f"(ended {d['state']})"))
            bad.sort(key=lambda d: d["index"])
        for d in bad:
            acts.append({"kind": "attempt", "path": d["dir"], "why": d["reason"]})
        if bad:
            # Both are derived from the old attempt set. build_rollout_bundle
            # deliberately keeps an existing summary when it finds no raw output,
            # so a stale bundle left in place would be reused instead of rebuilt.
            for p in (ep / "score" / "rollout", ep / "score.json"):
                if p.exists():
                    acts.append({"kind": "derived", "path": p,
                                 "why": f"derived from attempt(s) {[d['index'] for d in bad]}"})

    if stage in ("all", "judge") and rec["judge"] and is_retryable(rec["judge"]["state"]):
        acts.append({"kind": "judge", "path": ep / "score" / "judge",
                     "why": rec["judge"]["reason"]})
        if (ep / "score.json").exists() and not any(
                a["path"] == ep / "score.json" for a in acts):
            acts.append({"kind": "derived", "path": ep / "score.json",
                         "why": "derived from the judge verdict"})

    # Nothing to move, but the episode still needs the scoring pass finished. A
    # zero-move action so the rerun is still planned and printed.
    if not acts and stage in ("all", "difficulty") and rec["incomplete"]:
        acts.append({"kind": "resume", "path": None,
                     "why": "; ".join(rec["incomplete"])})
    return acts


# --------------------------------------------------------------------------- #
# moving things, carefully
# --------------------------------------------------------------------------- #

def _check_safe(path: Path, run_id: str) -> Path:
    """Refuse to move anything that is not a known artifact of THIS run.

    Four independent conditions, because a mistake here destroys measurements
    that cost real money: the path must exist, must resolve inside
    runs/<run_id>/, must not be the run root itself, and must match one of the
    exact shapes we know how to redo.
    """
    p = path.resolve()
    run_root = (RUNS / run_id).resolve()
    if not p.exists():
        raise SystemExit(f"refusing to move a path that does not exist: {p}")
    if run_root not in p.parents:
        raise SystemExit(f"refusing to move {p}: not inside {run_root}")
    if p == run_root:
        raise SystemExit(f"refusing to move the run root itself: {p}")
    rel = p.relative_to(run_root)
    parts = rel.parts
    ok = (
        (len(parts) == 3 and EP_RE.match(parts[2]))                              # ep dir
        or (len(parts) == 6 and parts[3:5] == ("score", ".raw")
            and parts[5].startswith("attempt-"))                                 # one attempt
        or (len(parts) == 5 and parts[3:5] == ("score", "rollout"))               # bundle
        or (len(parts) == 5 and parts[3:5] == ("score", "judge"))                 # judge
        or (len(parts) == 4 and parts[3] == "score.json")                         # score
    )
    if not ok:
        raise SystemExit(f"refusing to move {rel}: not a shape retry.py knows how to redo")
    return p


def move_aside(path: Path, run_id: str, stamp: str, apply: bool) -> Path:
    p = _check_safe(path, run_id)
    rel = p.relative_to((RUNS / run_id).resolve())
    dst = FAILED / run_id / rel.parent / f"{p.name}.{stamp}"
    n = 1
    while dst.exists():
        n += 1
        dst = FAILED / run_id / rel.parent / f"{p.name}.{stamp}-{n}"
    if apply:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(p), str(dst))
    return dst


def _own_pids() -> set[int]:
    """This process and every ancestor of it.

    Without this the busy check matches ITSELF: the shell that launched retry.py
    has the run id on its command line, and a compound command mentioning any
    driver filename supplies the second half of the pattern. That is the same
    self-match that made an earlier `pkill` kill its own shell -- a scan for
    "processes like me" must always exclude me.
    """
    pids = set()
    pid = os.getpid()
    for _ in range(20):                  # bounded: never loop on a cycle
        if pid <= 1 or pid in pids:
            break
        pids.add(pid)
        try:
            pid = int(Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1].split()[1])
        except (OSError, IndexError, ValueError):
            break
    return pids


def run_id_busy(run_id: str) -> list[str]:
    """Live processes working on this run. Retrying under them corrupts both."""
    try:
        ps = subprocess.run(["ps", "-eo", "pid=,args="], capture_output=True,
                            text=True, timeout=30).stdout
    except Exception:                                                # noqa: BLE001
        return []
    mine = _own_pids()
    hits = []
    for line in ps.splitlines():
        line = line.strip()
        head, _, args = line.partition(" ")
        try:
            if int(head) in mine:
                continue
        except ValueError:
            pass
        if "retry.py" in args:           # another survey is not a driver
            continue
        if run_id in args and ("harbor run" in args or "run_episode.py" in args
                               or "run_score.py" in args or "run_synth.py" in args):
            hits.append(line[:150])
    return hits


# --------------------------------------------------------------------------- #
# reruns
# --------------------------------------------------------------------------- #

def _python() -> str:
    """The interpreter the drivers are normally launched with.

    NOT sys.executable: retry.py may well be started by whatever python is first
    on PATH (a uv-managed 3.13, in the case that caught this), while the drivers
    have only ever been run from the conda env that also holds the harbor CLI.
    Handing the reruns a different interpreter than the batch used is a difference
    we would then have to rule out by hand.
    """
    cand = Path.home() / "miniconda3/envs/agent/bin/python3"
    return str(cand) if cand.exists() else sys.executable


def rerun_cmd(ep: Path, run_id: str, config: str | None, kind: str) -> list[str]:
    bench, task, epname = ep.parts[-3], ep.parts[-2], ep.name
    n = int(EP_RE.match(epname).group(1))
    cmd = [_python(), str(Path(__file__).resolve().parent / "run_episode.py"),
           "--benchmark", bench, "--run-id", run_id,
           "--first-episode", str(n), "--episodes", "1", "--task", task]
    if config:
        cmd += ["--config", config]
    cmd += ["--synth-only"] if kind == "synth" else ["--score-only"]
    return cmd


def main() -> int:
    ap = argparse.ArgumentParser(description="redo the runs in a batch that died on us")
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--benchmark", default=None)
    ap.add_argument("--task", action="append", default=None)
    ap.add_argument("--episode", type=int, default=None)
    ap.add_argument("--stage", choices=["all", "synth", "difficulty", "judge"], default="all")
    ap.add_argument("--apply", action="store_true",
                    help="actually move things aside (default: report only)")
    ap.add_argument("--no-rerun", action="store_true",
                    help="move aside but do not launch the reruns; prints the commands")
    ap.add_argument("--config", default=None)
    ap.add_argument("--suspect-zeros", action="store_true",
                    help="redo EVERY attempt whose reward is 0, not only the ones proven to "
                         "be faults. Raises pass_rate on any task the model does not always "
                         "solve -- see the warning printed when it is used")
    ap.add_argument("--force-while-running", action="store_true",
                    help="proceed even though processes are still working on this run")
    args = ap.parse_args()

    busy = run_id_busy(args.run_id)
    if busy and not args.force_while_running:
        print(f"{len(busy)} process(es) are still working on {args.run_id}:", file=sys.stderr)
        for b in busy[:8]:
            print("  " + b, file=sys.stderr)
        raise SystemExit("refusing to touch a run that is still going. Wait for it, or pass "
                         "--force-while-running if you are certain.")

    eps = episodes(args.run_id, args.benchmark, args.task, args.episode)
    if not eps:
        raise SystemExit("no episodes matched")

    if args.suspect_zeros:
        print("--suspect-zeros: every attempt that scored 0 will be redone, including the\n"
              "  ones that ended cleanly. This is NOT a neutral correction. Resampling only\n"
              "  the attempts that failed, while keeping the ones that passed, raises the\n"
              "  measured solve rate on any task the model does not always solve: a task with\n"
              "  a true rate of 0.5 has three zeros in six, and redoing those three returns\n"
              "  about 1.5 solves, so 3/6 = 0.500 is measured as 4.5/6 = 0.750 -- out of the\n"
              "  band on the easy side. Report the before and after pass rates side by side,\n"
              "  and prefer plain --stage difficulty unless you have reason to distrust every\n"
              "  zero in this batch.\n", file=sys.stderr)

    # Read the config as plain JSON rather than through load_config: that resolves
    # the `env:` credential placeholder and exits if the key is unset, and a
    # read-only survey must not need credentials to run.
    want_attempts = None
    cfg_path = Path(args.config) if args.config else ROOT / "configs" / "default.json"
    try:
        want_attempts = int(json.loads(cfg_path.read_text())["customer"]["attempts"])
    except Exception as e:                                           # noqa: BLE001
        print(f"note: could not read customer.attempts from {cfg_path} ({e}); "
              f"a short attempt set will not be detected", file=sys.stderr)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    plan: list[tuple[Path, str, list[dict]]] = []
    n_ok = 0
    for ep in eps:
        rec = survey_episode(ep, want_attempts)
        acts = actions(rec, args.stage, args.suspect_zeros)
        label = "/".join(ep.parts[-3:])
        d_states = {}
        for d in rec["difficulty"]:
            d_states[d["state"]] = d_states.get(d["state"], 0) + 1
        summary = (f"synth={rec['synth']['state']}"
                   f"{'/' + rec['synth']['reason'] if rec['synth']['state'] != 'ok' else ''}"
                   f"  attempts={d_states or 'none'}"
                   f"  judge={rec['judge']['state'] if rec['judge'] else 'not run'}"
                   f"  delivered={rec['n_delivered']}")
        if not acts:
            n_ok += 1
            print(f"  ok   {label:52} {summary}")
            continue
        print(f"  REDO {label:52} {summary}")
        for f in rec["flags"] + rec["incomplete"]:
            print(f"       flag: {f}")
        plan.append((ep, "synth" if acts[0]["kind"] == "synth" else "score", acts))

    print(f"\n{n_ok} episode(s) clean, {len(plan)} need work.")
    if not plan:
        return 0

    moves = [(ep, a) for ep, _, acts in plan for a in acts if a["path"] is not None]
    if moves:
        print("\nwould move aside:" if not args.apply else "\nmoving aside:")
        for ep, a in moves:
            dst = move_aside(a["path"], args.run_id, stamp, args.apply)
            print(f"  {a['kind']:8} {a['path'].relative_to(RUNS / args.run_id)}"
                  f"  ->  {dst.relative_to(RUNS)}      [{a['why']}]")
    else:
        print("\nnothing to move aside; the work below only needs finishing.")

    cmds = []
    for ep, kind in ((ep, kind) for ep, kind, _ in plan):
        cmds.append(rerun_cmd(ep, args.run_id, args.config, kind))
    print("\nreruns:" if args.apply else "\nwould rerun:")
    for c in cmds:
        print("  " + " ".join(c))

    if not args.apply:
        print("\nnothing was changed. Re-run with --apply to move things aside"
              " and launch the reruns.")
        return 0
    if args.no_rerun:
        print("\n--no-rerun: paths were moved aside, nothing was launched.")
        return 0

    rc_total = 0
    for c in cmds:
        log("retry", "launching: " + " ".join(c))
        rc = subprocess.run(c, cwd=str(ROOT)).returncode
        if rc != 0:
            log("retry", f"rerun exited {rc}: {' '.join(c)}")
            rc_total = rc
    return rc_total


if __name__ == "__main__":
    raise SystemExit(main())
