#!/usr/bin/env bash
# Placeholder verifier for the researcher task. Launched with
# --disable-verification; the delivered task is scored on the host by
# run_score.py (difficulty from customer rollouts, plus the judge). This exists
# only so the task is a well-formed harbor task.
set -euo pipefail
mkdir -p "$(dirname "${REWARD_PATH:-/tmp/reward.json}")" 2>/dev/null || true
echo '{"reward": 0.0, "note": "the delivered task is scored on the host, not here"}' \
  > "${REWARD_PATH:-/tmp/reward.json}"
