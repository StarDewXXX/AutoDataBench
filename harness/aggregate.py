#!/usr/bin/env python3
"""Aggregate episode scores into per-task, per-benchmark and overall numbers.

Three decisions about the arithmetic, all of them consequences of what v1 got
wrong:

Episodes are averaged per original task first, then tasks are averaged into a
benchmark score. Averaging all episodes flat would weight a task that happened to
get more repeats more heavily, and the whole reason repeats exist is that a single
episode is noisy.

Episodes with no quality signal (`quality: null` -- an empty failure-mode rubric,
or one holding only `minor` modes) are excluded from means and counted separately.
They are not zeros: nothing about the delivered task caused them.

The spread across repeats is reported next to the mean, because in v1 the
researcher's run-to-run variance was larger than the difference between the models
being compared. A benchmark mean without it invites a conclusion the data does not
support.

Joins each task back to the domain recorded in benchmarks/<b>/_meta.json, so
results can be broken down by field even though the task directories are flat.
"""
from __future__ import annotations

import argparse
import json
import statistics as st
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT, load_meta, log, rel


def collect(run_id: str) -> list[dict]:
    root = ROOT / "runs" / run_id
    if not root.is_dir():
        raise SystemExit(f"no such run: {root}")
    out: list[dict] = []
    for p in sorted(root.rglob("score.json")):
        try:
            out.append(json.loads(p.read_text()))
        except (OSError, json.JSONDecodeError) as e:
            log("aggregate", f"skipping unreadable {p}: {e}")
    return out


def summarize(values: list[float]) -> dict:
    if not values:
        return {"n": 0, "mean": None, "sd": None, "min": None, "max": None}
    return {
        "n": len(values),
        "mean": round(st.mean(values), 4),
        "sd": round(st.stdev(values), 4) if len(values) > 1 else 0.0,
        "min": round(min(values), 4),
        "max": round(max(values), 4),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="aggregate episode scores")
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--json", action="store_true", help="print the full report as JSON")
    args = ap.parse_args()

    recs = collect(args.run_id)
    if not recs:
        raise SystemExit(f"run {args.run_id} has no score.json files yet")

    by_bench: dict[str, dict[str, list[dict]]] = {}
    for r in recs:
        by_bench.setdefault(r["benchmark"], {}).setdefault(r["original_task"], []).append(r)

    report: dict = {"run_id": args.run_id, "benchmarks": {}}
    for bench, tasks in sorted(by_bench.items()):
        try:
            meta = load_meta(bench)
        except SystemExit:
            meta = {"tasks": {}}
        t_out: dict[str, dict] = {}
        task_means: list[float] = []
        for task, eps in sorted(tasks.items()):
            scored = [e for e in eps if e.get("episode_raw") is not None]
            raws = [float(e["episode_raw"]) for e in scored]
            t_out[task] = {
                "domain": (meta["tasks"].get(task) or {}).get("domain"),
                "episodes": len(eps),
                "episodes_unscorable": len(eps) - len(scored),
                "raw": summarize(raws),
                "gate_passed": sum(1 for e in eps if e.get("gate") == 1),
                "difficulty_in_band": sum(1 for e in eps if e.get("difficulty") == 1),
                "reskin_flagged": sum(1 for e in eps
                                      if (e.get("reskin_check") or {}).get("is_reskin")),
                "quality": summarize([float(e["quality"]) for e in eps
                                      if e.get("quality") is not None]),
                "format": summarize([float(e["format"]) for e in eps
                                     if e.get("format") is not None]),
                "pass_rate": summarize([float((e.get("rollout") or {}).get("pass_rate"))
                                        for e in eps
                                        if (e.get("rollout") or {}).get("pass_rate") is not None]),
            }
            if raws:
                task_means.append(st.mean(raws))
        report["benchmarks"][bench] = {
            "n_tasks": len(t_out),
            "score": summarize(task_means),      # mean over tasks of per-task means
            "tasks": t_out,
        }

    bench_scores = [b["score"]["mean"] for b in report["benchmarks"].values()
                    if b["score"]["mean"] is not None]
    report["overall"] = summarize(bench_scores)

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0

    def num(v, width: int, places: int = 3) -> str:
        return ("-" if v is None else f"{v:.{places}f}").rjust(width)

    print(f"run {args.run_id}\n")
    for bench, b in report["benchmarks"].items():
        s = b["score"]
        print(f"== {bench}  score={s['mean']} (sd {s['sd']} over {s['n']} task(s))")
        print(f"{'original task':<40}{'domain':<22}{'eps':>4}{'raw':>8}{'sd':>7}"
              f"{'gate':>6}{'band':>6}{'qual':>7}{'fmt':>7}{'reskin':>8}")
        for task, t in b["tasks"].items():
            print(f"{task[:39]:<40}{str(t['domain'] or '-')[:21]:<22}{t['episodes']:>4}"
                  + num(t["raw"]["mean"], 8)
                  + num(t["raw"]["sd"], 7)
                  + f"{t['gate_passed']:>6}{t['difficulty_in_band']:>6}"
                  + num(t["quality"]["mean"], 7)
                  + num(t["format"]["mean"], 7)
                  + f"{t['reskin_flagged']:>8}")
            if t["episodes_unscorable"]:
                print(f"{'':<40}{t['episodes_unscorable']} episode(s) had no quality signal")
        print()
    o = report["overall"]
    print(f"overall = {o['mean']} (mean over {o['n']} benchmark(s), sd {o['sd']})")

    out = ROOT / "runs" / args.run_id / "report.json"
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    log("aggregate", f"wrote {rel(out)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
