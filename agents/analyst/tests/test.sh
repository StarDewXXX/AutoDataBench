#!/usr/bin/env bash
# Placeholder verifier. This task is launched with --disable-verification;
# run_analyst.py reads the JSON the analyst agent wrote to ADB_RESULT
# rather than a reward from here. It exists only so the task is well-formed.
set -euo pipefail
mkdir -p "$(dirname "${REWARD_PATH:-/tmp/reward.json}")" 2>/dev/null || true
echo '{"reward": 0.0, "note": "the analyst verdict is in the result JSON, not here"}' \
  > "${REWARD_PATH:-/tmp/reward.json}"
