#!/usr/bin/env bash
# Stop a rollout driver AND the harbor runs it started, then clean their containers.
#
# Killing only the driver leaves its `harbor run` children alive as orphans
# (PPID 1). They keep writing to the same attempt directories, so a driver
# restarted over the same tasks races them and the rewards become unreliable --
# this happened once and cost a task's whole set of attempts. Always stop a driver
# through this script.
#
#   harness/stop_rollout.sh <benchmark> [--keep-containers]
set -uo pipefail
bench="${1:?usage: stop_rollout.sh <benchmark> [--keep-containers]}"
keep="${2:-}"
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

mapfile -t drivers < <(ps -eo pid=,args= | awk -v b="$bench" '$3=="harness/run_rollout.py" && $5==b {print $1}')
mapfile -t children < <(ps -eo pid=,args= | awk -v b="$bench" '/[h]arbor run -p/ && $0 ~ ("/benchmarks/" b "/") {print $1}')
echo "driver(s): ${drivers[*]:-none}    harbor run(s): ${#children[@]}"

# Driver first, so it cannot launch more while the children are being stopped.
for p in "${drivers[@]:-}"; do [[ -n "$p" ]] && kill "$p" 2>/dev/null && echo "  killed driver $p"; done
for p in "${children[@]:-}"; do [[ -n "$p" ]] && kill "$p" 2>/dev/null; done
sleep 5
# Anything that ignored SIGTERM.
for p in "${children[@]:-}"; do
  [[ -n "$p" ]] && kill -0 "$p" 2>/dev/null && kill -9 "$p" 2>/dev/null && echo "  SIGKILL $p"
done
sleep 2
left=$(ps -eo ppid=,args= | awk '/[h]arbor run -p/ && /v2\/benchmarks/ && $1==1' | wc -l)
echo "orphans (PPID 1) left: $left"

if [[ "$keep" != "--keep-containers" ]]; then
  mapfile -t names < <(cd "$root" && ls -d "benchmarks/$bench"/*/ 2>/dev/null | xargs -n1 basename | grep -v '^_')
  n=0
  for id in $(timeout 40 docker ps -q); do
    nm=$(timeout 10 docker inspect --format '{{.Name}}' "$id" 2>/dev/null | tr -d '/')
    for t in "${names[@]}"; do
      if [[ "$nm" == "$t"__* ]]; then timeout 20 docker rm -f "$id" >/dev/null 2>&1 && n=$((n+1)); break; fi
    done
  done
  echo "removed $n container(s) belonging to $bench"
fi
