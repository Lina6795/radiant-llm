#!/usr/bin/env bash
# S10: one-command release evaluation — full-layer eval + Release Gate.
#
# Usage: bash deploy/run_release_gate.sh [run_id]
#
# Steps:
#   1. run ALL eval layers against the live configuration
#      (env sourced from Docker_Executable/.env, values never echoed)
#   2. compare the candidate report against the M8 baseline with the
#      fail-closed Release Gate
#   3. persist report.json/report.md + gate.json under artifacts/eval/<run_id>/
#
# Exit code: 0 iff the gate passes.
set -u
set -o pipefail
cd "$(dirname "$0")/.."
PY="runtime/bin/python"
RUN_ID="${1:-s10-$(date +%Y%m%d-%H%M%S)}"
OUT="artifacts/eval/$RUN_ID"
BASELINE="artifacts/eval/m8-baseline-20260922/report.json"

set -a
[ -f Docker_Executable/.env ] && . Docker_Executable/.env
set +a
export LD_LIBRARY_PATH="$PWD/runtime/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export PYTHONUNBUFFERED=1

mkdir -p "$OUT"
echo "[gate] step 1/2: full-layer eval -> $OUT"
PYTHONPATH=app "$PY" -m eval.runner --all --out "$OUT" --baseline "$BASELINE" 2>&1 | tee "$OUT/eval.log"

echo "[gate] step 2/2: release gate vs $BASELINE"
PYTHONPATH=app "$PY" -m eval.release_gate \
  --current "$OUT/report.json" --baseline "$BASELINE" --out "$OUT/gate.json" 2>&1 | tee "$OUT/gate.log"
gate_rc=$?
echo "[gate] gate exit code: $gate_rc (report: $OUT/gate.json)"
exit $gate_rc
