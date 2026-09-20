#!/bin/bash
# Grade the world the agent left behind. Always writes a reward, on every path:
# harbor treats a missing reward file as an infrastructure error, and a crashed
# grader must read as "scored 0", not as "run broken".
set -uo pipefail

python3 /tests/verify.py
status=$?

if [ ! -s /logs/verifier/reward.txt ]; then
  echo "verify.py exited $status without writing a reward; recording 0" >&2
  echo 0 > /logs/verifier/reward.txt
fi
cat /logs/verifier/reward.txt
exit 0
