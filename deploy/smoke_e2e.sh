#!/usr/bin/env bash
# S9-6/S10: one-key small-data E2E smoke.
# Covers: query(unified) / retrieve(hybrid) / context(package) / verify(claims)
#         memory(write+read gate) / resume(plan persistence) / trace(events).
#
# Modes:
#   bash deploy/smoke_e2e.sh [port]           probe an already-running service
#                                             (default port 8080)
#   SMOKE_START=1 bash deploy/smoke_e2e.sh    self-start a temp service on a
#                                             free port, probe it, then stop it
#
# SMOKE_START=1 sources Docker_Executable/.env for the usual RADIANT_* env
# (values are never echoed) and mirrors start_radiant.sh's launch convention.
# All output is tee'd to artifacts/verification/smoke-<timestamp>.log; the
# temp server's own log goes to smoke-<timestamp>.server.log alongside it.
set -u
set -o pipefail
cd "$(dirname "$0")/.."
REPO="$PWD"
PY="$REPO/runtime/bin/python"
TS="$(date +%Y%m%d-%H%M%S)"
LOG_DIR="artifacts/verification"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/smoke-$TS.log"
SERVER_LOG="$LOG_DIR/smoke-$TS.server.log"

SERVER_PID=""
if [ "${SMOKE_START:-0}" = "1" ]; then
  set -a
  [ -f Docker_Executable/.env ] && . Docker_Executable/.env
  set +a
  export LD_LIBRARY_PATH="$PWD/runtime/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
  export PYTHONUNBUFFERED=1
  # Evidence KB/DB/vector-store fallback (see run_release_gate.sh): .env may
  # leave these empty; fall back to the repo-local M0 baseline artifacts.
  BASELINE_DIR="$PWD/artifacts/baseline/m0-20260922"
  [ -n "${RADIANT_EVIDENCE_KB_DIR:-}" ] && [ -d "$RADIANT_EVIDENCE_KB_DIR" ] \
    || export RADIANT_EVIDENCE_KB_DIR="$BASELINE_DIR/output"
  [ -n "${RADIANT_EVIDENCE_DB:-}" ] && [ -f "$RADIANT_EVIDENCE_DB" ] \
    || export RADIANT_EVIDENCE_DB="$BASELINE_DIR/evidence.db"
  [ -n "${RADIANT_VECTOR_STORE:-}" ] && [ -d "$RADIANT_VECTOR_STORE" ] \
    || export RADIANT_VECTOR_STORE="$BASELINE_DIR/output/local_vector_store"
  export RADIANT_EVIDENCE_SEARCH_MODE="${RADIANT_EVIDENCE_SEARCH_MODE:-hybrid}"
  PORT="${1:-$($PY -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')}"
  echo "[smoke] SMOKE_START=1: starting temp service on port $PORT" | tee "$LOG"
  (cd app && RADIANT_LLM_PORT="$PORT" "$PY" api.py) > "$SERVER_LOG" 2>&1 &
  SERVER_PID=$!
  cleanup() { [ -n "$SERVER_PID" ] && kill "$SERVER_PID" 2>/dev/null; }
  trap cleanup EXIT
  echo "[smoke] waiting for readiness (up to 180s)..." | tee -a "$LOG"
  ready=0
  for _ in $(seq 1 180); do
    if curl -sf -o /dev/null "http://127.0.0.1:$PORT/health/ready" \
       || curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/health/ready" | grep -q 503; then
      ready=1; break
    fi
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
      echo "[smoke] FATAL: temp service died; see $SERVER_LOG" | tee -a "$LOG"
      exit 1
    fi
    sleep 1
  done
  if [ "$ready" != "1" ]; then
    echo "[smoke] FATAL: service not ready after 180s; see $SERVER_LOG" | tee -a "$LOG"
    exit 1
  fi
  echo "[smoke] service up (PID $SERVER_PID, port $PORT)" | tee -a "$LOG"
else
  PORT="${1:-8080}"
fi
BASE="http://127.0.0.1:${PORT}"

$PY - "$BASE" "$TS" <<'EOF' 2>&1 | tee -a "$LOG"
import json, sys, time, urllib.request, urllib.error

BASE = sys.argv[1]
TAG = sys.argv[2]  # unique per invocation so re-runs don't hit duplicate_value
failures = []

def http(method, path, body=None, ok=(200, 201, 202), timeout=120):
    req = urllib.request.Request(
        BASE + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode() or "{}")

def check(name, cond, detail=""):
    print(("PASS" if cond else "FAIL"), name, detail[:120])
    if not cond:
        failures.append(name)

# 0. readiness
st, body = http("GET", "/health/ready", ok=(200, 503))
check("ready endpoint", st in (200, 503) and "checks" in body)

# 1. query through unified /query (control chain)
Q = ("What two main components does the Transformer architecture consist of? "
     "Answer with the PDF name, page number and Evidence ID.")
# 同步控制链含冷启动与 LLM 调用，放宽到 600s
st, body = http("POST", "/query", {"query": Q}, timeout=600)
check("query control chain", body.get("chain") == "control", str(body.get("verify_action")))
run_id = body.get("run_id")

# 2. retrieve (hybrid lanes present)
if run_id:
    st, snap = http("GET", f"/runs/{run_id}")
    steps = {s["step_id"]: s for s in snap.get("steps", [])}
    search_out = (steps.get("s1-search") or {}).get("output") or {}
    lanes = (search_out.get("lanes") or {})
    check("retrieve hybrid lanes", all(lanes.get(k) for k in ("bm25", "dense", "fused")),
          search_out.get("retrieval", ""))

    # 3. context package trace
    ctx = (steps.get("s3-context") or {}).get("output") or {}
    pkg = ctx.get("context_package") or {}
    check("context package", bool(pkg.get("package_fingerprint")) and pkg.get("usage", {}).get("evidence", 0) > 0)

    # 4. verify claims with verdicts
    verify = (steps.get("s5-verify") or {}).get("output") or {}
    check("verify claims", bool(verify.get("claims")), str(verify.get("verify_action")))

    # 5. trace: events contain binding + verify path
    req = urllib.request.Request(f"{BASE}/runs/{run_id}/events?after_seq=0")
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode()
    check("trace events", "binding_resolved" in raw and "run_completed" in raw)

    # 6. resume of a terminal run is stable (no second execution)
    st, res = http("POST", f"/runs/{run_id}/resume")
    check("resume terminal stable", st == 409, str(res.get("detail"))[:80])

# 7. memory write gate + read gate
smoke_subject = f"smoke-pref-{TAG}"
st, w = http("POST", "/memories", {
    "category": "user_fact", "subject": smoke_subject, "value": "DeepSeek",
    "workspace": "default", "write_reason": "smoke test", "confidence": 0.95,
    "user_confirmed": True, "user_confirmation_id": f"smoke-{TAG}",
    "provenance": {"origin": "user", "source_session_id": "smoke", "user_confirmation_id": f"smoke-{TAG}"}})
check("memory write gate", w.get("outcome") == "allow", str(w.get("outcome")))
st, r = http("GET", f"/memories?q={smoke_subject}&workspace=default")
check("memory read gate", any(x["value"] == "DeepSeek" for x in r.get("records", [])))
st, denied = http("POST", "/memories", {
    "category": "user_fact", "subject": "smoke-bad", "value": "x",
    "workspace": "default", "write_reason": "smoke", "confidence": 0.9,
    "provenance": {"origin": "model", "evidence_id": "ev-fake"}})
check("memory evidence-as-fact rejected", denied.get("outcome") == "reject", str(denied.get("reasons")))

# 8. control-chat frontend (S11-E/F): route, assets, dashboard entry
def http_text(path, timeout=15):
    try:
        with urllib.request.urlopen(BASE + path, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:
        return -1, f"{type(exc).__name__}: {exc}"

st, html = http_text("/control-chat/")
check("control-chat page", st == 200 and "app.js" in html and "style.css" in html, f"http {st}")
st, js = http_text("/control-chat/app.js")
check("control-chat assets", st == 200 and "/runs" in js and "/stream-query" not in js,
      f"http {st}")
st, dash = http_text("/dashboard/")
check("dashboard links control-chat", st == 200 and "/control-chat/" in dash, f"http {st}")

print("---")
print("SMOKE", "FAILED" if failures else "OK", failures or "")
sys.exit(1 if failures else 0)
EOFsmoke_rc=$?
echo "[smoke] log: $LOG"
[ -n "$SERVER_PID" ] && echo "[smoke] server log: $SERVER_LOG (temp service stopped)"
exit $smoke_rc
