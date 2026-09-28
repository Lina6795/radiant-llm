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

# Evidence KB/DB/vector-store fallback: .env may leave these empty (they were
# historically inherited from a live service's env). Point at the repo-local
# M0 baseline ingestion artifacts when unset or nonexistent.
BASELINE_DIR="$PWD/artifacts/baseline/m0-20260922"
[ -n "${RADIANT_EVIDENCE_KB_DIR:-}" ] && [ -d "$RADIANT_EVIDENCE_KB_DIR" ] \
  || export RADIANT_EVIDENCE_KB_DIR="$BASELINE_DIR/output"
[ -n "${RADIANT_EVIDENCE_DB:-}" ] && [ -f "$RADIANT_EVIDENCE_DB" ] \
  || export RADIANT_EVIDENCE_DB="$BASELINE_DIR/evidence.db"
[ -n "${RADIANT_VECTOR_STORE:-}" ] && [ -d "$RADIANT_VECTOR_STORE" ] \
  || export RADIANT_VECTOR_STORE="$BASELINE_DIR/output/local_vector_store"
export RADIANT_EVIDENCE_SEARCH_MODE="${RADIANT_EVIDENCE_SEARCH_MODE:-hybrid}"

mkdir -p "$OUT"
echo "[gate] step 1/2: full-layer eval -> $OUT"
PYTHONPATH=app "$PY" -m eval.runner --all --out "$OUT" --baseline "$BASELINE" 2>&1 | tee "$OUT/eval.log"

echo "[gate] step 2/2: release gate vs $BASELINE"
PYTHONPATH=app "$PY" -m eval.release_gate \
  --current "$OUT/report.json" --baseline "$BASELINE" --out "$OUT/gate.json" 2>&1 | tee "$OUT/gate.log"
gate_rc=$?
echo "[gate] gate exit code: $gate_rc (report: $OUT/gate.json)"
exit $gate_rc
