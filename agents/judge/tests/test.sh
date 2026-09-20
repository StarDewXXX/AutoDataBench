#!/usr/bin/env bash
# Placeholder verifier. Launched with --disable-verification; run_score.py reads
# the JSON the judge wrote to ADB_RESULT rather than a reward from here.
set -euo pipefail
mkdir -p "$(dirname "${REWARD_PATH:-/tmp/reward.json}")" 2>/dev/null || true
echo '{"reward": 0.0, "note": "the judge verdict is in the result JSON, not here"}' \
  > "${REWARD_PATH:-/tmp/reward.json}"
