#!/usr/bin/env python3
"""Stage 2a (prep): roll the customer model out on the ORIGINAL sampled tasks.

Offline prep, run once per benchmark before anything is measured. For every task
in benchmarks/<benchmark>/, the customer model attempts it K times and each
attempt is turned into a sanitized transcript bundle:

    prep/<customer-model>/<benchmark>/<task>/rollout/attempt-0.md ...
    prep/<customer-model>/<benchmark>/<task>/rollout/summary.json

The customer-model level is not cosmetic: a rollout bundle is a record of ONE
model's behaviour, and every rubric derived from it inherits that. The slug comes
from configs' customer.model, so switching customer model writes a fresh tree
instead of overwriting the previous model's evidence.

These bundles are the load-bearing new input of v2. They are read by three
different consumers, for three different purposes:

  the analyst (stage 2b)  to name the failure modes that become the hidden rubric
  the researcher (stage 3) to see how the customer actually fails on this task
  nobody else -- the judge reads the SYNTHESIZED task's own rollout, not this one

Raw harbor output is kept under prep/<benchmark>/<task>/.raw/ until the bundle is
built, then dropped unless --keep-raw: it is bulky and it carries the resolved
gateway key, while the bundle does not.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (ROOT, Harbor, add_path_args, apply_path_args, customer_slug,
                    load_config, load_meta, log, make_harbor, prep_root,
                    resolve_harbor_bin, sampled_tasks, verifier_env_vars, rel)
from rollout import build_rollout_bundle, clear_raw, run_rollouts


def prep_dir(cfg: dict, benchmark: str, task: str) -> Path:
    return prep_root(cfg) / benchmark / task


def rollout_bundle_path(cfg: dict, benchmark: str, task: str) -> Path:
    return prep_dir(cfg, benchmark, task) / "rollout"


def prebuild(tasks: list[Path], parallel: int = 2, timeout: float = 2400.0) -> dict[str, bool]:
    """Warm the docker layer cache with ONE build of each task's environment.

    harbor builds a separate image per attempt, and K attempts of the same task
    start at the same instant, so without this each task's Dockerfile is built K
    times concurrently from a cold cache: K parallel downloads of the same wheels
    from PyPI and K metadata pulls of the same base image from Docker Hub. On the
    first v2 rollout that produced a pip read timeout on one task (all K attempts
    lost) and a Docker Hub anonymous rate-limit hit shortly after. One warm build
    per task makes every attempt's build a cache hit.

    A task whose environment cannot be built here at all (an amd64-only base image
    on this arm64 host, a conda post-link script that fails under this kernel) is
    reported so the run can skip it instead of spending K attempts discovering it.
    """
    def one(t: Path) -> tuple[str, bool, str]:
        # Both images a task needs: the agent environment AND the verifier. A task
        # whose tests/Dockerfile cannot build fails only after the agent has spent
        # its whole budget -- edna-mifish-community lost 6 x 85 min that way.
        for sub in ("environment", "tests"):
            ctx = t / sub
            if not (ctx / "Dockerfile").is_file():
                continue
            tag = f"adb-prebuild-{t.name.lower()}-{sub}"
            try:
                r = subprocess.run(["docker", "build", "-q", "-t", tag, str(ctx)],
                                   capture_output=True, text=True, timeout=timeout)
            except subprocess.TimeoutExpired:
                return t.name, False, f"{sub}: build timed out"
            if r.returncode != 0:
                tail = (r.stderr or r.stdout).strip().splitlines()[-3:]
                return t.name, False, f"{sub}: " + " | ".join(x[:160] for x in tail)
            subprocess.run(["docker", "rmi", "-f", tag], capture_output=True)   # layers stay cached
        return t.name, True, "ok"

    out: dict[str, bool] = {}
    with cf.ThreadPoolExecutor(max_workers=parallel) as pool:
        for name, ok, msg in pool.map(one, tasks):
            out[name] = ok
            log("prebuild", f"{name}: {'ok' if ok else 'FAILED -- ' + msg}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="stage 2a: customer rollouts on the original sampled tasks")
    ap.add_argument("--prebuild", action="store_true",
                    help="build each task's environment once first to warm the layer cache; "
                         "tasks whose environment cannot be built here are skipped")
    ap.add_argument("--prebuild-only", action="store_true", help="prebuild and exit")
    ap.add_argument("--benchmark", required=True)
    ap.add_argument("--task", action="append", default=None,
                    help="task directory name; repeatable. default: every sampled task")
    ap.add_argument("--attempts", type=int, default=None)
    ap.add_argument("--concurrency", type=int, default=None,
                    help="max live attempts for THIS invocation (default: config score.n_concurrent). "
                         "When several benchmarks run side by side, split the global budget across them here.")
    ap.add_argument("--config", default=None)
    ap.add_argument("--keep-raw", action="store_true",
                    help="keep raw harbor output (bulky; contains the gateway key)")
    ap.add_argument("--force", action="store_true", help="re-run attempts that already have a verdict")
    ap.add_argument("--dry-run", action="store_true", help="print what would run and exit")
    add_path_args(ap)
    args = ap.parse_args()
    apply_path_args(args)

    cfg = load_config(args.config)
    bench = args.benchmark
    meta = load_meta(bench)
    tasks = sampled_tasks(bench)
    if args.task:
        want = set(args.task)
        tasks = [t for t in tasks if t.name in want]
        missing = want - {t.name for t in tasks}
        if missing:
            raise SystemExit(f"no such sampled task(s) in {bench}: {sorted(missing)}")

    cust = cfg["customer"]
    attempts = args.attempts if args.attempts is not None else int(cust["attempts"])

    # A task whose bundle already holds `attempts` verdicts is done. Without this
    # check, re-running a benchmark to top up one task pays for every other task's
    # attempts again: raw output is cleared once a bundle is complete, so nothing
    # downstream can tell the attempts were already made.
    if not args.force:
        done = []
        for t in list(tasks):
            s_path = rollout_bundle_path(cfg, bench, t.name) / "summary.json"
            if not s_path.is_file():
                continue
            try:
                prev = json.loads(s_path.read_text())
            except json.JSONDecodeError:
                continue
            if prev.get("attempts") == attempts and not prev.get("attempts_no_verdict"):
                done.append(t.name)
                tasks.remove(t)
        if done:
            log("rollout", f"{bench}: {len(done)} task(s) already have {attempts} verdicts, "
                           f"skipping (use --force to redo): {done}")
        if not tasks:
            log("rollout", f"{bench}: nothing left to do")
            return 0

    log("rollout", f"{bench}: {len(tasks)} task(s) x {attempts} attempts "
                   f"with {cust['agent']}/{cust['model']}")
    log("rollout", f"{bench}: writing to {rel(prep_root(cfg) / bench)} "
                   f"(customer slug {customer_slug(cfg)})")
    for t in tasks:
        need = verifier_env_vars(t)
        note = f"  (verifier.env: {sorted(need)})" if need else ""
        log("rollout", f"    {t.name}  domain={meta['tasks'].get(t.name, {}).get('domain') or '-'}{note}")
    if args.dry_run:
        return 0

    if args.prebuild or args.prebuild_only:
        built = prebuild(tasks)
        skipped = [n for n, ok in built.items() if not ok]
        if skipped:
            log("rollout", f"{bench}: skipping {len(skipped)} task(s) whose environment does not build "
                           f"on this host: {skipped}")
            tasks = [t for t in tasks if built.get(t.name, True)]
        if args.prebuild_only:
            return 0 if not skipped else 1

    hb = make_harbor(cfg, float(cfg["score"]["setup_timeout_mult"]))

    # One bounded pool across all tasks: the cap is on live containers, not on
    # tasks, so a per-task pool would multiply the intended concurrency by the
    # number of tasks.
    summaries: dict[str, dict] = {}
    n_conc = args.concurrency or int(cfg["score"]["n_concurrent"])
    log("rollout", f"{bench}: concurrency={n_conc}")
    with cf.ThreadPoolExecutor(max_workers=n_conc) as pool:
        pending = []
        for t in tasks:
            raw = prep_dir(cfg, bench, t.name) / ".raw"
            if args.force and raw.exists():
                clear_raw(raw)
            # Submit every task's attempts up front and collect afterwards; the
            # pool is the only thing bounding live containers.
            pending.append((t, raw, run_rollouts(
                hb, t, cust["model"], raw, attempts, cfg, bench,
                cust["agent"], bool(cust.get("closed_book", True)), pool=pool,
                tag="rollout", wait=False)))
        for t, raw, futs in pending:
            for f in futs:
                f.result()
            bundle = rollout_bundle_path(cfg, bench, t.name)
            s = build_rollout_bundle(raw, t, bundle)
            summaries[t.name] = s
            log("rollout", f"{t.name}: pass_rate={s['pass_rate']} "
                           f"({s['n_solved']}/{s['attempts']}) -> {rel(bundle)}")
            # Keep the raw output whenever an attempt produced no verdict: a rerun
            # of that task then redoes only the missing attempts (run_rollouts
            # reuses any attempt that already has a reward) instead of all K.
            if not args.keep_raw and s["attempts_no_verdict"] == 0:
                clear_raw(raw)
            elif s["attempts_no_verdict"]:
                log("rollout", f"{t.name}: {s['attempts_no_verdict']} attempt(s) without verdict; "
                               f"raw output kept for a rerun")

    index = prep_root(cfg) / bench / "rollout_index.json"
    index.parent.mkdir(parents=True, exist_ok=True)
    index.write_text(json.dumps({
        "benchmark": bench,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "customer": {"agent": cust["agent"], "model": cust["model"], "attempts": attempts,
                     "closed_book": bool(cust.get("closed_book", True))},
        "tasks": summaries,
    }, indent=2) + "\n")
    log("rollout", f"wrote {rel(index)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
