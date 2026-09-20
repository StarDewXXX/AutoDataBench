#!/usr/bin/env python3
"""Run whole episodes end to end: synthesize, then score.

An episode is one original task turned into one new task and scored. This driver
exists because episodes are the unit of repetition: the point of v2's one-task-at-
a-time design is that the same (benchmark, original task) pair can be run many
times, and the spread across repeats is itself a number we need. v1's lesson was
that the researcher's run-to-run variance exceeded the differences between the
models being compared, so a single episode per task cannot separate them.

  # one episode per sampled task, 3 repeats
  python3 harness/run_episode.py --benchmark tb-science --episodes 3

  # just one task, one episode, in an existing run
  python3 harness/run_episode.py --benchmark tb-science --task dna-storage-codec \
      --episodes 1 --run-id 20260908-1200

Episodes run concurrently up to --episode-concurrency; within one episode the
phases are strictly ordered (synthesize, then roll out, then judge, because each
needs the previous one's output). Two separate caps bound the container count:
--episode-concurrency times researcher.max_concurrent_docker bounds the nested
containers a researcher can spawn, and score.n_concurrent bounds the customer
attempts across all episodes of this driver. Running episodes one at a time is
correct but far too slow: on tb-science one episode is ~45 min of authoring plus
up to 2 h of rollouts plus ~40 min of judging, so eight of them serially is a day
and a half.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (ROOT, Harbor, add_budget_args, add_path_args, apply_budget_args,
                    apply_path_args, load_config, load_meta, log, resolve_harbor_bin,
                    sampled_tasks, rel, make_harbor)
from run_analyst import rubric_path
from run_rollout import rollout_bundle_path
from run_score import score_episode
from run_synth import episode_dir, synthesize


def preflight(cfg: dict, benchmark: str, tasks: list[Path]) -> None:
    """Refuse to start when the prep stages have not been run.

    Checked up front for the whole set rather than per task: discovering halfway
    through a multi-hour run that half the rubrics are missing wastes the half
    that already ran.
    """
    missing_rollout = [t.name for t in tasks
                       if not (rollout_bundle_path(cfg, benchmark, t.name) / "summary.json").is_file()]
    missing_rubric = [t.name for t in tasks if not rubric_path(cfg, benchmark, t.name).is_file()]
    if missing_rollout:
        raise SystemExit(
            f"{benchmark}: no customer rollout bundle for {missing_rollout}.\n"
            f"  run: python3 harness/run_rollout.py --benchmark {benchmark}")
    if missing_rubric:
        raise SystemExit(
            f"{benchmark}: no rubric for {missing_rubric}.\n"
            f"  run: python3 harness/run_analyst.py --benchmark {benchmark}")


def main() -> int:
    ap = argparse.ArgumentParser(description="run episodes end to end: synthesize then score")
    ap.add_argument("--benchmark", required=True)
    ap.add_argument("--task", action="append", default=None)
    ap.add_argument("--episodes", type=int, default=1, help="repeats per task")
    ap.add_argument("--first-episode", type=int, default=1,
                    help="episode index to start at (to add repeats to an existing run)")
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--config", default=None)
    ap.add_argument("--synth-only", action="store_true")
    ap.add_argument("--score-only", action="store_true",
                    help="score episodes that already exist under --run-id")
    ap.add_argument("--rollout-only", action="store_true",
                    help="like --score-only but stop after the customer attempts: measure "
                         "difficulty, finalise the out-of-band episodes at 0, and leave the "
                         "in-band ones for a later --score-only pass to judge. Use it to keep "
                         "every trainee container off the box before any judge starts")
    ap.add_argument("--episode-concurrency", type=int, default=1,
                    help="episodes to run at once (default 1). Each concurrent episode can "
                         "spawn up to researcher.max_concurrent_docker nested containers "
                         "while authoring, so raise this and that cap together, not either alone")
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
    preflight(cfg, bench, tasks)

    run_id = args.run_id or time.strftime("%Y%m%d-%H%M%S")
    eps = list(range(args.first_episode, args.first_episode + args.episodes))
    log("episode", f"run_id={run_id}  {bench}: {len(tasks)} task(s) x {len(eps)} episode(s) "
                   f"= {len(tasks) * len(eps)} episode(s)")

    hb_synth = make_harbor(cfg, float(cfg["researcher"]["agent_setup_timeout_mult"]))
    hb_score = make_harbor(cfg, float(cfg["score"]["setup_timeout_mult"]))

    # Which models produced this run. Without it a report is uninterpretable: the
    # difficulty term depends on the customer model and quality on its rubric.
    run_root = ROOT / "runs" / run_id
    run_root.mkdir(parents=True, exist_ok=True)
    (run_root / "run.json").write_text(json.dumps({
        "run_id": run_id,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "benchmark": bench,
        "tasks": [t.name for t in tasks],
        "episodes": eps,
        "episode_concurrency": args.episode_concurrency,
        "customer": cfg["customer"],
        "researcher": cfg["researcher"],
        "judge": cfg["judge"],
        "score": cfg["score"],
        "prep_root": rel(rollout_bundle_path(cfg, bench, tasks[0].name).parents[1]) if tasks else "",
        "rubrics_root": rel(rubric_path(cfg, bench, tasks[0].name).parents[2]) if tasks else "",
    }, indent=2, ensure_ascii=False) + "\n")

    jobs = [(ep_i, t) for ep_i in eps for t in tasks]
    n_ep = max(1, args.episode_concurrency)
    log("episode", f"episode concurrency {n_ep}, customer-attempt pool "
                   f"{cfg['score']['n_concurrent']}, researcher nested-docker cap "
                   f"{cfg['researcher']['max_concurrent_docker']}")

    records: list[dict] = []
    lock = threading.Lock()

    def one_episode(job) -> None:
        ep_i, t = job
        ep = episode_dir(run_id, bench, t.name, ep_i)
        ep.mkdir(parents=True, exist_ok=True)
        if not (args.score_only or args.rollout_only):
            try:
                synthesize(hb_synth, cfg, bench, t, ep, ep_i)
            except SystemExit as e:
                log("episode", f"{t.name} ep{ep_i:02d}: synthesis FAILED {e}")
                return
            except Exception as e:                                   # noqa: BLE001
                log("episode", f"{t.name} ep{ep_i:02d}: synthesis CRASHED "
                               f"{type(e).__name__}: {e}")
                return
        if args.synth_only:
            return
        try:
            rec = score_episode(hb_score, cfg, bench, t.name, ep, pool,
                                judge=not args.rollout_only)
        except SystemExit as e:
            log("episode", f"{t.name} ep{ep_i:02d}: scoring FAILED {e}")
            return
        except Exception as e:                                       # noqa: BLE001
            log("episode", f"{t.name} ep{ep_i:02d}: scoring CRASHED "
                           f"{type(e).__name__}: {e}")
            return
        with lock:
            records.append(rec)

    # Two pools: one for episodes, one shared by every episode's customer attempts.
    # The attempt pool has to be shared, or each episode would get its own and the
    # live-container count would be multiplied by the episode concurrency.
    with cf.ThreadPoolExecutor(max_workers=int(cfg["score"]["n_concurrent"])) as pool, \
            cf.ThreadPoolExecutor(max_workers=n_ep) as ep_pool:
        list(ep_pool.map(one_episode, jobs))

    if records and not args.synth_only:
        idx = ROOT / "runs" / run_id / bench / "episodes.json"
        recs = []
        for r in records:
            sj = ROOT / r["episode_dir"] / "score.json"
            recs.append(json.loads(sj.read_text()) if sj.is_file() else r)
        idx.write_text(json.dumps(recs, indent=2, ensure_ascii=False) + "\n")
        log("episode", f"wrote {rel(idx)}")
    log("episode", f"done. run_id={run_id}  "
                   f"aggregate with: python3 harness/aggregate.py --run-id {run_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
