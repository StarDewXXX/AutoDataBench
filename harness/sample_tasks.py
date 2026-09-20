#!/usr/bin/env python3
"""Stage 1: sample N tasks per benchmark into the flat v2 layout.

This runs once, offline, before anything is measured. It reads the full upstream
benchmark copies named by sample.source_override.<benchmark> and writes the sampled subset
to benchmarks/<benchmark>/, one directory per task, with no domain directories
in between:

    benchmarks/tb-science/dna-storage-codec/
    benchmarks/tb-science/hbv-calibration-1/
    benchmarks/tb-science/_meta.json
    benchmarks/tb-science/_infra/     (base-image/, engine/, vendor/, ...)

Why flat: every later stage addresses exactly one task, and the upstream layouts
disagree with each other (terminal-bench is already flat, automationbench groups
by one level, tb-science by two). A single flat convention means no stage
has to know which benchmark it is walking. The domain is not thrown away -- it is
recorded in _meta.json, which is the file to join against when analysing results
by field.

Infrastructure (base-image/, engine/, vendor/) is copied to _infra/ because tasks
reference their base image by TAG (`FROM tb-science-harbor-base:1`), not by relative
path, so it can live anywhere as long as the image gets built. Anything under
benchmarks/<benchmark>/ whose name starts with '_' is framework material; every
other directory is a task.

Sampling is seeded and recorded, so the same seed reproduces the same subset.
"""
from __future__ import annotations

import argparse
import json
import random
import re
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (ROOT, REPO, META_NAME, discover_tasks, is_task_dir, load_config,
                    log, task_name)

INFRA_DIRS = ("base-image", "engine", "vendor", "tools")
INFRA_FILES = ("build-base-image.sh", "setup.sh", "README.md", "LICENSE",
               "CITATION.cff", "CONTRIBUTING.md", "REVIEWING.md")


def upstream_root(cfg: dict, benchmark: str) -> Path:
    """The upstream copy to sample from.

    There is no default any more -- the upstream copies are not in this repository -- so
    sample.source_override.<benchmark> in
    the config points at another checkout (terminal-bench's full 66-task pool lives
    outside this repo; the sampled subset under benchmarks/ is what this repo ships).
    """
    override = (cfg["sample"].get("source_override") or {}).get(benchmark)
    src = Path(override) if override else REPO / cfg["sample"]["source_root"] / benchmark
    if not src.is_dir():
        raise SystemExit(f"no upstream copy at {src}")
    return src


def tasks_root(src: Path) -> Path:
    """Where the upstream copy keeps its tasks (all four use tasks/)."""
    return src / "tasks" if (src / "tasks").is_dir() else src


def domain_of(task_dir: Path, root: Path) -> list[str]:
    """The domain this task belongs to, as a path of increasingly specific names.

    From the directory layout when the upstream nests tasks (["support"] for
    automationbench, ["earth-sciences", "ocean-sciences"] for tb-science). For a
    flat upstream (terminal-bench) the same information lives in task.toml as
    [metadata] category / subcategory, so it is read from there instead -- the
    point of recording a domain is to break results down by field, and a flat
    layout should not lose that.
    """
    parts = list(task_dir.relative_to(root).parts[:-1])
    if parts:
        return parts
    try:
        toml = (task_dir / "task.toml").read_text()
    except OSError:
        return []
    out = []
    for key in ("category", "subcategory"):
        m = re.search(rf'^{key}\s*=\s*"([^"]*)"', toml, re.M)
        if m and m.group(1).strip():
            out.append(m.group(1).strip())
    return out


def stratified_round_robin(groups: dict[str, list[Path]], n: int, rng: random.Random) -> list[Path]:
    """Take n tasks spread as evenly as possible across domains.

    Shuffle within each domain, then deal one task per domain per pass, visiting
    domains in a shuffled order. With n smaller than the number of domains this
    gives n distinct domains; with n larger it fills up evenly. Uniform sampling
    would let one crowded domain dominate a small sample -- tb-science has 18
    tasks over 6 top-level fields, so an 8-task uniform draw can easily miss half
    the science in it.
    """
    keys = sorted(groups)
    rng.shuffle(keys)
    pools = {k: list(groups[k]) for k in keys}
    for k in pools:
        rng.shuffle(pools[k])
    picked: list[Path] = []
    while len(picked) < n and any(pools.values()):
        for k in keys:
            if not pools[k]:
                continue
            picked.append(pools[k].pop())
            if len(picked) == n:
                break
    return picked


def flat_name(task_dir: Path, domain: list[str], used: set[str]) -> tuple[str, str | None]:
    """A collision-free directory name for the flat layout.

    Returns (name, renamed_from). Two different domains can hold tasks with the
    same directory name, and flattening would silently overwrite one; prefixing
    with the domain keeps both, and the original name is recorded either way.
    """
    base = task_dir.name
    if base not in used:
        return base, None
    for prefix in ("-".join(domain), "-".join(domain[-1:])):
        if prefix:
            cand = f"{prefix}__{base}"
            if cand not in used:
                return cand, base
    i = 2
    while f"{base}-{i}" in used:
        i += 1
    return f"{base}-{i}", base


def copy_task(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    # Git plumbing only; data, images and any authoring/ trail are kept -- later
    # stages read them and none of it leaks an answer to the customer model,
    # which only ever sees the built container.
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns(
        ".git", ".github", ".gitignore", ".gitattributes", "__pycache__", "*.pyc"))


def copy_infra(src: Path, dst_root: Path) -> list[str]:
    infra = dst_root / "_infra"
    infra.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for name in INFRA_DIRS:
        s = src / name
        if s.is_dir():
            d = infra / name
            if d.exists():
                shutil.rmtree(d)
            shutil.copytree(s, d, ignore=shutil.ignore_patterns(
                ".git", "__pycache__", "*.pyc"))
            copied.append(name + "/")
    for name in INFRA_FILES:
        s = src / name
        if s.is_file():
            shutil.copy2(s, infra / name)
            copied.append(name)
    return copied


def _rel_or_abs(p: Path) -> str:
    """Repo-relative when inside the repo, absolute otherwise (an external pool)."""
    try:
        return str(p.resolve().relative_to(REPO))
    except ValueError:
        return str(p.resolve())


def git_provenance(src: Path) -> dict:
    """Upstream commit of the source copy, when it is a git checkout.

    Recorded so a subset can be traced back to an exact upstream state; empty when
    the copy carries no git dir (the v1 staging step stripped it).
    """
    try:
        out = subprocess.run(["git", "-C", str(src), "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=10)
        if out.returncode == 0:
            return {"commit": out.stdout.strip()}
    except (OSError, subprocess.SubprocessError):
        pass
    return {}


def sample_one(cfg: dict, benchmark: str, n: int, seed: int, force: bool,
               pick: list[str] | None = None) -> dict:
    src = upstream_root(cfg, benchmark)
    troot = tasks_root(src)
    all_tasks = discover_tasks(troot)
    if not all_tasks:
        raise SystemExit(f"{benchmark}: found no task.toml under {troot}")

    groups: dict[str, list[Path]] = defaultdict(list)
    for t in all_tasks:
        dom = domain_of(t, troot)
        groups[dom[0] if dom else ""].append(t)

    rng = random.Random(f"{seed}:{benchmark}")
    if pick:
        # An explicit selection (e.g. chosen by task.toml category after a build
        # test of what runs on this host) replaces the seeded draw; the names are
        # recorded in _meta.json so the choice is auditable.
        by_name = {t.name: t for t in all_tasks}
        missing = [x for x in pick if x not in by_name]
        if missing:
            raise SystemExit(f"{benchmark}: not in upstream pool: {missing}")
        picked = [by_name[x] for x in pick]
    else:
        n_eff = min(n, len(all_tasks))
        picked = stratified_round_robin(groups, n_eff, rng)
    picked.sort(key=lambda p: str(p))

    dst_root = ROOT / "benchmarks" / benchmark
    if dst_root.exists() and not force:
        raise SystemExit(f"{dst_root} already exists; pass --force to resample")
    if dst_root.exists():
        shutil.rmtree(dst_root)
    dst_root.mkdir(parents=True)

    used: set[str] = set()
    entries: dict[str, dict] = {}
    for t in picked:
        dom = domain_of(t, troot)
        name, renamed = flat_name(t, dom, used)
        used.add(name)
        copy_task(t, dst_root / name)
        entries[name] = {
            "domain": dom[0] if dom else None,
            "domain_path": dom,
            "source_rel": str(t.relative_to(troot)),
            "task_name": task_name(t),
            "renamed_from": renamed,
        }

    infra = copy_infra(src, dst_root)
    meta = {
        "benchmark": benchmark,
        "sampled_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "sampler": {
            "n_requested": n,
            "n_sampled": len(entries),
            "n_available": len(all_tasks),
            "seed": seed,
            "strategy": "explicit-pick" if pick else cfg["sample"]["strategy"],
            "pick": list(pick) if pick else None,
        },
        "source": {
            "root": _rel_or_abs(src),
            "tasks_root": _rel_or_abs(troot),
            **git_provenance(src),
        },
        "layout": {
            "note": "tasks are flat directories directly under this one; "
                    "names beginning with '_' are framework material, not tasks",
            "infra": infra,
        },
        "domains_available": {k or "(none)": len(v) for k, v in sorted(groups.items())},
        "tasks": entries,
    }
    (dst_root / META_NAME).write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n")
    return meta


def swap_task(cfg: dict, benchmark: str, old: str, new: str | None, seed: int) -> str:
    """Replace one sampled task with an unsampled one from the same upstream copy.

    Exists because a sampled task can turn out to be unrunnable on this host for
    reasons that have nothing to do with the task (an amd64-only base image on an
    arm64 machine, where the agent harness's installer aborts under emulation).
    The swap is recorded in _meta.json so the subset stays auditable.

    `new` may be omitted: then a candidate is drawn with the sampler's seed from
    the unsampled tasks, preferring the same domain as the task being replaced.
    """
    src = upstream_root(cfg, benchmark)
    troot = tasks_root(src)
    dst_root = ROOT / "benchmarks" / benchmark
    meta_p = dst_root / META_NAME
    meta = json.loads(meta_p.read_text())
    if old not in meta["tasks"]:
        raise SystemExit(f"{benchmark}: {old} is not a sampled task")
    all_tasks = {t.name: t for t in discover_tasks(troot)}
    sampled_src = {v["source_rel"].split("/")[-1] for v in meta["tasks"].values()}
    # Tasks known not to run on this host (config sample.exclude.<benchmark>) are
    # never drawn as replacements -- the first swap pass put one straight back in.
    excluded = set((cfg["sample"].get("exclude") or {}).get(benchmark) or [])
    unsampled = sorted(n for n in all_tasks if n not in sampled_src and n not in excluded)
    if new is None:
        old_dom = meta["tasks"][old]["domain"]
        same = [n for n in unsampled if (domain_of(all_tasks[n], troot) or [None])[0] == old_dom]
        pool = same or unsampled
        if not pool:
            raise SystemExit(f"{benchmark}: no unsampled task left to swap in")
        new = random.Random(f"{seed}:{benchmark}:swap:{old}").choice(pool)
    if new not in all_tasks:
        raise SystemExit(f"{benchmark}: no upstream task named {new}")
    if new in sampled_src:
        raise SystemExit(f"{benchmark}: {new} is already sampled")
    if new in excluded:
        raise SystemExit(f"{benchmark}: {new} is listed in sample.exclude (does not run on this host)")

    t = all_tasks[new]
    dom = domain_of(t, troot)
    used = set(meta["tasks"]) - {old}
    name, renamed = flat_name(t, dom, used)
    copy_task(t, dst_root / name)
    shutil.rmtree(dst_root / old, ignore_errors=True)
    removed = meta["tasks"].pop(old)
    meta["tasks"][name] = {
        "domain": dom[0] if dom else None,
        "domain_path": dom,
        "source_rel": str(t.relative_to(troot)),
        "task_name": task_name(t),
        "renamed_from": renamed,
    }
    meta["tasks"] = dict(sorted(meta["tasks"].items()))
    meta.setdefault("swaps", []).append({
        "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "removed": old, "removed_entry": removed, "added": name,
    })
    meta_p.write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n")
    return name


def main() -> int:
    ap = argparse.ArgumentParser(description="sample N tasks per benchmark into the flat v2 layout")
    ap.add_argument("--swap", action="append", default=None, metavar="OLD[=NEW]",
                    help="replace sampled task OLD with NEW (or a seeded pick from the same domain); "
                         "requires exactly one --benchmark; nothing else is resampled")
    ap.add_argument("--benchmark", action="append", default=None,
                    help="benchmark name; repeatable. default: every benchmark under the source root")
    ap.add_argument("--n", type=int, default=None, help="tasks per benchmark (default: config sample.n_per_benchmark)")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--config", default=None)
    ap.add_argument("--force", action="store_true", help="resample, replacing any existing subset")
    ap.add_argument("--pick", default=None,
                    help="comma-separated task names to take instead of a seeded draw; "
                         "requires exactly one --benchmark")
    args = ap.parse_args()

    cfg = load_config(args.config)
    n = args.n if args.n is not None else int(cfg["sample"]["n_per_benchmark"])
    seed = args.seed if args.seed is not None else int(cfg["sample"]["seed"])

    if args.swap:
        if not args.benchmark or len(args.benchmark) != 1:
            raise SystemExit("--swap needs exactly one --benchmark")
        for spec in args.swap:
            o, _, n = spec.partition("=")
            added = swap_task(cfg, args.benchmark[0], o, n or None, seed)
            log("sample", f"{args.benchmark[0]}: swapped {o} -> {added}")
        return 0

    src_root = REPO / cfg["sample"]["source_root"]
    names = args.benchmark or sorted(
        d.name for d in src_root.iterdir() if d.is_dir() and not d.name.startswith("."))

    pick = [x.strip() for x in args.pick.split(",") if x.strip()] if args.pick else None
    if pick and len(names) != 1:
        raise SystemExit("--pick needs exactly one --benchmark")
    for b in names:
        meta = sample_one(cfg, b, n, seed, args.force, pick=pick)
        s = meta["sampler"]
        doms = sorted({v["domain"] for v in meta["tasks"].values() if v["domain"]})
        log("sample", f"{b}: {s['n_sampled']}/{s['n_available']} tasks, "
                      f"{len(doms) or 1} domain(s) covered -> benchmarks/{b}/")
        for name, v in meta["tasks"].items():
            log("sample", f"    {name:<44} domain={v['domain'] or '-'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
