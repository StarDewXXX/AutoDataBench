# Sourced by adb-oracle / adb-testrun.
#
# AutoDataBench_acquire_slot blocks until one of ADB_MAX_CONCURRENT (default 8)
# docker slots is free, then holds it via an flock'd fd for the life of the
# calling process. Since the caller runs `harbor run` in the foreground and
# then exits, the slot is held for exactly the duration of that one child
# container batch and released automatically on exit. This caps how many
# containers the synthesis agent can have running at once, no matter how many
# oracle/testrun calls it fires in parallel.
AutoDataBench_acquire_slot() {
  local n="${ADB_MAX_CONCURRENT:-8}"
  local dir="${ADB_WORK:-/tmp}/.AutoDataBench-slots"
  mkdir -p "$dir"
  local waited=0 i fd
  while :; do
    for ((i=0; i<n; i++)); do
      exec {fd}>>"$dir/slot.$i" || continue
      if flock -n "$fd"; then
        ADB_SLOT_FD=$fd
        return 0
      fi
      exec {fd}>&-
    done
    sleep 2; waited=$((waited+2))
    if (( waited % 60 == 0 )); then
      echo "[AutoDataBench-slots] all $n docker slots busy; waiting for one to free up..." >&2
    fi
  done
}
