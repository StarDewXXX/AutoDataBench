#!/usr/bin/env bash
# search-serper.sh "<query>" [num]
#
# Reference implementation of the AutoDataBench search contract, backed by the
# gateway's serper proxy. Every user is expected to be able to swap this for
# their own provider (Brave, Google CSE, an internal index) by pointing
# synthesis.search_script -- or $ADB_SEARCH_SCRIPT -- at their own executable.
#
# THE CONTRACT (all a replacement has to honour):
#   argv[1]  the query string                (required)
#   argv[2]  how many results to return      (optional, default 5)
#   stdout   normalized plain text, one result as three lines:
#              [n] <title>
#                  <url>
#                  <snippet>
#            separated by a blank line. Plain text, not JSON: it is what the
#            agent reads, and it has to look the same whatever provider is
#            behind it, so the agent's behaviour does not depend on whose
#            machine it runs on.
#   exit 0   on success; non-zero (with a message on stderr) on failure.
#
# NO SECRETS LIVE IN THIS FILE. It authenticates with the gateway key that the
# container already has in its environment -- the same key every other component
# uses -- so nothing here is sensitive and nothing has to be kept out of git.
# A replacement that needs its own key should read it from the environment too
# (inject with `--ae MY_KEY=...`), never hard-code it, and if it must be
# hard-coded, keep the file outside the repo or name it *.local.sh (gitignored).
set -uo pipefail

query="${1:-}"
num="${2:-5}"
[[ -n "$query" ]] || { echo "usage: $(basename "$0") \"<query>\" [num]" >&2; exit 2; }

base="${ANTHROPIC_BASE_URL:-}"
key="${ANTHROPIC_API_KEY:-}"
[[ -n "$base" && -n "$key" ]] || {
  echo "search-serper: ANTHROPIC_BASE_URL / ANTHROPIC_API_KEY not set in the environment" >&2
  exit 3
}

body="$(jq -n --arg q "$query" --argjson n "$num" '{q: $q, num: $n}')"
resp="$(curl -sS -m 45 -X POST "${base%/}/v1/tools/serper/search" \
  -H "Authorization: Bearer $key" \
  -H "Content-Type: application/json" \
  -d "$body" 2>/dev/null)" || {
  echo "search-serper: request failed" >&2; exit 4; }

# A gateway error comes back as JSON without .organic; surface it rather than
# printing an empty result set that reads like "the web has nothing on this".
if ! jq -e 'has("organic")' >/dev/null 2>&1 <<<"$resp"; then
  echo "search-serper: no results field in response: $(head -c 200 <<<"$resp")" >&2
  exit 5
fi

jq -r '
  if (.organic | length) == 0 then "（no results）"
  else
    [ .organic
      | to_entries[]
      | "[\(.key + 1)] \(.value.title // "(no title)")\n    \(.value.link // "")\n    \(.value.snippet // "" | gsub("\n"; " "))"
    ] | join("\n\n")
  end
' <<<"$resp"

# Credit accounting goes to stderr so it never pollutes the result text the
# agent parses, but still lands in the shim's log for quota tracking.
credits="$(jq -r '.credits // empty' <<<"$resp")"
[[ -n "$credits" ]] && echo "search-serper: credits used by this call: $credits" >&2
exit 0
