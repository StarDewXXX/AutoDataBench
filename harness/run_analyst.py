#!/usr/bin/env python3
"""Stage 2b (prep): turn customer rollouts into the hidden rubric.

Offline prep, run once per benchmark after stage 2a. For every sampled task, one
claude-code run driving the analyst model reads the original task plus that task's
rollout bundle and writes a short list of failure modes, each stated at benchmark
level (a behaviour at a decision, not this task's facts) with scenario examples
and the attempts it was observed in. That list is the hidden quality rubric for
every episode built from that task:

    rubrics/<customer-model>/<benchmark>/<task>/modes.json

Modes come from three sources -- attempts that failed, detours in attempts that
succeeded, and error-prone spots the model handled -- so an all-solved original
still yields a rubric. Downstream scoring asks the same question of every mode:
did the new task exercise it.

The researcher never sees this file. It sees the same rollout bundle the analyst
saw, which is the point: the researcher has to work out for itself what the
customer's weakness is, and the rubric records what a careful reading of the same
evidence concluded.

Kept under rubrics/ rather than prep/ because it is a committed artifact of the
benchmark, not a byproduct of a run: the raw rollouts can be regenerated, but the
rubric they were reduced to is what later scores are measured against.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mounts
from common import (ROOT, Harbor, add_path_args, apply_path_args, benchmark_dir,
                    customer_slug,
                    load_config, load_meta, log, render_task, resolve_harbor_bin,
                    rubrics_root, sampled_tasks, task_name, rel, make_harbor)
from run_rollout import prep_dir, rollout_bundle_path

PROMPT = ROOT / "agents" / "analyst"


def rubric_path(cfg: dict, benchmark: str, task: str) -> Path:
    """Per-task rubric, under the customer model that produced the rollouts.

    A rubric is a statement about one model's failure modes; scoring a new task
    against another model's rubric would look perfectly plausible and be wrong, so
    the model is part of the path rather than something to remember.
    """
    return rubrics_root(cfg) / benchmark / task / "modes.json"


_OBSERVED_ORDER = ("failure", "detour", "handled")


def normalise_observed_as(src: dict) -> str | None:
    """The single strongest label in source.observed_as, or None if there is none.

    `failure` beats `detour` beats `handled`: the strongest way a mode was seen is
    the one that characterises it, and the attempt list already records the spread.
    """
    raw = str(src.get("observed_as") or "").lower()
    for k in _OBSERVED_ORDER:
        if k in raw:
            return k
    return None


def validate(fm: dict, task: str) -> list[str]:
    """Structural complaints about an analyst's output.

    Loud about a malformed rubric rather than silently letting a later stage score
    against nothing: an empty or unparseable mode list would make every episode
    built on this task unscorable, and that must not look like a zero.
    """
    problems: list[str] = []
    if not isinstance(fm, dict):
        return ["result is not a JSON object"]
    for key in ("task", "n_solved", "modes"):
        if key not in fm:
            problems.append(f"missing key: {key}")
    modes = fm.get("modes")
    if not isinstance(modes, list):
        problems.append("modes is not a list")
        return problems
    seen: set[str] = set()
    for i, m in enumerate(modes):
        if not isinstance(m, dict):
            problems.append(f"mode {i} is not an object")
            continue
        mid = m.get("id")
        if not mid:
            problems.append(f"mode {i} has no id")
        elif mid in seen:
            problems.append(f"duplicate mode id: {mid}")
        else:
            seen.add(mid)
        if not (m.get("content") or "").strip():
            problems.append(f"mode {mid or i}: empty content")
        ex = m.get("examples")
        if not isinstance(ex, list) or not [e for e in ex if str(e).strip()]:
            problems.append(f"mode {mid or i}: no examples")
        src = m.get("source") or {}
        label = normalise_observed_as(src)
        if label is None:
            problems.append(f"mode {mid or i}: source.observed_as is {src.get('observed_as')!r}")
        elif label != src.get("observed_as"):
            # A mode genuinely can show up several ways across attempts, and the
            # analyst sometimes writes "failure + handled". Rather than reject an
            # otherwise good rubric, keep the strongest label and record the raw
            # value, so nothing downstream has to parse a compound string.
            src["observed_as_raw"] = src["observed_as"]
            src["observed_as"] = label
            problems.append(f"mode {mid or i}: observed_as {src['observed_as_raw']!r} "
                            f"normalised to {label!r}")
        att = src.get("attempts")
        if not isinstance(att, list) or not att or not all(isinstance(x, int) for x in att):
            problems.append(f"mode {mid or i}: source.attempts must be a non-empty list of attempt numbers")
        if not (src.get("where") or "").strip():
            problems.append(f"mode {mid or i}: source.where is empty")
    return problems


def analyse_one(hb: Harbor, cfg: dict, benchmark: str, task_dir: Path,
                force: bool) -> dict:
    task = task_dir.name
    out_rubric = rubric_path(cfg, benchmark, task)
    if out_rubric.is_file() and not force:
        log("analyst", f"{task}: rubric exists, skipping (use --force to redo)")
        return json.loads(out_rubric.read_text())

    bundle = rollout_bundle_path(cfg, benchmark, task)
    if not (bundle / "summary.json").is_file():
        raise SystemExit(f"{task}: no rollout bundle at {bundle}; run run_rollout.py first")
    summary = json.loads((bundle / "summary.json").read_text())

    work = prep_dir(cfg, benchmark, task) / "analysis"
    work.mkdir(parents=True, exist_ok=True)
    result = work / "failure_modes.json"
    if result.exists():
        result.unlink()
    job_out = work / "job"
    # The analyst is asked to state each failure mode at BENCHMARK level -- general
    # enough that a different task in the same benchmark could provoke it. Having
    # seen one task, it can only guess what that level is; the subset shows it.
    # Rollouts stay per-task: only this task's transcripts are mounted, so the modes
    # are still inferred from the one behavioural record we have.
    bench_dir = benchmark_dir(benchmark)
    rendered = render_task(PROMPT, work / "task", {
        "__BENCHMARK__": benchmark,
        "__BENCHMARK_DIR__": str(bench_dir.resolve()),
        "__TASK_DIR__": str(task_dir.resolve()),
        "__ROLLOUT_DIR__": str(bundle.resolve()),
        "__RESULT__": str(result.resolve()),
        "__TASK_NAME__": task,
        "__CUSTOMER_MODEL__": cfg["customer"]["model"],
        "__ATTEMPTS__": str(summary.get("attempts", 0)),
    })

    an = cfg["analyst"]
    budget_sec = float(an["budget_min"]) * 60
    mult = budget_sec / 2400.0                       # task.toml declares 2400s
    mnt = mounts.build(hb.bin, ro=[bench_dir, task_dir, bundle], rw=[work],
                       with_docker=False)

    log("analyst", f"{task}: analysing with {an['agent']}/{an['model']} "
                     f"({summary.get('n_solved')}/{summary.get('attempts')} solved)")
    rc = hb.agent_task(rendered, an["agent"], an["model"], job_out,
                       budget_sec * 1.5 + 900, mnt,
                       agent_env=[f"ADB_RESULT={result}",
                                  f"ADB_DEADLINE_EPOCH={int(time.time() + budget_sec)}"],
                       agent_timeout_mult=mult)
    if not result.is_file():
        raise SystemExit(f"{task}: analyst wrote no result (harbor rc={rc}); see {job_out}")
    fm = json.loads(result.read_text())
    problems = validate(fm, task)
    fm["_meta"] = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "analyst": {"agent": an["agent"], "model": an["model"]},
        "rollout_summary": {k: summary.get(k) for k in
                            ("attempts", "n_usable", "n_solved", "pass_rate",
                             "pass_threshold", "attempts_excluded")},
        "validation_problems": problems,
    }
    out_rubric.parent.mkdir(parents=True, exist_ok=True)
    out_rubric.write_text(json.dumps(fm, indent=2, ensure_ascii=False) + "\n")

    modes = fm.get("modes") or []
    kinds: dict[str, int] = {}
    for m in modes:
        if isinstance(m, dict):
            k = (m.get("source") or {}).get("observed_as", "?")
            kinds[k] = kinds.get(k, 0) + 1
    log("analyst", f"{task}: {len(modes)} mode(s) {kinds} -> {rel(out_rubric)}")
    if problems:
        log("analyst", f"{task}: VALIDATION PROBLEMS: {problems}")
    return fm


def main() -> int:
    ap = argparse.ArgumentParser(description="stage 2b: derive the hidden decision-point rubric from rollouts")
    ap.add_argument("--benchmark", required=True)
    ap.add_argument("--task", action="append", default=None)
    ap.add_argument("--config", default=None)
    ap.add_argument("--force", action="store_true", help="re-analyse tasks that already have a rubric")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--concurrency", type=int, default=None,
                    help="max analyst runs at once (default: config score.n_concurrent)")
    add_path_args(ap)
    args = ap.parse_args()
    apply_path_args(args)

    cfg = load_config(args.config)
    bench = args.benchmark
    load_meta(bench)
    tasks = sampled_tasks(bench)
    if args.task:
        want = set(args.task)
        tasks = [t for t in tasks if t.name in want]
        missing = want - {t.name for t in tasks}
        if missing:
            raise SystemExit(f"no such sampled task(s) in {bench}: {sorted(missing)}")

    log("analyst", f"{bench}: rubrics -> {rel(rubrics_root(cfg) / bench)} "
                   f"(customer slug {customer_slug(cfg)})")
    if args.dry_run:
        for t in tasks:
            have = "rubric exists" if rubric_path(cfg, bench, t.name).is_file() else "to analyse"
            log("analyst", f"    {t.name:<44} {have}")
        return 0

    hb = make_harbor(cfg, float(cfg["score"]["setup_timeout_mult"]))

    # Analyst runs are single-container and cheap to parallelise; the cap is the
    # shared container budget, same as everywhere else.
    n = min(args.concurrency or int(cfg["score"]["n_concurrent"]), max(1, len(tasks)))
    log("analyst", f"{bench}: {len(tasks)} task(s), concurrency {n}, "
                   f"{cfg['analyst']['agent']}/{cfg['analyst']['model']}")
    failures: list[str] = []
    with cf.ThreadPoolExecutor(max_workers=n) as pool:
        futs = {pool.submit(analyse_one, hb, cfg, bench, t, args.force): t for t in tasks}
        for f in cf.as_completed(futs):
            t = futs[f]
            try:
                f.result()
            except SystemExit as e:
                failures.append(f"{t.name}: {e}")
                log("analyst", f"{t.name}: FAILED {e}")
            except Exception as e:                                   # noqa: BLE001
                # One malformed rubric must not abort the batch: the expensive part
                # (the analyst run) is already paid for, and its raw output is on
                # disk under prep/<...>/analysis/ for recovery.
                failures.append(f"{t.name}: {type(e).__name__}: {e}")
                log("analyst", f"{t.name}: CRASHED {type(e).__name__}: {e}")

    index = rubrics_root(cfg) / bench / "index.json"
    entries = {}
    for t in tasks:
        p = rubric_path(cfg, bench, t.name)
        if not p.is_file():
            continue
        fm = json.loads(p.read_text())
        modes = fm.get("modes") or []
        rs = (fm.get("_meta") or {}).get("rollout_summary") or {}
        entries[t.name] = {
            "n_modes": len(modes),
            "observed_as": {k: sum(1 for m in modes if isinstance(m, dict)
                                   and (m.get("source") or {}).get("observed_as") == k)
                            for k in ("failure", "detour", "handled")},
            "original_pass_rate": rs.get("pass_rate"),
            "mode_ids": [m.get("id") for m in modes if isinstance(m, dict)],
            "task_defects": len(fm.get("task_defects") or []),
            "validation_problems": (fm.get("_meta") or {}).get("validation_problems") or [],
        }
    index.parent.mkdir(parents=True, exist_ok=True)
    index.write_text(json.dumps({"benchmark": bench, "tasks": entries}, indent=2,
                                ensure_ascii=False) + "\n")
    log("analyst", f"wrote {rel(index)}")
    if failures:
        log("analyst", f"{len(failures)} task(s) failed: {failures}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
