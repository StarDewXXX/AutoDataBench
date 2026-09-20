#!/usr/bin/env python3
"""Write rubrics from analyst output that is already on disk.

An analyst run writes its JSON to prep/<slug>/<benchmark>/<task>/analysis/ before
the harness validates it and copies it to rubrics/. If validation crashed (it did:
a shadowed variable made any rubric with two or more modes raise), the expensive
part is already paid for and the raw output is sitting there. This picks it up,
validates it with the current code, and writes the rubric, so nothing has to be
re-run against the model.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import add_path_args, apply_path_args, load_config, log, rel, sampled_tasks
from run_analyst import rubric_path, validate
from run_rollout import prep_dir, rollout_bundle_path


def main() -> int:
    ap = argparse.ArgumentParser(description="write rubrics from analyst output already on disk")
    ap.add_argument("--benchmark", action="append", required=True)
    ap.add_argument("--config", default=None)
    ap.add_argument("--force", action="store_true", help="overwrite an existing rubric")
    add_path_args(ap)
    args = ap.parse_args()
    apply_path_args(args)
    cfg = load_config(args.config)

    n = 0
    for bench in args.benchmark:
        for t in sampled_tasks(bench):
            out = rubric_path(cfg, bench, t.name)
            if out.is_file() and not args.force:
                continue
            raw = prep_dir(cfg, bench, t.name) / "analysis" / "failure_modes.json"
            if not raw.is_file():
                log("recover", f"{bench}/{t.name}: no analyst output at {rel(raw)}")
                continue
            try:
                fm = json.loads(raw.read_text())
            except json.JSONDecodeError as e:
                log("recover", f"{bench}/{t.name}: analyst output is not valid JSON ({e})")
                continue
            problems = validate(fm, t.name)
            summary = {}
            s = rollout_bundle_path(cfg, bench, t.name) / "summary.json"
            if s.is_file():
                j = json.loads(s.read_text())
                summary = {k: j.get(k) for k in ("attempts", "n_solved", "pass_rate", "pass_threshold")}
            fm["_meta"] = {
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "analyst": {"agent": cfg["analyst"]["agent"], "model": cfg["analyst"]["model"]},
                "rollout_summary": summary,
                "validation_problems": problems,
                "recovered_from": rel(raw),
            }
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(fm, indent=2, ensure_ascii=False) + "\n")
            kinds: dict[str, int] = {}
            for m in fm.get("modes") or []:
                k = (m.get("source") or {}).get("observed_as", "?")
                kinds[k] = kinds.get(k, 0) + 1
            log("recover", f"{bench}/{t.name}: {len(fm.get('modes') or [])} mode(s) {kinds} -> {rel(out)}"
                           + (f"  problems={problems}" if problems else ""))
            n += 1
    log("recover", f"wrote {n} rubric(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
