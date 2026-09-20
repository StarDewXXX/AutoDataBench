# Sourced by adb-oracle / adb-testrun.
#
# AutoDataBench_thread_flags <flag> <task-dir> prints, one token per line, the
# harbor flag pairs that pin every numeric-library thread pool to the task's
# declared [environment] cpus, e.g.:  --ae OMP_NUM_THREADS=2  --ae ...
#
# Why: docker --cpus throttles a container's CPU time but does NOT shrink the
# core count it sees (nproc stays at the host's value), so numpy/scipy/BLAS
# default to one thread per host core and pile dozens of threads onto a 1-2 core
# quota -- context-switch churn and a load spike with no speedup. Setting the
# thread pools to the task's cpus makes the thread count match the quota.
AutoDataBench_thread_flags() {
  local flag="$1" task="$2" cpus="" v
  cpus="$(awk '
    /^\[/{ sec=$0 }
    sec=="[environment]" && /^[[:space:]]*cpus[[:space:]]*=/ {
      v=$0; sub(/.*=/,"",v); sub(/#.*/,"",v); gsub(/[^0-9.]/,"",v); sub(/\..*/,"",v)
      print v; exit
    }' "$task/task.toml" 2>/dev/null)"
  [[ "$cpus" =~ ^[0-9]+$ && "$cpus" -ge 1 ]] || cpus=4
  for v in OMP_NUM_THREADS OPENBLAS_NUM_THREADS MKL_NUM_THREADS \
           NUMEXPR_NUM_THREADS VECLIB_MAXIMUM_THREADS OMP_THREAD_LIMIT; do
    printf '%s\n%s\n' "$flag" "$v=$cpus"
  done
}
