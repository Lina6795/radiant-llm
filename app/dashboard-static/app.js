/* RADIANT-Control M9 Dashboard — vanilla JS, no build step. */
"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

async function api(path, opts = {}) {
  const resp = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  let body = null;
  try { body = await resp.json(); } catch (e) { /* SSE etc. */ }
  if (!resp.ok) {
    const detail = body && body.detail ? body.detail : resp.statusText;
    const err = new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    err.status = resp.status;
    err.body = body;
    throw err;
  }
  return body;
}

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function statePill(s) { return `<span class="state ${esc(s)}">${esc(s)}</span>`; }
function shortId(id) { return id ? String(id).slice(0, 18) : ""; }
function fmtTime(ts) {
  if (!ts) return "—";
  const d = typeof ts === "number" ? new Date(ts * 1000) : new Date(ts);
  return isNaN(d) ? String(ts) : d.toLocaleString();
}

/* ---------------- tabs ---------------- */
const tabs = ["home", "runs", "evidence", "reviews", "benchmarks"];
function activateTab() {
  const hash = (location.hash || "#home").slice(1).split("/")[0];
  const tab = tabs.includes(hash) ? hash : "home";
  tabs.forEach((t) => {
    $("#tab-" + t).classList.toggle("hidden", t !== tab);
    $(`nav a[data-tab="${t}"]`).classList.toggle("active", t === tab);
  });
  if (tab === "runs") loadRuns();
  if (tab === "reviews") loadReviews();
  if (tab === "benchmarks") loadBenchmarks();
}
window.addEventListener("hashchange", activateTab);

/* ---------------- health + home ---------------- */
async function loadHome() {
  const pill = $("#health-pill");
  try {
    await api("/health");
    pill.textContent = "health: ok";
    pill.className = "pill ok";
  } catch (e) {
    pill.textContent = "health: 不可用";
    pill.className = "pill bad";
  }
  const cards = [];
  try {
    const ms = await api("/model-settings");
    cards.push(`<div class="card"><h3>对话模型</h3><p>initialized: ${esc(ms.model_initialized)}<br>reasoning: ${esc(ms.reasoning_effort)} · temp: ${esc(ms.temperature)}</p></div>`);
  } catch (e) { cards.push(`<div class="card"><h3>对话模型</h3><p>${esc(e.message)}</p></div>`); }
  try {
    const runs = await api("/runs?limit=1");
    cards.push(`<div class="card"><h3>Runs</h3><p>累计 ${esc(runs.total)} 个 run</p></div>`);
  } catch (e) { /* durable DB 未初始化时忽略 */ }
  try {
    const reviews = await api("/reviews");
    cards.push(`<div class="card"><h3>Review 队列</h3><p>待审 ${esc(reviews.total)} 项</p></div>`);
    const badge = $("#review-badge");
    badge.textContent = reviews.total;
    badge.classList.toggle("hidden", !reviews.total);
  } catch (e) { /* ignore */ }
  try {
    const docs = await api("/documents?limit=1");
    cards.push(`<div class="card"><h3>Evidence 文档</h3><p>文档版本 ${esc(docs.total)} 条</p></div>`);
  } catch (e) { /* ignore */ }
  $("#home-status").innerHTML = cards.join("");
}

/* ---------------- ingest ---------------- */
$("#ingest-btn").addEventListener("click", async () => {
  const out = $("#ingest-result");
  out.textContent = "提交中…";
  try {
    const job = await api("/documents/ingest", {
      method: "POST",
      body: JSON.stringify({
        input_dir: $("#ingest-dir").value,
        workspace_id: $("#ingest-ws").value,
        parse: $("#ingest-parse").checked,
      }),
    });
    out.textContent = `job_id=${job.job_id} 状态轮询中…`;
    const timer = setInterval(async () => {
      try {
        const s = await api(`/documents/ingest/${job.job_id}`);
        out.textContent = JSON.stringify(s, null, 2);
        if (s.status === "done" || s.status === "failed") clearInterval(timer);
      } catch (e) { clearInterval(timer); out.textContent = e.message; }
    }, 1500);
  } catch (e) { out.textContent = `失败: ${e.message}`; }
});

/* ---------------- runs ---------------- */
let selectedRunId = null;
let runEventSource = null;

async function loadRuns() {
  try {
    const data = await api("/runs?limit=100");
    const tbody = $("#runs-table tbody");
    tbody.innerHTML = data.items.map((r) => `
      <tr data-run="${esc(r.run_id)}" class="${r.run_id === selectedRunId ? "selected" : ""}">
        <td title="${esc(r.run_id)}">${esc(shortId(r.run_id))}</td>
        <td>${esc((r.goal || "").slice(0, 42))}</td>
        <td>${statePill(r.state)}</td>
        <td>${esc(r.workspace)}</td>
        <td>${fmtTime(r.updated_at)}</td>
      </tr>`).join("") || `<tr><td colspan="5" class="muted">暂无 run</td></tr>`;
    tbody.querySelectorAll("tr[data-run]").forEach((tr) =>
      tr.addEventListener("click", () => selectRun(tr.dataset.run)));
  } catch (e) {
    $("#runs-table tbody").innerHTML = `<tr><td colspan="5">${esc(e.message)}</td></tr>`;
  }
}

$("#new-run-btn").addEventListener("click", async () => {
  const msg = $("#new-run-msg");
  msg.textContent = "…";
  try {
    const r = await api("/runs", {
      method: "POST",
      body: JSON.stringify({ goal: $("#new-run-goal").value, workspace: $("#new-run-ws").value }),
    });
    if (!r.run_id) {
      msg.textContent = `router: ${r.status} (${(r.reason_codes || []).join(", ")})`;
    } else {
      msg.textContent = `run_id=${r.run_id}`;
      selectedRunId = r.run_id;
      setTimeout(() => { loadRuns(); selectRun(r.run_id); }, 400);
    }
  } catch (e) { msg.textContent = `失败: ${e.message}`; }
});

async function selectRun(runId) {
  selectedRunId = runId;
  $("#run-detail").classList.remove("hidden");
  await refreshRunDetail();
  startRunEvents(runId);
  loadRuns();
}

async function refreshRunDetail() {
  if (!selectedRunId) return;
  try {
    const r = await api(`/runs/${selectedRunId}`);
    $("#run-detail-title").innerHTML = `Run ${esc(r.run_id)} ${statePill(r.state)}`;
    $("#run-detail-meta").innerHTML =
      `<div class="muted">goal: ${esc(r.goal)}<br>workspace: ${esc(r.workspace)} · owner: ${esc(r.owner)} · ` +
      `fingerprint: ${esc(r.config_fingerprint)} · cancel_requested: ${esc(r.cancel_requested)} · updated: ${fmtTime(r.updated_at)}</div>`;
    $("#run-steps tbody").innerHTML = (r.steps || []).map((s) => `
      <tr>
        <td>${esc(s.step_id)}</td><td>${esc(s.tool || "—")}</td><td>${esc(s.risk || "—")}</td>
        <td>${statePill(s.state)}</td><td>${esc(s.attempt)}</td>
        <td>${esc(s.error ? (s.error.code || JSON.stringify(s.error)) : "")}</td>
      </tr>`).join("") || `<tr><td colspan="6" class="muted">无节点</td></tr>`;
  } catch (e) {
    $("#run-detail-title").textContent = e.message;
  }
}

function startRunEvents(runId) {
  if (runEventSource) { runEventSource.close(); runEventSource = null; }
  const box = $("#run-events");
  box.innerHTML = "";
  const es = new EventSource(`/runs/${runId}/events`);
  runEventSource = es;
  const types = ["run_started", "node_started", "node_completed", "node_failed", "node_retried",
    "node_skipped", "waiting_review", "run_completed", "run_failed", "run_cancelled", "run_resumed",
    "lease_acquired", "lease_taken_over"];
  types.forEach((t) => es.addEventListener(t, (ev) => {
    if (runId !== selectedRunId) return;
    const line = document.createElement("div");
    line.className = "ev";
    line.innerHTML = `<span class="t">#${esc(ev.lastEventId)} ${esc(t)}</span> ${esc(ev.data)}`;
    box.appendChild(line);
    box.scrollTop = box.scrollHeight;
    refreshRunDetail();
  }));
  es.addEventListener("end", () => { es.close(); if (runEventSource === es) runEventSource = null; refreshRunDetail(); });
  es.onerror = () => { /* EventSource 自动带 Last-Event-ID 重连 */ };
}

$("#run-cancel-btn").addEventListener("click", async () => {
  if (!selectedRunId) return;
  try { await api(`/runs/${selectedRunId}/cancel`, { method: "POST", body: "{}" }); } catch (e) { alert(e.message); }
  setTimeout(refreshRunDetail, 300);
});
$("#run-resume-btn").addEventListener("click", async () => {
  if (!selectedRunId) return;
  try { await api(`/runs/${selectedRunId}/resume`, { method: "POST", body: "{}" }); } catch (e) { alert(e.message); }
  setTimeout(refreshRunDetail, 300);
});
$("#run-refresh-btn").addEventListener("click", refreshRunDetail);
setInterval(() => { if (!$("#tab-runs").classList.contains("hidden")) loadRuns(); }, 8000);

/* ---------------- evidence ---------------- */
async function searchEvidence() {
  const params = new URLSearchParams();
  if ($("#ev-doc").value) params.set("document_id", $("#ev-doc").value);
  if ($("#ev-modality").value) params.set("modality", $("#ev-modality").value);
  if ($("#ev-degraded").checked) params.set("include_degraded", "true");
  params.set("limit", "100");
  const data = await api(`/evidence?${params}`);
  $("#ev-table thead").innerHTML = "<tr><th>evidence_id</th><th>document</th><th>modality</th><th>page</th><th>degraded</th></tr>";
  $("#ev-table tbody").innerHTML = (data.items || []).map((it) => `
    <tr data-ev="${esc(it.evidence_id)}">
      <td title="${esc(it.evidence_id)}">${esc(shortId(it.evidence_id))}</td>
      <td title="${esc(it.document_id)}">${esc(shortId(it.document_id))}</td>
      <td>${esc(it.modality)}</td><td>${esc(it.page ?? "")}</td><td>${esc(it.degraded)}</td>
    </tr>`).join("") || `<tr><td colspan="5" class="muted">无结果（total=${esc(data.total)}）</td></tr>`;
  $("#ev-table tbody").querySelectorAll("tr[data-ev]").forEach((tr) =>
    tr.addEventListener("click", () => showEvidence(tr.dataset.ev)));
}

async function showDocuments() {
  const data = await api("/documents?limit=100");
  $("#ev-table thead").innerHTML = "<tr><th>document_id</th><th>version</th><th>source</th><th>pages</th><th>status</th></tr>";
  $("#ev-table tbody").innerHTML = (data.items || []).map((d) => `
    <tr>
      <td title="${esc(d.document_id)}">${esc(shortId(d.document_id))}</td>
      <td>${esc(d.document_version)}</td><td>${esc(d.source || "")}</td>
      <td>${esc(d.page_count ?? "")}</td><td>${esc(d.artifact_status || "")}</td>
    </tr>`).join("") || `<tr><td colspan="5" class="muted">无文档</td></tr>`;
}

async function showEvidence(evidenceId) {
  try {
    const item = await api(`/evidence/${encodeURIComponent(evidenceId)}`);
    $("#ev-detail").classList.remove("hidden");
    $("#ev-detail-json").textContent = JSON.stringify(item, null, 2);
  } catch (e) { alert(e.message); }
}
$("#ev-search-btn").addEventListener("click", () => searchEvidence().catch((e) => alert(e.message)));
$("#ev-docs-btn").addEventListener("click", () => showDocuments().catch((e) => alert(e.message)));

/* ---------------- reviews ---------------- */
async function loadReviews() {
  try {
    const data = await api("/reviews");
    const badge = $("#review-badge");
    badge.textContent = data.total;
    badge.classList.toggle("hidden", !data.total);
    $("#reviews-list").innerHTML = (data.items || []).map((it) => `
      <div class="review-card" data-rv="${esc(it.review_id)}">
        <div class="meta">${esc(it.review_id)} · run ${esc(it.run_id)} · workspace ${esc(it.workspace)} · ${esc(it.created_at)}</div>
        <div>风险原因: ${esc((it.risk_reasons || []).join(", "))}</div>
        <div class="muted">goal: ${esc((it.metadata && it.metadata.goal) || "")}</div>
        <textarea placeholder="rationale（决策理由）"></textarea>
        <input class="edited" placeholder="edited_answer（仅 edit 决策需要）" size="40">
        <div class="btnrow">
          <button data-dec="approve">Approve</button>
          <button data-dec="edit" class="secondary">Edit</button>
          <button data-dec="reject" class="danger">Reject</button>
        </div>
      </div>`).join("") || `<div class="panel muted">队列为空</div>`;
    $$("#reviews-list .review-card button").forEach((btn) =>
      btn.addEventListener("click", () => submitDecision(btn)));
  } catch (e) {
    $("#reviews-list").innerHTML = `<div class="panel">${esc(e.message)}</div>`;
  }
}

async function submitDecision(btn) {
  const card = btn.closest(".review-card");
  const reviewId = card.dataset.rv;
  const decision = btn.dataset.dec;
  const rationale = card.querySelector("textarea").value;
  const edited = card.querySelector(".edited").value;
  btn.disabled = true;
  try {
    await api(`/reviews/${reviewId}/decision`, {
      method: "POST",
      body: JSON.stringify({
        decision,
        reviewer_id: $("#reviewer-id").value,
        rationale,
        edited_answer: edited || null,
      }),
    });
    loadReviews();
  } catch (e) { alert(`决策失败: ${e.message}`); btn.disabled = false; }
}
$("#reviews-refresh").addEventListener("click", loadReviews);
setInterval(() => { if (!$("#tab-reviews").classList.contains("hidden")) loadReviews(); }, 10000);

/* ---------------- benchmarks ---------------- */
const BM_LAYERS = ["control", "durable", "retrieval", "context", "memory", "verification"];
let selectedBenchmark = null;

function renderLayerCheckboxes() {
  $("#bm-layers").innerHTML = BM_LAYERS.map((l) =>
    `<label><input type="checkbox" class="bm-layer" value="${l}" checked> ${l}</label>`).join("");
}

$("#bm-run-btn").addEventListener("click", async () => {
  const layers = $$(".bm-layer").filter((c) => c.checked).map((c) => c.value);
  const msg = $("#bm-msg");
  msg.textContent = "提交中…";
  try {
    const r = await api("/benchmarks/run", {
      method: "POST",
      body: JSON.stringify(layers.length === BM_LAYERS.length ? {} : { layers }),
    });
    msg.textContent = `benchmark_id=${r.benchmark_id} 后台运行中`;
    selectedBenchmark = r.benchmark_id;
    setTimeout(loadBenchmarks, 1000);
  } catch (e) { msg.textContent = `失败: ${e.message}`; }
});
$("#bm-refresh-btn").addEventListener("click", loadBenchmarks);

async function loadBenchmarks() {
  try {
    const data = await api("/benchmarks");
    $("#bm-table tbody").innerHTML = (data.items || []).map((b) => {
      const s = b.summary || {};
      return `<tr data-bm="${esc(b.benchmark_id)}" class="${b.benchmark_id === selectedBenchmark ? "selected" : ""}">
        <td title="${esc(b.benchmark_id)}">${esc(b.benchmark_id)}</td>
        <td>${statePill(b.status)}</td>
        <td>${esc(b.created_at || b.started_at || "")}</td>
        <td>${s.cases_total != null ? `${s.cases_passed}/${s.cases_total}` : "—"}</td>
      </tr>`;
    }).join("") || `<tr><td colspan="4" class="muted">暂无记录</td></tr>`;
    $("#bm-table tbody").querySelectorAll("tr[data-bm]").forEach((tr) =>
      tr.addEventListener("click", () => showBenchmark(tr.dataset.bm)));
  } catch (e) {
    $("#bm-table tbody").innerHTML = `<tr><td colspan="4">${esc(e.message)}</td></tr>`;
  }
}

async function showBenchmark(id) {
  selectedBenchmark = id;
  try {
    const b = await api(`/benchmarks/${id}`);
    $("#bm-detail").classList.remove("hidden");
    $("#bm-detail-title").innerHTML = `${esc(id)} ${statePill(b.status)}`;
    const s = (b.report && b.report.summary) || {};
    $("#bm-summary").innerHTML = `<div class="muted">layers ok/skip/fail: ${s.layers_ok ?? "—"}/${s.layers_skipped ?? "—"}/${s.layers_failed ?? "—"} · ` +
      `cases pass/fail/skip: ${s.cases_passed ?? "—"}/${s.cases_failed ?? "—"}/${s.cases_skipped ?? "—"} / ${s.cases_total ?? "—"}</div>`;
    const comp = b.report && b.report.baseline_comparison;
    const flat = (b.report && b.report.metrics_flat) || {};
    let rows = "";
    if (comp && comp.metrics) {
      rows = Object.entries(comp.metrics).map(([k, d]) => {
        const bad = d.delta != null && d.delta < 0;
        return `<tr class="${bad ? "fail-row" : ""}"><td>${esc(k)}</td><td>${esc(d.baseline ?? "—")}</td><td>${esc(d.current ?? "—")}</td><td>${esc(d.delta ?? "—")}</td></tr>`;
      }).join("");
    } else {
      rows = Object.entries(flat).map(([k, v]) => `<tr><td>${esc(k)}</td><td>—</td><td>${esc(v)}</td><td>—</td></tr>`).join("");
    }
    $("#bm-metrics tbody").innerHTML = rows || `<tr><td colspan="4" class="muted">报告尚未生成</td></tr>`;
    const failed = (b.report && b.report.failed_cases) || [];
    $("#bm-failed").innerHTML = failed.length
      ? `<table><thead><tr><th>layer</th><th>case</th><th>dataset</th><th>error</th></tr></thead><tbody>` +
        failed.map((c) => `<tr class="fail-row"><td>${esc(c.layer)}</td><td>${esc(c.case_id)}</td><td>${esc(c.dataset)}</td><td>${esc(c.error || "")}</td></tr>`).join("") +
        `</tbody></table>`
      : `<div class="muted">无失败 case</div>`;
    if (b.status === "running") setTimeout(() => { if (selectedBenchmark === id) showBenchmark(id); }, 3000);
  } catch (e) { alert(e.message); }
}

/* ---------------- boot ---------------- */
renderLayerCheckboxes();
activateTab();
loadHome();
setInterval(loadHome, 30000);
