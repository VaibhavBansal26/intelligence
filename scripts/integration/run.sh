#!/usr/bin/env bash
# End-to-end integration run: REAL Funnels + REAL Conversations + the Intelligence service.
#
#   scripts/integration/run.sh [--days N] [--scale X] [--record] [--keep-workdir] [--reuse DIR]
#
# 1. temp workdir; free random ports; install Node deps only if node_modules is missing
# 2. seed both apps (their own seed code), start them in dev mode on SQLite with a shifted clock and generous limits
# 3. generate_traffic.py drives the apps' HTTP APIs (same contacts in both apps)
# 4. restart both apps WITHOUT the clock shift and with default rate limits; create API keys + read peppers via their APIs
# 5. start the Intelligence service on a temp SQLite, run verify.py (conformance, sync, train, rank, score, anomalies,
#    insights, ask, auth failures, rate limits, join) and exit non-zero on any failed assertion
# 6. kill every process it started and delete the workdir (also on Ctrl-C / failure)
#
# Never touches /Library/PostgreSQL or the redis on 6379; no Redis or Postgres is used at all.
set -uo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
REPOS=$(cd "$ROOT/.." && pwd)
FUNNELS_DIR=${FUNNELS_DIR:-$REPOS/funnels}
CONV_DIR=${CONVERSATIONS_DIR:-$REPOS/conversations}
DAYS=${DAYS:-45}
SCALE=${SCALE:-1.0}
RECORD=""
KEEP=0
REUSE=""
HOLD=0
while [ $# -gt 0 ]; do
  case "$1" in
    --days) DAYS=$2; shift 2 ;;
    --scale) SCALE=$2; shift 2 ;;
    --record) RECORD="--record"; shift ;;
    --keep-workdir) KEEP=1; shift ;;
    --reuse) REUSE=$2; shift 2 ;;
    --hold) HOLD=1; shift ;;  # debugging: start everything, print the URLs and wait for Ctrl-C
    -h|--help) sed -n 2,16p "$0"; exit 0 ;;
    *) echo "unknown argument $1" >&2; exit 2 ;;
  esac
done

if [ -n "$REUSE" ]; then WORK=$REUSE; OWN_WORK=0; else WORK=$(mktemp -d "${TMPDIR:-/tmp}/intel-integ.XXXXXX"); OWN_WORK=1; fi
rm -rf "$WORK/intel"
mkdir -p "$WORK"/{f,c,intel,logs}
PIDS=()
STATUS=1
# A fixed (throw-away) signing secret for both apps: without one they generate a new secret on every start, and
# Conversations encrypts the identity pepper under JWT_SECRET, so the pepper would be unreadable after the restart
# between the traffic phase and the verification phase.
APP_JWT_SECRET=integration-test-only-jwt-secret-0123456789

log() { printf '\n== %s\n' "$*"; }
cleanup() {
  trap - EXIT INT TERM
  for pid in "${PIDS[@]:-}"; do
    [ -n "$pid" ] && kill "$pid" 2>/dev/null
  done
  sleep 1
  for pid in "${PIDS[@]:-}"; do
    [ -n "$pid" ] && kill -9 "$pid" 2>/dev/null
  done
  if [ "$OWN_WORK" = 1 ] && [ "$KEEP" = 0 ]; then rm -rf "$WORK"; else echo "workdir kept: $WORK"; fi
  exit "$STATUS"
}
trap cleanup EXIT INT TERM

free_port() { python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()'; }
wait_http() { # url, seconds
  local i
  for ((i = 0; i < $2 * 5; i++)); do
    if curl -s -o /dev/null --max-time 2 "$1"; then return 0; fi
    sleep 0.2
  done
  echo "timeout waiting for $1" >&2
  return 1
}

# -- 1. dependencies ----------------------------------------------------------------------------------------------
command -v node >/dev/null || { echo "node is required" >&2; exit 2; }
command -v uv >/dev/null || { echo "uv is required" >&2; exit 2; }
for d in "$FUNNELS_DIR" "$CONV_DIR"; do
  if [ ! -d "$d/server/node_modules" ]; then
    log "installing Node dependencies in $d/server (node_modules was missing)"
    (cd "$d" && npm ci --prefix server >"$WORK/logs/npm-$(basename "$d").log" 2>&1) || { echo "npm ci failed, see $WORK/logs" >&2; exit 2; }
  fi
done
PY=$(cd "$ROOT" && uv run python -c 'import sys; print(sys.executable)')
FPORT=$(free_port); CPORT=$(free_port); IPORT=$(free_port)
FURL=http://127.0.0.1:$FPORT; CURL_=http://127.0.0.1:$CPORT; IURL=http://127.0.0.1:$IPORT
echo "workdir $WORK  funnels :$FPORT  conversations :$CPORT  intelligence :$IPORT"

start_funnels() { # extra env...
  (cd "$FUNNELS_DIR/server" && exec env PORT=$FPORT HOST=127.0.0.1 DATABASE_PATH="$WORK/f/funnels.db" DATA_DIR="$WORK/f" NODE_ENV=development JWT_SECRET="$APP_JWT_SECRET" "$@" \
    node ${FAKE:+--import "$HERE/fakeclock.mjs"} --import tsx src/index.ts >>"$WORK/logs/funnels.log" 2>&1) &
  echo $! >"$WORK/funnels.pid"; disown
  PIDS+=("$(cat "$WORK/funnels.pid")")
  wait_http "$FURL/healthz" 60 || wait_http "$FURL/" 5
}
start_conv() {
  (cd "$CONV_DIR/server" && exec env PORT=$CPORT HOST=127.0.0.1 DATABASE_PATH="$WORK/c/conv.db" DATA_DIR="$WORK/c" NODE_ENV=development JWT_SECRET="$APP_JWT_SECRET" PUBLIC_URL="$CURL_" "$@" \
    node ${FAKE:+--import "$HERE/fakeclock.mjs"} --import tsx src/index.ts >>"$WORK/logs/conversations.log" 2>&1) &
  echo $! >"$WORK/conv.pid"; disown
  PIDS+=("$(cat "$WORK/conv.pid")")
  wait_http "$CURL_/healthz" 60 || wait_http "$CURL_/" 5
}
stop_pid() { # graceful TERM, wait up to 10 s, then KILL
  local pid i; pid=$(cat "$1" 2>/dev/null); [ -n "$pid" ] || return 0
  kill "$pid" 2>/dev/null
  for ((i = 0; i < 50; i++)); do kill -0 "$pid" 2>/dev/null || return 0; sleep 0.2; done
  kill -9 "$pid" 2>/dev/null; true
}

if [ -z "$REUSE" ] || [ ! -f "$WORK/traffic.json" ]; then
  # -- 2. seed both apps with their own seed code ---------------------------------------------------------------
  log "seeding Funnels and Conversations with their own seed scripts (npm run seed; both exit by themselves since the fixes N1/N2)"
  (cd "$FUNNELS_DIR/server" && DATABASE_PATH="$WORK/f/funnels.db" DATA_DIR="$WORK/f" NODE_ENV=development \
    timeout 120 npm run seed >"$WORK/logs/seed-funnels.log" 2>&1) || { cat "$WORK/logs/seed-funnels.log"; echo "funnels seed failed" >&2; exit 1; }
  (cd "$CONV_DIR/server" && DATABASE_PATH="$WORK/c/conv.db" DATA_DIR="$WORK/c" NODE_ENV=development \
    timeout 120 npm run seed >"$WORK/logs/seed-conv.log" 2>&1) || { cat "$WORK/logs/seed-conv.log"; echo "conversations seed failed" >&2; exit 1; }
  grep -q "Seeded demo workspace" "$WORK/logs/seed-conv.log" || { cat "$WORK/logs/seed-conv.log"; echo "conversations seed failed" >&2; exit 1; }

  # -- 3. phase A: shifted clock, generous limits, traffic -------------------------------------------------------
  log "phase A: start apps with a shifted clock (history building) and relaxed public rate limits"
  echo 0 >"$WORK/f.clock"; echo 0 >"$WORK/c.clock"
  FAKE=1 start_funnels FAKE_CLOCK_FILE="$WORK/f.clock" RATELIMIT_ENABLED=false EVENT_BUFFER_MS=0 || { tail -20 "$WORK/logs/funnels.log"; exit 1; }
  FAKE=1 start_conv FAKE_CLOCK_FILE="$WORK/c.clock" RATE_LIMIT_SCALE=1000 ACCESS_TOKEN_TTL_SECONDS=100000000 || { tail -20 "$WORK/logs/conversations.log"; exit 1; }
  log "generating traffic through the apps' HTTP APIs ($DAYS days, scale $SCALE)"
  "$PY" "$HERE/generate_traffic.py" --funnels-url "$FURL" --conversations-url "$CURL_" --funnels-clock "$WORK/f.clock" \
    --conversations-clock "$WORK/c.clock" --days "$DAYS" --scale "$SCALE" --out "$WORK/traffic.json" || { echo "traffic generation failed" >&2; exit 1; }
  stop_pid "$WORK/funnels.pid"; stop_pid "$WORK/conv.pid"
fi

# -- 4. phase B: real clock, default rate limits ------------------------------------------------------------------
log "phase B: restart both apps with the real clock and DEFAULT rate limits"
start_funnels || { tail -20 "$WORK/logs/funnels.log"; exit 1; }
start_conv || { tail -20 "$WORK/logs/conversations.log"; exit 1; }

# -- 5. intelligence service --------------------------------------------------------------------------------------
log "starting the Intelligence service (SQLite in the workdir)"
(cd "$ROOT" && exec env ENV=dev ADMIN_API_KEY=integration-admin ALLOW_PRIVATE_SOURCES=true DATABASE_URL="sqlite:///$WORK/intel/intelligence.db" \
  MODEL_DIR="$WORK/intel/models" COLUMNAR_DIR="$WORK/intel/columnar" LOG_LEVEL=WARNING \
  "$PY" -m uvicorn app.main:app_factory --factory --host 127.0.0.1 --port "$IPORT" >"$WORK/logs/intelligence.log" 2>&1) &
echo $! >"$WORK/intel.pid"; disown
PIDS+=("$(cat "$WORK/intel.pid")")
wait_http "$IURL/v1/health" 60 || wait_http "$IURL/docs" 10 || { tail -30 "$WORK/logs/intelligence.log"; exit 1; }

if [ "$HOLD" = 1 ]; then
  echo "HOLD funnels=$FURL conversations=$CURL_ intelligence=$IURL work=$WORK"
  while true; do sleep 5; done
fi
log "verify"
(cd "$ROOT" && "$PY" "$HERE/verify.py" --funnels-url "$FURL" --conversations-url "$CURL_" --intelligence-url "$IURL" \
  --admin-key integration-admin --workdir "$WORK" --traffic "$WORK/traffic.json" $RECORD)
STATUS=$?
if [ $STATUS -ne 0 ]; then echo; echo "---- tails of service logs ----"; tail -15 "$WORK/logs/intelligence.log"; fi
log "done (exit $STATUS)"
exit "$STATUS"
