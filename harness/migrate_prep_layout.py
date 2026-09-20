#!/usr/bin/env python3
"""One-off: move prep/ and rubrics/ trees under a customer-model directory.

The first v2 rollouts were written before prep and rubric artifacts were keyed by
customer model. This moves an existing tree into the new layout:

    prep/<benchmark>/          -> prep/<slug>/<benchmark>/
    rubrics/<benchmark>/       -> rubrics/<slug>/<benchmark>/

`rubrics/format.md` and `rubrics/gate.md` are model-independent and stay put.

Safe to re-run: a benchmark already under the slug is left alone. Refuses to move a
tree that another process is writing to unless --force, because a driver holding
the old path open would keep writing there after the move.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT, customer_slug, load_config, log, rel

SHARED = {"format.md", "gate.md", "_old-shape", "_logs"}


def in_use(path: Path) -> list[str]:
    """Driver processes whose command line names this tree, or harbor runs under it."""
    try:
        out = subprocess.run(["ps", "-eo", "pid=,args="], capture_output=True, text=True,
                             timeout=15).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    hits = []
    for line in out.splitlines():
        if "run_rollout.py" in line or "run_analyst.py" in line or "harbor run" in line:
            if str(path) in line:
                hits.append(line.strip()[:120])
    return hits


def move_tree(src: Path, dst: Path, force: bool) -> bool:
    if not src.exists():
        return False
    if dst.exists():
        log("migrate", f"{rel(dst)} already exists; leaving {rel(src)} alone")
        return False
    busy = in_use(src)
    if busy and not force:
        log("migrate", f"REFUSING {rel(src)}: in use by {len(busy)} process(es); "
                       f"wait for them to exit or pass --force")
        for b in busy[:3]:
            log("migrate", f"    {b}")
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    log("migrate", f"{rel(src)} -> {rel(dst)}")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="move prep/ and rubrics/ under the customer-model slug")
    ap.add_argument("--config", default=None)
    ap.add_argument("--benchmark", action="append", default=None,
                    help="benchmark to migrate; repeatable. default: every one found at the old level")
    ap.add_argument("--force", action="store_true", help="migrate even if a process is using the tree")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = load_config(args.config)
    slug = customer_slug(cfg)
    log("migrate", f"customer slug: {slug}")

    moved = 0
    for base in ("prep", "rubrics"):
        root = ROOT / base
        if not root.is_dir():
            continue
        names = args.benchmark or sorted(
            d.name for d in root.iterdir()
            if d.is_dir() and d.name not in SHARED and d.name != slug
            and not (root / slug / d.name).exists())
        for name in names:
            src, dst = root / name, root / slug / name
            if args.dry_run:
                log("migrate", f"would move {rel(src)} -> {rel(dst)}"
                               + (f"  (IN USE by {len(in_use(src))} process(es))" if in_use(src) else ""))
                continue
            moved += int(move_tree(src, dst, args.force))
    if not args.dry_run:
        log("migrate", f"moved {moved} tree(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
