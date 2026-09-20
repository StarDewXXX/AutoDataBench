#!/usr/bin/env bash
# Overnight driver: finish the rollouts, derive the remaining rubrics, then run the
# full episode matrix (deepseek as researcher) benchmark by benchmark.
#
# Sequential by benchmark on purpose. Episodes inside a benchmark run concurrently
# (--episode-concurrency), and each concurrent episode can spawn nested containers
# while authoring, so running three benchmarks at once would multiply the container
# count by three with no way to bound it from here.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
export AUTODATABENCH_API_KEY="${AUTODATABENCH_API_KEY:?export the gateway key first}"
L=prep/_logs
EP_CONC="${EP_CONC:-4}"
RUN_ID="${RUN_ID:-$(date +%Y%m%d-%H%M%S)}"
say() { echo "[night $(date '+%H:%M:%S')] $*" | tee -a "$L/night.log"; }

say "run_id=$RUN_ID episode_concurrency=$EP_CONC"

# ---- 1. wait for the in-flight rollout drivers -----------------------------
while pgrep -f 'python3 harness/run_rollout.py' >/dev/null; do sleep 60; done
say "rollout drivers done"

# ---- 2. any task still short of 6 verdicts gets one more pass --------------
for b in automationbench terminal-bench tb-science; do
  short=$(python3 - "$b" <<'PY'
import json,sys,glob,os
b=sys.argv[1]; out=[]
for d in sorted(glob.glob(f"benchmarks/{b}/*/")):
    t=os.path.basename(d.rstrip('/'))
    if t.startswith('_'): continue
    s=f"prep/deepseek-v4-pro/{b}/{t}/rollout/summary.json"
    if not os.path.isfile(s): out.append(t); continue
    j=json.load(open(s))
    if j.get("attempts",0)<6 or j.get("attempts_no_verdict",0)>0: out.append(t)
print(" ".join(out))
PY
)
  if [[ -n "$short" ]]; then
    say "$b: topping up $short"
    for t in $short; do
      python3 harness/run_rollout.py --benchmark "$b" --task "$t" --prebuild --concurrency 6 \
        >> "$L/rollout-topup-$b.log" 2>&1
    done
  fi
done
say "rollouts complete"

# ---- 3. rubrics for every task that does not have one ---------------------
for b in automationbench terminal-bench tb-science; do
  python3 harness/run_analyst.py --benchmark "$b" --concurrency 8 >> "$L/analyst-$b.log" 2>&1
  say "$b: analyst done ($(ls rubrics/deepseek-v4-pro/$b/*/modes.json 2>/dev/null | wc -l) rubrics)"
done

# ---- 4. the experiment: one new task per original task -------------------
for b in automationbench terminal-bench tb-science; do
  say "$b: episodes starting"
  python3 harness/run_episode.py --benchmark "$b" --episodes 1 \
      --episode-concurrency "$EP_CONC" --run-id "$RUN_ID" >> "$L/episode-$b.log" 2>&1
  say "$b: episodes done"
  python3 harness/aggregate.py --run-id "$RUN_ID" >> "$L/aggregate.log" 2>&1 || true
done

say "ALL DONE run_id=$RUN_ID"
python3 harness/aggregate.py --run-id "$RUN_ID" | tee -a "$L/aggregate.log"
