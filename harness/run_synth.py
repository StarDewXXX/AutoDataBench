#!/usr/bin/env python3
"""Stage 3a: the researcher authors ONE new task from ONE original task.

This is the measured capability. The researcher agent is given the sampled subset of
the benchmark, the one original task inside it that it must build from, and the
customer model's rollout bundle for that task -- rollouts for the other tasks are
deliberately withheld. It is not given the failure-mode rubric, the format rubric,
or the gate: in the real setting a data researcher does not receive the customer's
acceptance criteria, and working out what the customer actually needs from the
evidence in front of it is the capability under test.

The hygiene requirements those hidden rubrics check are a separate matter, and the
instruction states their substance directly (the solver's contract, a verifier that
runs unattended and offline, resources that fit the work). Scoring a researcher on a
convention nobody told it about measures nothing about data synthesis.

One episode = one original task -> one synthesized task. Episodes are the unit of
repetition: run the same (benchmark, task) pair several times with different
episode ids to average over the researcher's own variance, which in v1 turned out
to be larger than the differences between models.

Writes into runs/<run_id>/<benchmark>/<task>/ep<NN>/:

    task/       the rendered researcher harbor task
    job/        harbor's run output, including the researcher's trajectory
    work/out/   what the researcher delivered (one task directory, hopefully)
    synth.json  what was run, and what came out
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mounts
from common import (ROOT, Harbor, add_budget_args, add_path_args, apply_budget_args,
                    apply_path_args, benchmark_dir,
                    discover_tasks,
                    load_config, load_meta, log, render_task, resolve_harbor_bin,
                    sampled_tasks, rel, make_harbor)
from run_rollout import rollout_bundle_path

PROMPT = ROOT / "agents" / "synth"
RUNS = ROOT / "runs"


def episode_dir(run_id: str, benchmark: str, task: str, episode: int) -> Path:
    return RUNS / run_id / benchmark / task / f"ep{episode:02d}"


def band_text(cfg: dict) -> str:
    """The difficulty requirement in the researcher's own units: solves out of K.

    The band is configured as a rate, but the researcher will be counting solves
    over a handful of test runs, so the prompt states it as a count. Edges are
    inclusive, matching difficulty_score().
    """
    import math
    k = int(cfg["customer"]["attempts"])
    lo, hi = float(cfg["score"]["band_lo"]), float(cfg["score"]["band_hi"])
    lo_n = max(1, math.ceil(lo * k - 1e-9))
    hi_n = min(k - 1, math.floor(hi * k + 1e-9))
    return (f"that the customer's model solves it between {lo_n} and {hi_n} times out of "
            f"{k} (a solve rate between {lo:g} and {hi:g}, inclusive)")


def search_backend(cfg: dict) -> Path | None:
    """Absolute path of the configured web-search backend, or None.

    The script may live outside the repo (it can hold a provider key), so it is
    mounted read-only at its own host path rather than copied into the image. A
    relative path is resolved against the repository root.
    """
    raw = (cfg["researcher"].get("search_script") or "").strip()
    if not raw:
        return None
    p = Path(raw)
    if not p.is_absolute():
        p = ROOT / p
    if not p.is_file():
        log("synth", f"WARN: search_script not found: {p}; adb-search will be unavailable")
        return None
    return p.resolve()


def search_line(cfg: dict, search: Path | None) -> str:
    if search:
        return (f"  adb-search \"<query>\" [num]   Searches the web and prints, for each hit, a "
                f"title, a URL and a snippet. You have {cfg['researcher'].get('search_max_calls', 100)} "
                f"searches for the session; `adb-search --status` shows what is left. Every query is "
                f"logged and read by the judge alongside your trajectory.\n\n"
                f"You also have ordinary web access from your own tools. Use the web the way a "
                f"researcher would -- to check a constant, confirm a method, see whether a problem "
                f"you are designing already exists -- and then write your own task.")
    return ("You have ordinary web access from your own tools. Use the web the way a researcher "
            "would -- to check a constant, confirm a method, see whether a problem you are designing "
            "already exists -- and then write your own task.")


def synthesize(hb: Harbor, cfg: dict, benchmark: str, task_dir: Path,
               ep_dir: Path, episode: int) -> dict:
    task = task_dir.name
    bundle = rollout_bundle_path(cfg, benchmark, task)
    if not (bundle / "summary.json").is_file():
        raise SystemExit(f"{task}: no rollout bundle at {bundle}; run run_rollout.py first")
    summary = json.loads((bundle / "summary.json").read_text())

    # Authoring is the single most expensive step in an episode -- a 45-minute
    # researcher budget -- so a driver restarted after a crash must not pay for it
    # again. Reuse whatever was already delivered into this episode directory.
    prev_path = ep_dir / "synth.json"
    if prev_path.is_file():
        try:
            prev = json.loads(prev_path.read_text())
        except json.JSONDecodeError:
            prev = {}
        if prev.get("n_delivered"):
            log("synth", f"{task} ep{episode:02d}: reusing the delivered task(s) "
                         f"{prev.get('delivered')} from the earlier run; not re-authoring")
            return prev

    # An episode that was authored before and has nothing reusable is NOT re-authored
    # here. It used to be: the branch above fell through, the researcher ran again
    # into the same directory, and work/out still held whatever the dead session had
    # written. That is how one delivery becomes two tasks, and "the delivery is not
    # one task" is gate 0 by definition -- so the episode is destroyed by the rerun
    # rather than by the researcher. It cost glm-5.3 three episodes on 2026-09-11.
    #
    # Whether to re-author is also not a decision this function can make correctly.
    # A researcher that used its whole budget and delivered nothing has produced a
    # result, not a fault, and must not be handed a second budget; one that died on a
    # gateway error must be redone. Telling those apart means reading how harbor's
    # run ended, which is harness/terminal.py's job, and acting on it means clearing
    # the episode first, which is harness/retry.py's. So this stops and says so.
    #
    # The condition is the presence of an earlier harbor job, not a missing
    # synth.json: the most dangerous window is the one between harbor returning and
    # synth.json being written, where a delivery is already on disk and nothing
    # records it.
    job_root = ep_dir / "job"
    authored = sorted(p.name for p in job_root.glob("2026-*") if p.is_dir()) \
        if job_root.is_dir() else []
    out_dir = ep_dir / "work" / "out"
    residue = sorted(p.name for p in out_dir.glob("*")) if out_dir.is_dir() else []
    if authored or residue:
        raise SystemExit(
            f"{benchmark}/{task} ep{episode:02d}: already authored "
            f"({len(authored)} harbor job(s) {authored}, work/out holds {residue}) "
            f"with no reusable delivery; not re-authoring in place, because the dead "
            f"session's files would be left beside the new ones. Check how it ended -- "
            f"a timeout is a result and stays as it is, a fault gets redone:\n"
            f"    python3 harness/retry.py --run-id {ep_dir.parts[-4]} "
            f"--benchmark {benchmark} --task {task} --episode {episode} --stage synth\n"
            f"and add --apply to move the episode aside and author it from scratch.")

    work = ep_dir / "work"
    out = work / "out"
    testrun = work / "testruns"
    tmp = work / ".tmp"
    for d in (out, testrun, tmp):
        d.mkdir(parents=True, exist_ok=True)
    job_out = ep_dir / "job"

    res = cfg["researcher"]
    budget_sec = float(res["budget_min"]) * 60
    mult = budget_sec / 1800.0                       # task.toml declares 1800s

    search = search_backend(cfg)
    # The whole sampled subset, not just the one task. A researcher that has only
    # ever seen one task has to guess what the benchmark is; with the subset it can
    # read what kind of work this benchmark actually contains, which is what
    # "a task good enough to be added to this benchmark" is measured against.
    # Rollouts stay per-task: only the target task's transcripts are mounted.
    bench_dir = benchmark_dir(benchmark)
    rendered = render_task(PROMPT, ep_dir / "task", {
        "__BAND_TEXT__": band_text(cfg),
        "__SEARCH_LINE__": search_line(cfg, search),
        "__BENCHMARK__": benchmark,
        "__BENCHMARK_DIR__": str(bench_dir.resolve()),
        "__ORIGINAL_DIR__": str(task_dir.resolve()),
        "__ROLLOUT_DIR__": str(bundle.resolve()),
        "__OUT__": str(out.resolve()),
        "__WORK__": str(work.resolve()),
        "__ATTEMPTS__": str(summary.get("attempts", 0)),
        "__ALLOWED_MODEL__": cfg["customer"]["model"],
        "__BUDGET_MIN__": str(res["budget_min"]),
        # Only environment/codex-config.toml reads this, and only a codex
        # researcher reads that. Substituted unconditionally so the gateway can
        # never differ between the agent's own route and the config it was
        # built with.
        "__GATEWAY_BASE_URL__": cfg["gateway"]["base_url"].rstrip("/"),
    })

    # bench_dir first: task_dir is inside it, so mounts.build folds the two into one
    # read-only mount instead of rejecting the duplicate target.
    mnt = mounts.build(hb.bin, ro=[bench_dir, task_dir, bundle] + ([search] if search else []),
                       rw=[work])
    agent_env = [
        f"ADB_WORK={work.resolve()}",
        f"ADB_OUT={out.resolve()}",
        f"ADB_TESTRUN_OUT={testrun.resolve()}",
        f"ADB_BENCHMARK_DIR={bench_dir.resolve()}",
        f"ADB_ORIGINAL_DIR={task_dir.resolve()}",
        f"ADB_ROLLOUT_DIR={bundle.resolve()}",
        f"ADB_ALLOWED_MODEL={cfg['customer']['model']}",
        f"ADB_MAX_CONCURRENT={res['max_concurrent_docker']}",
        f"ADB_DEADLINE_EPOCH={int(time.time() + budget_sec)}",
        f"TMPDIR={tmp.resolve()}",
        f"HARBOR_REAL={hb.bin}",
        f"ANTHROPIC_BASE_URL={hb.base_url}",
        f"ADB_CC_VERSION={cfg.get('claude_code_version') or ''}",
        f"ADB_SEARCH_SCRIPT={search or ''}",
        f"ADB_SEARCH_MAX_CALLS={res.get('search_max_calls', 100)}",
    ]

    log("synth", f"{benchmark}/{task} ep{episode:02d}: researcher "
                 f"{res['agent']}/{res['model']}, {res['budget_min']} min")
    t0 = time.time()
    # claude-code has the web through its own tools, so web_access needs no flag
    # there. codex ships its search switched off, so the same permission has to be
    # turned on explicitly or the two harnesses would be compared with different
    # tool sets -- which measures the harness, not the researcher.
    ak: list[str] = []
    if res["agent"] == "codex" and res.get("web_access"):
        ak.append("web_search=live")
    # Reasoning effort has to come through here and not through
    # environment/codex-config.toml: harbor's codex adapter declares it as a CLI flag
    # with default "high" and puts `-c model_reasoning_effort=high` on the command
    # line, which overrides the config file. A `model_reasoning_effort` written into
    # the toml is silently dead -- measured 2026-09-15, with the toml saying `max` and
    # codex's own session still recording "effort":"high". So `high` in the 48
    # episodes of runs/20260913-gpt56-alb1 is harbor's default, not a choice codex
    # made; leaving researcher.reasoning_effort unset reproduces it exactly.
    if res["agent"] == "codex" and res.get("reasoning_effort"):
        ak.append(f"reasoning_effort={res['reasoning_effort']}")
    rc = hb.agent_task(rendered, res["agent"], res["model"], job_out,
                       budget_sec * 1.5 + 900, mnt, agent_env=agent_env,
                       agent_timeout_mult=mult, agent_kwargs=ak)
    elapsed = time.time() - t0

    delivered = discover_tasks(out)
    rec = {
        "benchmark": benchmark,
        "original_task": task,
        "episode": episode,
        "researcher": {"agent": res["agent"], "model": res["model"],
                       "budget_min": res["budget_min"]},
        "harbor_rc": rc,
        "elapsed_sec": round(elapsed, 1),
        "delivered": [str(d.relative_to(out)) for d in delivered],
        "n_delivered": len(delivered),
        "original_rollout": {k: summary.get(k) for k in
                             ("attempts", "n_usable", "n_solved", "pass_rate",
                              "pass_threshold", "attempts_excluded")},
    }
    (ep_dir / "synth.json").write_text(json.dumps(rec, indent=2, ensure_ascii=False) + "\n")

    if len(delivered) == 0:
        log("synth", f"{task} ep{episode:02d}: delivered NOTHING (rc={rc}) after {elapsed/60:.1f} min")
    elif len(delivered) > 1:
        # Not fixed up here: the contract is one task, and quietly picking one
        # would hide a researcher that ignored the contract. The gate handles it.
        log("synth", f"{task} ep{episode:02d}: delivered {len(delivered)} tasks "
                     f"(contract is one): {rec['delivered']}")
    else:
        log("synth", f"{task} ep{episode:02d}: delivered {rec['delivered'][0]} "
                     f"in {elapsed/60:.1f} min")
    return rec


def main() -> int:
    ap = argparse.ArgumentParser(description="stage 3a: one original task -> one synthesized task")
    ap.add_argument("--benchmark", required=True)
    ap.add_argument("--task", action="append", default=None,
                    help="original task directory name; repeatable. default: all sampled tasks")
    ap.add_argument("--episode", type=int, default=1, help="episode index (repeat to average variance)")
    ap.add_argument("--run-id", default=None, help="default: a timestamp")
    ap.add_argument("--config", default=None)
    ap.add_argument("--dry-run", action="store_true")
    add_budget_args(ap)
    add_path_args(ap)
    args = ap.parse_args()
    apply_path_args(args)

    cfg = load_config(args.config)
    apply_budget_args(args, cfg)
    bench = args.benchmark
    load_meta(bench)
    tasks = sampled_tasks(bench)
    if args.task:
        want = set(args.task)
        tasks = [t for t in tasks if t.name in want]
        missing = want - {t.name for t in tasks}
        if missing:
            raise SystemExit(f"no such sampled task(s) in {bench}: {sorted(missing)}")

    run_id = args.run_id or time.strftime("%Y%m%d-%H%M%S")
    if args.dry_run:
        for t in tasks:
            log("synth", f"    would author from {t.name} -> "
                         f"{rel(episode_dir(run_id, bench, t.name, args.episode))}")
        return 0

    hb = make_harbor(cfg, float(cfg["researcher"]["agent_setup_timeout_mult"]))
    for t in tasks:
        ep = episode_dir(run_id, bench, t.name, args.episode)
        ep.mkdir(parents=True, exist_ok=True)
        synthesize(hb, cfg, bench, t, ep, args.episode)
    log("synth", f"run_id={run_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
