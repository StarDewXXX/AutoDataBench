#!/usr/bin/env python3
"""Stage 3b: score one synthesized task.

Three things are measured, and the arithmetic that combines them is here rather
than in any prompt, so it can be changed without touching what a model is asked
to do.

  difficulty  The customer model attempts the delivered task K times; the task's
              own verifier grades each attempt. difficulty is 1 when the solved
              rate lands inside the configured band and 0 otherwise. A task the
              customer always solves and one it never solves are both useless as
              training data, for opposite reasons.

  quality     Coverage of the hidden rubric: for each failure mode the original
              task exposed, did the DELIVERED task exercise it. Binary per mode:
              present or absent. Scored against a coverage target rather than as a
              plain fraction -- see quality_from_coverage. The judge gives the
              verdicts; the arithmetic is here. The same rule applies whether the
              original attempts all failed, all passed, or split -- the rubric
              records that, the scoring ignores it.

              A verdict whose structure we cannot read is an error, not a low score:
              see validate_verdict.

  format      Mean of the format rubric's checks, from the same judge run. Recorded
              for inspection; it does not enter the score (see the note by `graded`).

  gate        0/1 from the judge, per the gate rubric.

    episode_raw = gate * difficulty * quality

Both quality and format come from one judge run: it needs the delivered task, its
rollouts, the original, the original's rollouts and the hidden rubric in front of
it either way, and splitting that into two agents would double the reading for no
extra signal.

The difficulty rollouts must finish before the judge starts, because the judge's
main evidence IS those rollouts.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import difflib
import json
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mounts
from common import (ROOT, Harbor, add_path_args, apply_path_args, benchmark_dir,
                    discover_tasks, load_config, log, render_task, resolve_harbor_bin,
                    rel, shared_rubrics_dir, make_harbor)
from rollout import build_rollout_bundle, run_rollouts
from run_analyst import rubric_path
from run_rollout import rollout_bundle_path
from run_synth import episode_dir

PROMPT = ROOT / "agents" / "judge"

# Per-mode scoring is binary: a failure mode is exercised by the delivered task or
# it is not. What the customer did there (fell in, detoured, handled it) is recorded
# by the judge as `outcome` for analysis and does not change the credit -- whether
# the model then fails is the difficulty measurement's business. Every mode counts
# once. Overridable from config as score.verdict_credit.
DEFAULT_VERDICT_CREDIT = {
    "present": 1.0,
    "absent": 0.0,
    "unreachable": None,          # excluded from both numerator and denominator
}


def difficulty_score(pass_rate: float | None, lo: float, hi: float) -> int:
    if pass_rate is None:
        return 0
    return 1 if lo <= pass_rate <= hi else 0


def validate_verdict(verdict: dict, modes: list, name: str) -> None:
    """Refuse to score a verdict whose structure we cannot read. No aliases.

    Every silent fallback in this file used to absorb a schema drift and turn it
    into a plausible-looking score, always downwards. In run 20260909-003117 one
    judge wrote `mode_id` instead of `id` for all five coverage entries and the
    episode was recorded as quality 0.0 when the judge had in fact marked all five
    present; another wrote `name`/`points` instead of `point`/`score` and was
    recorded as format 0.0 with all eight checks actually scored. Both numbers
    looked like judgements and were artefacts.

    Accepting the alternative key names would fix those two cases and hide the next
    three, so the drift is an error: the episode is not scored, the operator is told
    exactly which key was wrong, and the judge is re-run. Every problem found is
    reported at once, because a drifting verdict usually drifts in several places.
    """
    problems: list[str] = []

    cov = verdict.get("coverage")
    if not isinstance(cov, list):
        problems.append(f"coverage is {type(cov).__name__}, expected a list")
        cov = []
    seen_ids = []
    for i, c in enumerate(cov):
        if not isinstance(c, dict):
            problems.append(f"coverage[{i}] is {type(c).__name__}, expected an object")
            continue
        if not isinstance(c.get("id"), str) or not c["id"].strip():
            problems.append(f"coverage[{i}] has no usable \"id\" (keys present: "
                            f"{sorted(c)}) -- the mode id must be under the key `id`")
            continue
        if not isinstance(c.get("verdict"), str):
            problems.append(f"coverage[{i}] (id={c['id']!r}) has no string \"verdict\"")
        seen_ids.append(c["id"])
    want = [m.get("id") for m in modes if isinstance(m, dict) and m.get("id")]
    absent = [m for m in want if m not in seen_ids]
    if absent:
        problems.append(f"no coverage entry for {len(absent)} rubric mode(s): {absent}")

    fp = verdict.get("format_points")
    if not isinstance(fp, list) or not fp:
        problems.append(f"format_points is {type(fp).__name__}"
                        f"{' and empty' if isinstance(fp, list) else ''}, expected a non-empty list")
    else:
        for i, f in enumerate(fp):
            if not isinstance(f, dict):
                problems.append(f"format_points[{i}] is {type(f).__name__}, expected an object")
                continue
            bad = []
            if not isinstance(f.get("point"), str) or not f["point"].strip():
                bad.append('a non-empty string "point" (the check name)')
            if not isinstance(f.get("score"), (int, float)) or isinstance(f.get("score"), bool):
                bad.append('a numeric "score"')
            if bad:
                problems.append(f"format_points[{i}] needs {' and '.join(bad)} "
                                f"(keys present: {sorted(f)})")

    gr = verdict.get("gate_reasons")
    if gr is not None and not isinstance(gr, list):
        problems.append(f"gate_reasons is {type(gr).__name__}, expected a list")
    for i, r in enumerate(gr or []):
        if not isinstance(r, dict):
            problems.append(f"gate_reasons[{i}] is {type(r).__name__}, expected an object "
                            f"with \"gate\" and \"evidence\"")
        elif not isinstance(r.get("gate"), str) or r["gate"].strip().isdigit():
            problems.append(f"gate_reasons[{i}] has \"gate\"={r.get('gate')!r}; it must be the "
                            f"gate's name copied from the gate rubric, not its number")

    if problems:
        raise SystemExit(
            f"{name}: the judge's verdict does not match the required schema, so this episode "
            f"is not scored. Re-run the judge for it.\n  "
            + "\n  ".join(problems))


def quality_from_coverage(coverage: list, modes: list, cfg: dict) -> tuple[float | None, dict]:
    """How much of the rubric the delivered task exercised, against a coverage target.

    Not the raw fraction. One new task cannot stage every failure mode of the task it
    was built from without being that task: in run 20260909-003117 the only episodes
    that reached 100% coverage were ones the judge had also gated as clones, and one
    judge said so outright ("the modes carry over by construction rather than by
    design"). Demanding all of them therefore rewards exactly the behaviour the gate
    exists to punish.

    So a target fraction `a` = score.quality_coverage_target counts as full marks:

        quality = min(1, covered / (a * scoreable_modes))

    Each covered mode is worth 1/(a*N) up to the cap. a = 1 gives back the plain
    fraction. The judge is NOT told about `a`: it answers present/absent per mode and
    the arithmetic stays here, because a judge that knows it only needs a*N modes has
    a reason to stop looking at the rest.

    Returns (None, detail) when nothing is scoreable -- an empty rubric, or every
    mode judged `unreachable`. None is not zero and must not be treated as zero: it
    means this episode carries no quality signal, which is a fact about the
    original task or the run, not a fault of the delivered task. The caller reports
    it and excludes the episode from means.
    """
    vc = {**DEFAULT_VERDICT_CREDIT, **(cfg["score"].get("verdict_credit") or {})}
    ids = [m.get("id") for m in modes if isinstance(m, dict) and m.get("id")]
    by_id = {c.get("id"): c for c in coverage if isinstance(c, dict)}

    num = den = 0.0
    per_mode = []
    missing: list[str] = []
    unknown_verdict: list[str] = []
    for mid in ids:
        c = by_id.get(mid)
        if c is None:
            # A mode the judge did not report. Counted as absent rather than
            # skipped: skipping would let an incomplete verdict raise the score.
            missing.append(str(mid))
            verdict, credit = "missing", 0.0
        else:
            verdict = c.get("verdict")
            credit = vc.get(verdict, 0.0)
            if verdict not in vc:
                unknown_verdict.append(f"{mid}={verdict!r}")
        if credit is None:                      # unreachable
            per_mode.append({"id": mid, "verdict": verdict, "credit": None,
                             "outcome": (c or {}).get("outcome", "")})
            continue
        num += credit
        den += 1.0
        per_mode.append({"id": mid, "verdict": verdict, "credit": credit,
                         "outcome": (c or {}).get("outcome", "")})

    extra = [str(k) for k in by_id if k not in set(ids)]
    target = float(cfg["score"].get("quality_coverage_target", 1.0))
    if not 0 < target <= 1:
        raise SystemExit(f"score.quality_coverage_target must be in (0, 1], got {target}")
    detail = {
        "per_mode": per_mode,
        "modes_scored": int(den),
        "modes_in_rubric": len(ids),
        "modes_missing_from_verdict": missing,
        "modes_not_in_rubric": extra,
        "unknown_verdicts": unknown_verdict,
        "coverage_target": target,
    }
    if den <= 0:
        detail["unscorable_reason"] = ("no scoreable mode: the rubric is empty, or every mode "
                                       "was judged unreachable")
        return None, detail
    # Ceil, so the target is a whole number of modes: with 5 modes and a = 0.6 it is
    # 3 modes, not 3.0000000000000004, and 3 covered modes really do score 1.0.
    need = max(1, math.ceil(target * den - 1e-9))
    detail["modes_needed_for_full"] = need
    detail["modes_covered"] = num
    return min(1.0, num / need), detail


def format_from_points(points: list, name: str) -> tuple[float, dict]:
    """Mean of the format checks. Assumes validate_verdict() has already run.

    There is deliberately no fallback for an unreadable list: recording 0 for "the
    judge's keys were wrong" reads as "this task's formatting is terrible", which is
    a different and much worse claim.
    """
    scored = [(str(p["point"]), float(p["score"])) for p in points]
    if not scored:                       # unreachable after validation; defensive
        raise SystemExit(f"{name}: no format points to score")
    return (sum(v for _, v in scored) / len(scored),
            {"n_points": len(scored), "readable": True,
             "points": [{"point": k, "score": v} for k, v in scored]})


def run_difficulty(hb: Harbor, cfg: dict, benchmark: str, new_task: Path,
                   ep_dir: Path, pool: cf.Executor) -> dict:
    cust = cfg["customer"]
    bundle = ep_dir / "score" / "rollout"
    want = int(cust["attempts"])
    existing = bundle / "summary.json"
    if existing.is_file():
        # Re-scoring an episode (a judge that failed, a rubric change) must not
        # re-run the customer. Raw output is cleared once its bundle is built, so
        # without this check a --score-only pass silently pays for K fresh attempts
        # and, worse, measures difficulty on a different sample than the judge is
        # reading.
        try:
            prev = json.loads(existing.read_text())
        except json.JSONDecodeError:
            prev = {}
        if prev.get("attempts") == want and not prev.get("attempts_no_verdict"):
            log("difficulty", f"{new_task.name}: reusing the existing bundle "
                              f"({prev['n_solved']}/{want} solved)")
            return prev
    raw = ep_dir / "score" / ".raw"
    run_rollouts(hb, new_task, cust["model"], raw, int(cust["attempts"]), cfg,
                 benchmark, cust["agent"], bool(cust.get("closed_book", True)),
                 pool=pool, tag="difficulty")
    summary = build_rollout_bundle(raw, new_task, bundle)
    # Raw output is KEPT (2026-09-13). It is the only place that records how each
    # attempt ended -- harbor's exception_info and claude-code's terminal result
    # line -- and without it a gateway 429 that killed an attempt is
    # indistinguishable from a model that tried and failed. It is also what
    # harness/retry.py reads to decide what to re-run. See rollout.py's docstring
    # for why the old reason to delete it (a key in config.json) does not hold.
    if summary.get("attempts_excluded") or summary.get("attempts_no_verdict"):
        log("difficulty",
            f"{new_task.name}: {len(summary.get('attempts_excluded') or [])} attempt(s) "
            f"excluded as faults, {summary.get('attempts_no_verdict', 0)} without a verdict; "
            f"n_usable={summary.get('n_usable')} -- run harness/retry.py to re-run them")
    return summary


def instruction_diff(orig_dir: Path, new_task: Path) -> dict:
    """Word-level diff of the two instructions, computed here rather than argued about.

    The surface-swap gate now has a bright line: an instruction that is the
    original's wording with names and quantities substituted is a surface swap
    whatever family the task belongs to. Deciding that from prose invited the judge
    to reason its way out -- in run 20260909-235203 it cleared deliveries whose
    instruction differed from the original in ten spans, every one of them a noun
    swap (warranty -> expense, ss_warranty -> ss_expense). So the harness hands it
    the diff: the spans are a fact, and only "is this span a renaming" is left to
    judge.
    """
    a = (orig_dir / "instruction.md").read_text(errors="replace").split()
    b = (new_task / "instruction.md").read_text(errors="replace").split()
    sm = difflib.SequenceMatcher(None, a, b)
    same = sum(bl.size for bl in sm.get_matching_blocks())
    spans = [{"op": op, "original": " ".join(a[i1:i2]), "delivered": " ".join(b[j1:j2])}
             for op, i1, i2, j1, j2 in sm.get_opcodes() if op != "equal"]
    return {"tokens_original": len(a), "tokens_delivered": len(b),
            "token_similarity": round(same / max(len(a), len(b), 1), 4),
            "n_spans": len(spans), "spans": spans}


def write_instruction_diff(orig_dir: Path, new_task: Path, dst: Path) -> dict:
    d = instruction_diff(orig_dir, new_task)
    lines = [f"# Word-level diff: {orig_dir.name} (original) vs {new_task.name} (delivered)", "",
             f"- instruction.md word count: {d['tokens_original']} original, "
             f"{d['tokens_delivered']} delivered",
             f"- words shared, in order: {d['token_similarity']:.1%}",
             f"- differing spans: {d['n_spans']}", "",
             "Computed mechanically from the two files; nothing here is a judgement. "
             "Read the spans and decide only one thing: is every span a renaming — a "
             "different quantity, entity, path or identifier — or does some span add or "
             "remove something a solver has to work out?", ""]
    for i, s in enumerate(d["spans"], 1):
        lines += [f"## span {i} ({s['op']})", "",
                  "original:", "```", s["original"] or "(nothing)", "```",
                  "delivered:", "```", s["delivered"] or "(nothing)", "```", ""]
    dst.write_text("\n".join(lines) + "\n")
    return d


def run_judge(hb: Harbor, cfg: dict, benchmark: str, original: str, new_task: Path,
              ep_dir: Path, new_bundle: Path) -> dict:
    rub = rubric_path(cfg, benchmark, original)
    if not rub.is_file():
        raise SystemExit(f"no rubric at {rub}; run run_analyst.py first")
    orig_dir = benchmark_dir(benchmark) / original
    orig_bundle = rollout_bundle_path(cfg, benchmark, original)

    work = ep_dir / "score" / "judge"
    work.mkdir(parents=True, exist_ok=True)
    result = work / "verdict.json"
    if result.exists():
        result.unlink()

    fmt_rubric = shared_rubrics_dir(cfg) / "format.md"
    gate_rubric_src = shared_rubrics_dir(cfg) / "gate.md"
    # The gate rubric names the two models by placeholder so the judge cannot
    # mistake the model the researcher was RUN ON for one it chose to call -- the
    # single most expensive judging error in v1, where it zeroed honest batches.
    gate_rubric = work / "gate.md"
    gate_rubric.write_text(
        gate_rubric_src.read_text()
        .replace("__RESEARCHER_MODEL__", cfg["researcher"]["model"])
        .replace("__ALLOWED_MODEL__", cfg["customer"]["model"]))

    summary = json.loads((new_bundle / "summary.json").read_text())
    # The judge sees the same subset the researcher did. Without it, it cannot tell
    # a benchmark-wide shared template from a file the researcher copied, and it
    # cannot know what a task in this benchmark normally looks like -- both of which
    # it was citing as reskin evidence in the first run.
    bench_dir = benchmark_dir(benchmark)
    instr_diff_path = work / "instruction-diff.md"
    instr_diff = write_instruction_diff(orig_dir, new_task, instr_diff_path)
    rendered = render_task(PROMPT, work / "task", {
        "__BENCHMARK_DIR__": str(bench_dir.resolve()),
        "__INSTRUCTION_DIFF__": str(instr_diff_path.resolve()),
        "__NEW_TASK_DIR__": str(new_task.resolve()),
        "__NEW_ROLLOUT_DIR__": str(new_bundle.resolve()),
        "__ORIGINAL_DIR__": str(orig_dir.resolve()),
        "__ORIGINAL_ROLLOUT_DIR__": str(orig_bundle.resolve()),
        "__RUBRIC__": str(rub.resolve()),
        "__FORMAT_RUBRIC__": str(fmt_rubric.resolve()),
        "__GATE_RUBRIC__": str(gate_rubric.resolve()),
        "__TRAJECTORY_DIR__": str((ep_dir / "job").resolve()),
        "__RESULT__": str(result.resolve()),
        "__ATTEMPTS__": str(summary.get("attempts", 0)),
        "__HARBOR__": hb.bin,
    })

    jd = cfg["judge"]
    budget_sec = float(jd["budget_min"]) * 60
    mult = budget_sec / 2400.0                       # task.toml declares 2400s
    mnt = mounts.build(hb.bin, ro=[bench_dir, new_task, new_bundle, orig_dir, orig_bundle,
                                   rub.parent, fmt_rubric.parent, ep_dir / "job"],
                       rw=[work])
    log("judge", f"{original}: judging with {jd['agent']}/{jd['model']}")
    rc = hb.agent_task(rendered, jd["agent"], jd["model"], work / "job",
                       budget_sec * 1.5 + 900, mnt,
                       agent_env=[f"ADB_RESULT={result}",
                                  f"ADB_DEADLINE_EPOCH={int(time.time() + budget_sec)}"],
                       agent_timeout_mult=mult)
    if not result.is_file():
        raise SystemExit(f"{original}: judge wrote no verdict (harbor rc={rc}); see {work / 'job'}")
    return json.loads(result.read_text())


def score_episode(hb: Harbor, cfg: dict, benchmark: str, original: str,
                  ep_dir: Path, pool: cf.Executor, judge: bool = True) -> dict:
    out = ep_dir / "work" / "out"
    delivered = discover_tasks(out) if out.is_dir() else []
    rec: dict = {
        "benchmark": benchmark,
        "original_task": original,
        "episode_dir": rel(ep_dir),
        "scored_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "n_delivered": len(delivered),
    }

    if len(delivered) != 1:
        # The contract is one task. Nothing to roll out or judge, and this is a
        # gate failure by definition, so it is recorded as one rather than left
        # as a crash.
        rec.update({
            "gate": 0,
            "gate_reasons": [{"gate": "The delivery is not one task",
                              "evidence": f"{len(delivered)} task directories under {out}"}],
            "difficulty": 0, "quality": None, "format": 0.0, "episode_raw": 0.0,
            "note": "no single delivered task; not rolled out or judged",
        })
        (ep_dir / "score.json").write_text(json.dumps(rec, indent=2, ensure_ascii=False) + "\n")
        log("score", f"{original}: {len(delivered)} tasks delivered -> gate 0")
        return rec

    new_task = delivered[0]
    rec["delivered_task"] = new_task.name

    diff = run_difficulty(hb, cfg, benchmark, new_task, ep_dir, pool)
    lo, hi = float(cfg["score"]["band_lo"]), float(cfg["score"]["band_hi"])
    difficulty = difficulty_score(diff.get("pass_rate"), lo, hi)
    rec["rollout"] = diff
    rec["difficulty"] = difficulty
    rec["band"] = {"lo": lo, "hi": hi}

    # Too few attempts actually measured the model, so the band cannot be read off
    # this sample. Recorded as unscorable with episode_raw = None, which
    # aggregate.py already drops from the mean instead of counting as a zero -- a
    # confident 0 here would be a claim we cannot support. The judge is not run:
    # it costs $10-20 and its verdict cannot be combined into a score anyway.
    if diff.get("unscorable"):
        rec.update({
            "gate": None, "quality": None, "format": None,
            "episode_raw": None, "difficulty": None,
            "unscorable": diff["unscorable"],
            "note": "difficulty not measurable; judge not run",
        })
        (ep_dir / "score.json").write_text(json.dumps(rec, indent=2, ensure_ascii=False) + "\n")
        log("score", f"{original}: UNSCORABLE -- {diff['unscorable']}")
        return rec

    # Out of band: the customer solved the delivered task every time, or never, so
    # difficulty is 0 and episode_raw = gate * difficulty * graded is 0 no matter what
    # the judge would have said. Judging anyway buys nothing and costs a 40-minute
    # opus session per episode -- the same argument the unscorable branch above
    # already makes about its own case. It is most of a run's judging bill, not a
    # rounding error: 7 of the 8 terminal-bench ep02 episodes were out of band.
    #
    # episode_raw is 0.0, not None. 0 is what the episode scored, and it has to stay
    # in the denominator: scoreboard.py counts None as a zero but aggregate.py:84
    # drops it from the mean, which would pay the researcher for delivering a task
    # that separates nothing. scoreboard.py's "unjudged" complaint is already scoped
    # to difficulty == 1, i.e. it asks for a judge exactly where one can change a
    # score. Re-judging later is still possible from the artifacts on disk if the band
    # itself is ever redefined: run_episode.py --score-only re-runs the judge and
    # reuses the existing rollout bundle.
    if not difficulty:
        rec.update({
            "gate": None, "quality": None, "format": None, "episode_raw": 0.0,
            "instruction_diff": {k: v for k, v in
                                 instruction_diff(benchmark_dir(benchmark) / original,
                                                  new_task).items() if k != "spans"},
            "note": "out of band; judge not run because difficulty 0 zeroes the score",
        })
        (ep_dir / "score.json").write_text(json.dumps(rec, indent=2, ensure_ascii=False) + "\n")
        log("score", f"{original}: difficulty=0 (pass_rate={diff.get('pass_rate')}) "
                     f"-> episode_raw=0.0, judge skipped")
        return rec

    if not judge:
        # Phase separation, asked for explicitly: every episode's customer attempts
        # finish before any judge container starts, so a 40-minute opus session is
        # never competing for the box with the measurement it is supposed to be
        # reading. Only in-band episodes reach here -- the branch above has already
        # finalised the out-of-band ones at 0, and they never need a judge at all.
        #
        # episode_raw is None, not 0: this episode's score is not zero, it is not yet
        # known. scoreboard.py already reports difficulty == 1 with a null raw under
        # "unjudged", which names exactly what still has to be run.
        rec.update({
            "gate": None, "quality": None, "format": None, "episode_raw": None,
            "note": "in band; judge deferred to a later pass (--rollout-only)",
        })
        (ep_dir / "score.json").write_text(json.dumps(rec, indent=2, ensure_ascii=False) + "\n")
        log("score", f"{original}: difficulty=1 (pass_rate={diff.get('pass_rate')}) "
                     f"-> in band, judge deferred")
        return rec

    verdict = run_judge(hb, cfg, benchmark, original, new_task, ep_dir,
                        ep_dir / "score" / "rollout")
    # Deterministic evidence for the surface-swap gate, recorded whatever the judge
    # concluded, so a disagreement between the two can be settled from the record.
    idiff = instruction_diff(benchmark_dir(benchmark) / original, new_task)
    rec["instruction_diff"] = {k: v for k, v in idiff.items() if k != "spans"}
    modes = (json.loads(rubric_path(cfg, benchmark, original).read_text())
             .get("modes") or [])
    validate_verdict(verdict, modes, original)
    quality, qdetail = quality_from_coverage(verdict.get("coverage") or [], modes, cfg)
    fmt, fdetail = format_from_points(verdict.get("format_points") or [], original)
    gate = 1 if verdict.get("gate") in (1, True) else 0

    # Format is measured and recorded but does not enter the score. Across in-band
    # deliveries it concentrates between 0.75 and 1.0, so at the weight it used to
    # carry it left a spread too small to separate agents: recomputing every score
    # without it moved each agent by at most 0.007 and changed no ordering, at either
    # the aggregate or the per-suite level. The checks are not discarded -- a delivery
    # that fails them badly enough is unusable and the gate catches it, which is a
    # sharper instrument for the same concern. What the term measured was hygiene
    # among deliveries that were already well formed.
    graded = quality
    rec.update({
        "gate": gate,
        "gate_reasons": verdict.get("gate_reasons") or [],
        "reskin_check": verdict.get("reskin_check") or {},
        "targeted_mode": verdict.get("targeted_mode", ""),
        "quality": quality,
        "quality_detail": qdetail,
        "format": fmt,
        "format_detail": fdetail,
        "graded_term": graded,
        "episode_raw": (gate * difficulty * graded) if graded is not None else None,
        "quality_note": verdict.get("quality_note", ""),
        "judge_evidence": verdict.get("evidence", ""),
    })
    if quality is None:
        rec["unscorable"] = qdetail.get("unscorable_reason", "quality could not be computed")
        log("score", f"{original}: UNSCORABLE quality -- {rec['unscorable']}")
    (ep_dir / "score.json").write_text(json.dumps(rec, indent=2, ensure_ascii=False) + "\n")
    log("score", f"{original}: gate={gate} difficulty={difficulty} "
                 f"(pass_rate={diff.get('pass_rate')}) quality={quality} "
                 f"format={fmt:.3f} -> episode_raw={rec['episode_raw']}")
    return rec


def main() -> int:
    ap = argparse.ArgumentParser(description="stage 3b: score one or more synthesized tasks")
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--benchmark", required=True)
    ap.add_argument("--task", action="append", default=None)
    ap.add_argument("--episode", type=int, default=1)
    ap.add_argument("--config", default=None)
    add_path_args(ap)
    args = ap.parse_args()
    apply_path_args(args)

    cfg = load_config(args.config)
    bench_root = ROOT / "runs" / args.run_id / args.benchmark
    if not bench_root.is_dir():
        raise SystemExit(f"no such run: {bench_root}")
    names = args.task or sorted(d.name for d in bench_root.iterdir() if d.is_dir())

    hb = make_harbor(cfg, float(cfg["score"]["setup_timeout_mult"]))
    with cf.ThreadPoolExecutor(max_workers=int(cfg["score"]["n_concurrent"])) as pool:
        for name in names:
            ep = episode_dir(args.run_id, args.benchmark, name, args.episode)
            if not ep.is_dir():
                log("score", f"{name}: no ep{args.episode:02d} directory, skipping")
                continue
            try:
                score_episode(hb, cfg, args.benchmark, name, ep, pool)
            except SystemExit as e:
                log("score", f"{name}: FAILED {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
