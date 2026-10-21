#!/usr/bin/env bash
#
# One scheduled Damien pass: rules first, the model on what they left, applied only where
# it is certain. Everything else is REPORTED, never applied — that is the whole point of
# the split, and it is what makes the pass safe to run unattended.
#
#   damien-pass.sh weekly              # the week just gone
#   damien-pass.sh monthly             # the month just gone
#   damien-pass.sh weekly --dry-run     # build and report; apply nothing
#   damien-pass.sh monthly --limit 200  # cap how much this pass looks at
#
# Reads nothing but the Damien CLI and its store. Exit 1 only on a real failure; a pass
# with nothing to do exits 0 and says so.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
py="$repo/.venv/bin/python"
[ -x "$py" ] || py="$(command -v python3)"
MIN_CONFIDENCE=0.9

period="${1:-weekly}"
shift || true
dry_run=0
limit=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) dry_run=1 ;;
    --limit) limit="${2:-}"; shift ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

case "$period" in
  weekly)
    window="in:inbox newer_than:7d" ;;
  monthly)
    # the calendar month just gone, whatever today is
    window="in:inbox after:$(date -v-1m +%Y/%m/01) before:$(date +%Y/%m/01)" ;;
  *)
    echo "usage: damien-pass.sh weekly|monthly [--dry-run] [--limit N]" >&2
    exit 2 ;;
esac

command -v damien >/dev/null 2>&1 || { echo "damien is not on PATH" >&2; exit 1; }

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

echo "Damien pass — $period"
echo "  window    $window"
echo "  certain   rules, your own corrections, and the model at >= $MIN_CONFIDENCE"

# --- build: writes to Damien's store, never to Gmail -------------------------
build_args=(-q "$window" --show 0 --json)
[ -n "$limit" ] && build_args+=(--limit "$limit")
if ! damien plan build "${build_args[@]}" > "$work/plan.json" 2> "$work/build.err"; then
  echo "  FAILED    the plan did not build:" >&2
  sed 's/^/            /' "$work/build.err" >&2
  exit 1
fi

eval "$("$py" - "$work/plan.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
counts = d.get("counts") or {}
print(f"PLAN={d['plan']}")
print(f"TOTAL={sum(counts.values())}")
print(f"CALLS={d.get('judge_calls') or 0}")
print(f"CACHED={d.get('judge_cached') or 0}")
print(f"USD={d.get('judge_usd') or 0}")
PY
)"

counts_line="$("$py" - "$work/plan.json" <<'PY'
import json, sys
counts = (json.load(open(sys.argv[1])).get("counts") or {})
print(" · ".join(f"{k} {v:,}" for k, v in sorted(counts.items())) or "nothing")
PY
)"
echo "  plan      $PLAN"
echo "  found     $TOTAL message(s) — $counts_line"
echo "  judgment  $CALLS call(s), $CACHED from cache, \$$USD"

if [ "$TOTAL" -eq 0 ]; then
  echo
  echo "nothing to do — the window is empty."
  exit 0
fi

# --- what the unattended rule will take, and what it will leave --------------
WAITING="$("$py" - "$PLAN" "$MIN_CONFIDENCE" "$repo" <<'PY'
import sys
sys.path.insert(0, sys.argv[3] + "/src")
from damien.config import load_settings
from damien.store import open_store

plan_id, threshold = sys.argv[1], float(sys.argv[2])
with open_store(load_settings()) as store:
    row = store.conn.execute(
        "SELECT COUNT(*) AS n FROM plan_items WHERE plan_id = ?"
        " AND NOT (source IN ('rule', 'person') OR COALESCE(confidence, 0) >= ?)",
        (plan_id, threshold),
    ).fetchone()
    print(int(row["n"]))
PY
)"

if [ "$dry_run" -eq 1 ]; then
  echo
  echo "DRY RUN — nothing was written to Gmail."
  echo "  certain   $((TOTAL - WAITING)) would be decided without you (keep is decided, not written)"
  echo "  waiting   $WAITING would be reported for a person"
  echo "  damien plan show $PLAN --sample 20"
  exit 0
fi

# --- apply: the deterministic slice only ------------------------------------
token="$(damien plan approve "$PLAN" | grep -oE '[0-9a-f]{16}' | head -1)"
if ! out="$(damien plan apply "$PLAN" --token "$token" --yes --min-confidence "$MIN_CONFIDENCE")"; then
  echo "  FAILED    the apply did not run:" >&2
  echo "$out" >&2
  echo "  nothing partial is untraceable: damien run list   (then: damien run undo <run_id>)" >&2
  exit 1
fi

run_id="$(printf '%s\n' "$out" | head -1 | awk '{print $2}')"
applied="$(printf '%s\n' "$out" | head -1 | grep -oE '[0-9]+ message' | grep -oE '[0-9]+' || echo 0)"

echo
echo "applied   $applied message(s)   run $run_id"
if [ "$WAITING" -gt 0 ]; then
  echo "waiting   $WAITING message(s) the model was not sure of — still approved, so:"
  echo "            damien plan show $PLAN --sample 20"
  echo "            token=\$(damien plan approve $PLAN | grep -oE '[0-9a-f]{16}')"
  echo "            damien plan apply $PLAN --token \"\$token\" --yes"
fi
echo "undo      damien run undo $run_id"
