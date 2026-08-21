#!/usr/bin/env bash
# One-command local test instance: launch the PRIVATE console tokenless and drive real UI flows
# with Playwright, then tear down. No Runalyze token or network needed. Two phases:
#   1. full  — a synthetic-seeded instance (dashboard, #67 dialog cycle, first-run hidden + step ③)
#   2. empty — a fresh dataless instance (first-run card step ①)
#   (+ noplan / settled / cold / blocked — see launch_and_drive calls below; "blocked" cuts /api/* + /healthz
#    and asserts every tile reaches a failure terminus with a working retry, UX-3)
#
#   ./test/run_local_test.sh
#
# Env knobs: PY (python, default venv/bin/python), PORT (base, default 8801),
#            NODE_PATH (default: a user-global install if present, else /usr/lib/node_modules),
#            KEEP=1 to keep the temp dir.
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${PY:-venv/bin/python}"
PORT="${PORT:-8801}"
# Playwright may be installed system-wide (sudo) or user-global (npm prefix=~/.local, no sudo —
# the only option on a box where pacman is wedged). Prefer whichever actually exists so the caller
# doesn't have to remember; an explicit NODE_PATH still wins.
if [ -z "${NODE_PATH:-}" ]; then
  for cand in "$HOME/.local/lib/node_modules" /usr/lib/node_modules; do
    [ -d "$cand/playwright" ] && { NODE_PATH="$cand"; break; }
  done
  export NODE_PATH="${NODE_PATH:-/usr/lib/node_modules}"
fi
WORK="$(mktemp -d)"
PIDS=()

cleanup() {
  for p in "${PIDS[@]:-}"; do [ -n "$p" ] && kill "$p" 2>/dev/null || true; done
  if [ "${KEEP:-0}" != "1" ]; then rm -rf "$WORK"; else echo "▸ kept: $WORK"; fi
}
trap cleanup EXIT

# launch_and_drive <db> <port> <mode> <label>
launch_and_drive() {
  local db="$1" port="$2" mode="$3" label="$4"
  echo "▸ [$label] launching tokenless PRIVATE instance on :$port"
  SH_DB="$db" RUNALYZE_TOKEN= SH_PORT="$port" "$PY" SparingHorse.py >"$WORK/server-$mode.log" 2>&1 &
  PIDS+=("$!")
  local up=0
  for _ in $(seq 1 40); do
    if curl -sf "http://127.0.0.1:$port/healthz" >/dev/null 2>&1; then up=1; break; fi
    sleep 0.5
  done
  if [ "$up" != 1 ]; then echo "✗ [$label] server didn't come up"; cat "$WORK/server-$mode.log"; return 1; fi
  echo "▸ [$label] driving flows (mode=$mode)"
  NODE_PATH="${NODE_PATH:-/usr/lib/node_modules}" \
    BASE_URL="http://127.0.0.1:$port" SHOT_DIR="$WORK" MODE="$mode" \
    node test/drive_local.mjs
}

# Phase 1 — full (synthetic-seeded)
FULL_DB="$WORK/full.db"
echo "▸ seeding $FULL_DB"
SH_DB="$FULL_DB" "$PY" SparingHorse.py seed >/dev/null
RC=0
launch_and_drive "$FULL_DB" "$PORT" full "full" || RC=$?

# Phase 2 — empty (fresh, unseeded; the app creates the schema on boot)
EMPTY_DB="$WORK/empty.db"
launch_and_drive "$EMPTY_DB" "$((PORT + 1))" empty "empty" || RC=$?

# Phase 3 — noplan (seeded history, no objective/plan; first-run step ③ + CTA generate-then-focus)
NOPLAN_DB="$WORK/noplan.db"
echo "▸ seeding $NOPLAN_DB (--no-objective)"
SH_DB="$NOPLAN_DB" "$PY" SparingHorse.py seed --no-objective >/dev/null
launch_and_drive "$NOPLAN_DB" "$((PORT + 2))" noplan "noplan" || RC=$?

# Phase 4 — settled (an A-race ran 5 days ago; the drift scorecard reckons it, §6s)
SETTLED_DB="$WORK/settled.db"
echo "▸ seeding $SETTLED_DB (--past-race)"
SH_DB="$SETTLED_DB" "$PY" SparingHorse.py seed --past-race >/dev/null
launch_and_drive "$SETTLED_DB" "$((PORT + 3))" settled "settled" || RC=$?

# Phase 5 — cold (§FT5: one 10k + an objective, NO snapshot/history; the engine cold-starts
# assertive — §FORM1, body evidence only — ramping from measured small, with a wide-band prediction
# seeded by VDOT inversion of the 10k)
COLD_DB="$WORK/cold.db"
echo "▸ seeding $COLD_DB (--cold)"
SH_DB="$COLD_DB" "$PY" SparingHorse.py seed --cold >/dev/null
launch_and_drive "$COLD_DB" "$((PORT + 4))" cold "cold" || RC=$?

# Phase 6 — blocked (UX-3): /api/* + /healthz unreachable ⇒ every loader reaches a failure terminus
launch_and_drive "$FULL_DB" "$((PORT + 5))" blocked "blocked" || RC=$?

echo "▸ artifacts: $WORK (server logs + screenshots)"
exit $RC
