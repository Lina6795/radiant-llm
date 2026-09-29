/* RADIANT-Control S11-E: governed-chain chat frontend.
 * Vanilla JS, no build step. All server-supplied text is rendered through
 * textContent / createTextNode only; styling is done via classList so no
 * untrusted string ever becomes markup. Talks exclusively to the governed
 * /runs control plane (plus /evidence lookup); there is no fallback to any
 * other query path.
 */
(function () {
  "use strict";

  var STEP_DEFS = [
    { id: "s1-search", label: "search" },
    { id: "s2-inspect", label: "inspect" },
    { id: "s3-context", label: "context" },
    { id: "s4-draft", label: "draft" },
    { id: "s5-verify", label: "verify" }
  ];
  var TERMINAL_STATES = { succeeded: true, failed: true, cancelled: true };

  var els = {
    runPill: document.getElementById("run-pill"),
    statusBar: document.getElementById("status-bar"),
    chatArea: document.getElementById("chat-area"),
    routerPanel: document.getElementById("router-panel"),
    routerBody: document.getElementById("router-body"),
    chainPanel: document.getElementById("chain-panel"),
    chainRunId: document.getElementById("chain-run-id"),
    chainSteps: document.getElementById("chain-steps"),
    verifyPanel: document.getElementById("verify-panel"),
    verifyAction: document.getElementById("verify-action"),
    claimCounts: document.getElementById("claim-counts"),
    claimList: document.getElementById("claim-list"),
    controls: document.getElementById("controls-panel"),
    btnCancel: document.getElementById("btn-cancel"),
    btnResume: document.getElementById("btn-resume"),
    btnRefresh: document.getElementById("btn-refresh"),
    gotoReview: document.getElementById("goto-review"),
    timelinePanel: document.getElementById("timeline-panel"),
    timeline: document.getElementById("timeline"),
    askForm: document.getElementById("ask-form"),
    goalInput: document.getElementById("goal-input"),
    btnSend: document.getElementById("btn-send"),
    historyList: document.getElementById("history-list"),
    historyRefresh: document.getElementById("history-refresh"),
    modal: document.getElementById("evidence-modal"),
    evidenceTitle: document.getElementById("evidence-title"),
    evidenceBody: document.getElementById("evidence-body"),
    evidenceClose: document.getElementById("evidence-close")
  };

  var currentRunId = null;
  var currentState = null;
  var eventSource = null;
  var stepEls = {};

  // ------------------------------------------------------------------
  // helpers
  // ------------------------------------------------------------------

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function clearChildren(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function showStatus(message, isError) {
    els.statusBar.textContent = message;
    els.statusBar.classList.remove("hidden");
    els.statusBar.classList.toggle("info", !isError);
  }

  function hideStatus() {
    els.statusBar.classList.add("hidden");
    els.statusBar.textContent = "";
  }

  function shortId(runId) {
    if (!runId) return "";
    return runId.length > 12 ? runId.slice(0, 8) + "…" : runId;
  }

  function truncate(text, max) {
    text = String(text == null ? "" : text);
    return text.length > max ? text.slice(0, max) + "…" : text;
  }

  function fmtTime(ts) {
    var d = typeof ts === "number" ? new Date(ts * 1000) : new Date(ts || Date.now());
    if (isNaN(d.getTime())) d = new Date();
    return d.toLocaleTimeString();
  }

  function setRunPill(state) {
    els.runPill.textContent = state || "空闲";
    els.runPill.className = "pill pill-" + (state || "idle");
  }

  function isBusy() {
    return currentRunId !== null && currentState !== null && !TERMINAL_STATES[currentState];
  }

  function updateFormState() {
    var busy = isBusy();
    els.goalInput.disabled = busy;
    els.btnSend.disabled = busy;
    if (busy) {
      showStatus("run 进行中（" + currentRunId + "），完成前无法发送新提问。", false);
    } else if (!els.statusBar.classList.contains("hidden") &&
               els.statusBar.classList.contains("info")) {
      hideStatus();
    }
  }

  // fetch wrapper: every call handles network errors, non-2xx and JSON
  // parse failures, surfacing them in the status bar.
  function api(path, options) {
    options = options || {};
    if (options.body !== undefined && typeof options.body !== "string") {
      options.body = JSON.stringify(options.body);
      options.headers = { "Content-Type": "application/json" };
    }
    return fetch(path, options).then(function (resp) {
      return resp.text().then(function (raw) {
        var data = null;
        if (raw) {
          try {
            data = JSON.parse(raw);
          } catch (parseErr) {
            throw new Error("响应不是合法 JSON（HTTP " + resp.status + "）");
          }
        }
        if (!resp.ok) {
          var detail = data && data.detail ? data.detail : raw;
          if (typeof detail !== "string") {
            try { detail = JSON.stringify(detail); } catch (e) { detail = "未知错误"; }
          }
          var err = new Error("HTTP " + resp.status + ": " + truncate(detail, 200));
          err.status = resp.status;
          throw err;
        }
        return data;
      });
    }).catch(function (err) {
      if (err instanceof TypeError) {
        throw new Error("网络错误：" + err.message);
      }
      throw err;
    });
  }

  function reportError(prefix, err) {
    showStatus(prefix + "：" + (err && err.message ? err.message : String(err)), true);
  }

  // ------------------------------------------------------------------
  // chat bubbles
  // ------------------------------------------------------------------

  function addBubble(role, text, meta) {
    var bubble = el("div", "bubble bubble-" + role);
    bubble.appendChild(document.createTextNode(text));
    if (meta) bubble.appendChild(el("span", "b-meta", meta));
    els.chatArea.appendChild(bubble);
    bubble.scrollIntoView({ block: "nearest" });
    return bubble;
  }

  // ------------------------------------------------------------------
  // governed chain panel
  // ------------------------------------------------------------------

  function buildChainPanel(runId) {
    els.chainPanel.classList.remove("hidden");
    els.chainRunId.textContent = "run: " + runId;
    clearChildren(els.chainSteps);
    stepEls = {};
    STEP_DEFS.forEach(function (def) {
      var li = el("li", "step st-pending");
      li.appendChild(el("span", "s-name", def.label));
      li.appendChild(el("span", "s-state", "pending"));
      var extra = el("span", "s-extra", "");
      li.appendChild(extra);
      els.chainSteps.appendChild(li);
      stepEls[def.id] = { root: li, state: li.children[1], extra: extra };
    });
  }

  var STATE_CLASS = {
    pending: "st-pending",
    running: "st-running",
    succeeded: "st-success",
    success: "st-success",
    skipped: "st-success",
    retry: "st-retry",
    failed: "st-failed",
    waiting_review: "st-waiting_review"
  };

  function setStepState(stepId, state, extraText) {
    var entry = stepEls[stepId];
    if (!entry) return;
    var cls = STATE_CLASS[state] || "st-pending";
    entry.root.className = "step " + cls;
    entry.state.textContent = state;
    entry.extra.textContent = extraText || "";
  }

  function applySnapshotSteps(steps) {
    (steps || []).forEach(function (step) {
      var extra = "";
      if (step.attempt > 1) extra = "attempt " + step.attempt;
      if (step.error) extra = (extra ? extra + " · " : "") + truncate(step.error, 60);
      setStepState(step.step_id, step.state || "pending", extra);
    });
  }

  // ------------------------------------------------------------------
  // verify / claims panel
  // ------------------------------------------------------------------

  function renderVerifyOutput(output) {
    els.verifyPanel.classList.remove("hidden");
    var action = output.verify_action || "unknown";
    els.verifyAction.textContent = action;
    els.verifyAction.className = "pill pill-" +
      (action === "accept" ? "succeeded" : action === "abstain" ? "failed" : "waiting_review");
    if (output.revise_used) {
      els.verifyAction.textContent = action + "（已修订）";
    }

    var claims = Array.isArray(output.claims) ? output.claims : [];
    var counts = { supported: 0, unsupported: 0, other: 0 };
    claims.forEach(function (c) {
      if (c.verdict === "supported") counts.supported += 1;
      else if (c.verdict === "unsupported") counts.unsupported += 1;
      else counts.other += 1;
    });
    clearChildren(els.claimCounts);
    els.claimCounts.appendChild(el("span", "cc", "supported: " + counts.supported));
    els.claimCounts.appendChild(el("span", "cc", "unsupported: " + counts.unsupported));
    els.claimCounts.appendChild(el("span", "cc", "其他: " + counts.other));

    clearChildren(els.claimList);
    claims.forEach(function (claim) {
      var verdict = claim.verdict || "other";
      var cls = verdict === "supported" ? "v-supported"
        : verdict === "unsupported" ? "v-unsupported"
        : verdict === "conflicted" ? "v-conflicted" : "v-other";
      var li = el("li", "claim " + cls);
      li.appendChild(el("span", "c-verdict", verdict));
      li.appendChild(el("span", "c-text", claim.text || claim.claim || ""));
      var ids = Array.isArray(claim.evidence_ids) ? claim.evidence_ids : [];
      if (ids.length) {
        var chips = el("div", "ev-chips");
        ids.forEach(function (eid) {
          var chip = el("button", "ev-chip", eid);
          chip.type = "button";
          chip.addEventListener("click", function () { openEvidence(eid); });
          chips.appendChild(chip);
        });
        li.appendChild(chips);
      }
      els.claimList.appendChild(li);
    });
  }

  // ------------------------------------------------------------------
  // evidence modal
  // ------------------------------------------------------------------

  function closeModal() {
    els.modal.classList.add("hidden");
  }

  function kvRow(key, value) {
    var row = el("div", "kv");
    row.appendChild(el("span", "k", key));
    row.appendChild(el("span", "v", value == null ? "—" : String(value)));
    return row;
  }

  function openEvidence(evidenceId) {
    els.evidenceTitle.textContent = evidenceId;
    clearChildren(els.evidenceBody);
    els.evidenceBody.appendChild(el("p", null, "加载中…"));
    els.modal.classList.remove("hidden");
    api("/evidence/" + encodeURIComponent(evidenceId)).then(function (data) {
      clearChildren(els.evidenceBody);
      els.evidenceBody.appendChild(kvRow("evidence_id", data.evidence_id));
      els.evidenceBody.appendChild(kvRow("document_id", data.document_id));
      els.evidenceBody.appendChild(kvRow("page", data.page));
      els.evidenceBody.appendChild(kvRow("modality", data.modality));
      els.evidenceBody.appendChild(kvRow("workspace", data.workspace_id));
      var content = data.content;
      if (content == null && data.payload && typeof data.payload === "object") {
        content = data.payload.content || data.payload.text;
      }
      if (content == null) {
        try { content = JSON.stringify(data); } catch (e) { content = ""; }
      }
      els.evidenceBody.appendChild(el("div", "ev-content", truncate(content, 500)));
    }).catch(function (err) {
      clearChildren(els.evidenceBody);
      els.evidenceBody.appendChild(el("p", null, "加载失败：" + err.message));
    });
  }

  // ------------------------------------------------------------------
  // timeline
  // ------------------------------------------------------------------

  function summarizeEvent(type, data) {
    var step = data.step_id ? data.step_id + " " : "";
    switch (type) {
      case "run_started": return "goal: " + truncate(data.goal || "", 80);
      case "node_started": return step + "tool=" + (data.tool || "?") + " attempt=" + (data.attempt || 1);
      case "node_completed": return step + "latency=" + (data.latency_ms != null ? data.latency_ms + "ms" : "?");
      case "node_retried": return step + "attempt=" + (data.attempt || "?");
      case "node_failed": return step + truncate(data.error || "", 80);
      case "node_skipped": return step + (data.reason || "");
      case "waiting_review": return step + "tool=" + (data.tool || "?") + " risk=" + (data.risk || "?");
      case "run_completed": return "tools_executed=" + (data.tools_executed != null ? data.tools_executed : "?");
      case "run_failed": return step + truncate(data.error || data.status || "", 80);
      case "run_cancelled": return "cancelled_steps=" + (data.cancelled_steps != null ? data.cancelled_steps : "?");
      case "run_resumed": return "restored_steps=" + (data.restored_steps != null ? data.restored_steps : "?");
      case "binding_resolved": return step + truncate(data.binding || data.name || "", 60);
      default:
        try { return truncate(JSON.stringify(data), 100); } catch (e) { return ""; }
    }
  }

  function appendTimeline(type, data) {
    els.timelinePanel.classList.remove("hidden");
    var li = document.createElement("li");
    li.appendChild(el("span", "t-time", fmtTime(data && data.created_at)));
    li.appendChild(el("span", "t-type", type));
    li.appendChild(el("span", "t-sum", data ? summarizeEvent(type, data) : ""));
    els.timeline.appendChild(li);
    li.scrollIntoView({ block: "nearest" });
  }

  // ------------------------------------------------------------------
  // SSE wiring
  // ------------------------------------------------------------------

  function closeEvents() {
    if (eventSource) {
      eventSource.close();
      eventSource = null;
    }
  }

  function connectEvents(runId) {
    closeEvents();
    var es = new EventSource("/runs/" + encodeURIComponent(runId) + "/events");
    eventSource = es;

    var nodeState = {
      node_started: ["running", function (d) { return "attempt " + (d.attempt || 1); }],
      node_completed: ["success", function (d) {
        return d.latency_ms != null ? d.latency_ms + "ms" : "";
      }],
      node_retried: ["retry", function (d) { return "attempt " + (d.attempt || "?"); }],
      node_failed: ["failed", function (d) { return truncate(d.error || "", 60); }],
      node_skipped: ["skipped", function (d) { return d.reason || ""; }],
      waiting_review: ["waiting_review", function (d) { return d.tool || ""; }]
    };
    Object.keys(nodeState).forEach(function (type) {
      es.addEventListener(type, function (ev) {
        var data = parseEventData(ev);
        if (data && data.step_id) {
          setStepState(data.step_id, nodeState[type][0], nodeState[type][1](data));
        }
        appendTimeline(type, data);
        if (type === "waiting_review") onRunState("waiting_review");
      });
    });

    ["run_started", "run_completed", "run_failed", "run_cancelled", "run_resumed",
     "binding_resolved", "lease_acquired", "lease_taken_over"].forEach(function (type) {
      es.addEventListener(type, function (ev) {
        var data = parseEventData(ev);
        appendTimeline(type, data);
        if (type === "run_started" || type === "run_resumed") onRunState("running");
        if (type === "run_completed") onRunState("succeeded");
        if (type === "run_failed") onRunState("failed");
        if (type === "run_cancelled") onRunState("cancelled");
      });
    });

    es.addEventListener("end", function () {
      closeEvents();
      if (currentRunId === runId) refreshSnapshot();
    });

    // Network drops: the browser reconnects automatically with
    // Last-Event-ID; surface the interruption without tearing down.
    es.onerror = function () {
      if (eventSource === es && es.readyState === EventSource.CONNECTING) {
        showStatus("事件流中断，正在自动重连…", false);
      }
    };
    es.onopen = function () {
      if (!isBusy()) hideStatus();
    };
  }

  function parseEventData(ev) {
    try {
      return JSON.parse(ev.data);
    } catch (e) {
      return null;
    }
  }

  function onRunState(state) {
    currentState = state;
    setRunPill(state);
    els.controls.classList.remove("hidden");
    els.gotoReview.classList.toggle("hidden", state !== "waiting_review");
    if (TERMINAL_STATES[state]) {
      closeEvents();
      refreshSnapshot();
    }
    updateFormState();
  }

  // ------------------------------------------------------------------
  // run lifecycle
  // ------------------------------------------------------------------

  function refreshSnapshot() {
    if (!currentRunId) return;
    api("/runs/" + encodeURIComponent(currentRunId)).then(function (snap) {
      renderSnapshot(snap);
    }).catch(function (err) {
      reportError("获取 run 快照失败", err);
    });
  }

  var renderedOutcomeFor = null; // run_id whose terminal bubble was rendered

  function renderSnapshot(snap) {
    if (!snap) return;
    if (snap.run_id && currentRunId && snap.run_id !== currentRunId) return;
    currentState = snap.state || currentState;
    setRunPill(currentState);
    els.controls.classList.remove("hidden");
    els.gotoReview.classList.toggle("hidden", currentState !== "waiting_review");
    applySnapshotSteps(snap.steps);
    updateFormState();

    var verifyStep = null;
    (snap.steps || []).forEach(function (s) {
      if (s.step_id === "s5-verify") verifyStep = s;
    });
    if (verifyStep && verifyStep.output && typeof verifyStep.output === "object") {
      var output = verifyStep.output;
      renderVerifyOutput(output);
      if (output.answer && renderedOutcomeFor !== currentRunId) {
        renderedOutcomeFor = currentRunId;
        addBubble("agent", output.answer,
          "run " + shortId(currentRunId) + " · " + (output.verify_action || ""));
      }
    }
    if (currentState === "failed" && renderedOutcomeFor !== currentRunId) {
      var failedStep = (snap.steps || []).filter(function (s) { return s.state === "failed"; })[0];
      if (failedStep) {
        renderedOutcomeFor = currentRunId;
        addBubble("agent", "运行失败：" + (failedStep.error || failedStep.step_id),
          "run " + shortId(currentRunId));
      }
    }
  }

  function resetRunPanels() {
    clearChildren(els.timeline);
    els.timelinePanel.classList.add("hidden");
    els.verifyPanel.classList.add("hidden");
    clearChildren(els.claimList);
    clearChildren(els.claimCounts);
    els.routerPanel.classList.add("hidden");
    els.controls.classList.add("hidden");
    els.gotoReview.classList.add("hidden");
  }

  function loadRun(runId, fromHistory) {
    currentRunId = runId;
    currentState = "loading";
    renderedOutcomeFor = null;
    setRunPill("loading");
    resetRunPanels();
    buildChainPanel(runId);
    updateFormState();
    if (fromHistory) {
      addBubble("agent", "已加载历史 run " + runId + "，正在重放事件流…");
    }
    api("/runs/" + encodeURIComponent(runId)).then(function (snap) {
      renderSnapshot(snap);
      // Replay the event log from seq=0; for terminal runs the stream
      // replays everything then emits `end`, which closes the source.
      connectEvents(runId);
    }).catch(function (err) {
      reportError("加载 run 失败", err);
      currentRunId = null;
      currentState = null;
      setRunPill(null);
      updateFormState();
    });
  }

  function showRouterRejection(resp) {
    els.routerPanel.classList.remove("hidden");
    clearChildren(els.routerBody);
    var p = el("p", null);
    p.appendChild(document.createTextNode("路由器决策："));
    p.appendChild(el("span", "router-action", resp.status || "unknown"));
    els.routerBody.appendChild(p);
    var codes = Array.isArray(resp.reason_codes) ? resp.reason_codes : [];
    if (codes.length) {
      var ul = el("ul", "router-codes");
      codes.forEach(function (code) {
        ul.appendChild(el("li", null, code));
      });
      els.routerBody.appendChild(ul);
    }
    if (resp.detail) els.routerBody.appendChild(el("p", null, resp.detail));
    els.routerBody.appendChild(el("p", "router-note",
      "该请求未进入受控工具链，没有创建 run，也不会产生答案。"));
  }

  function sendGoal(goal) {
    currentState = "loading";
    setRunPill("loading");
    els.goalInput.disabled = true;
    els.btnSend.disabled = true;
    showStatus("正在提交到受控链…", false);

    api("/runs", { method: "POST", body: { goal: goal, workspace: "default" } })
      .then(function (resp) {
        if (!resp || !resp.run_id) {
          // Router declined: no run was created. Show the decision and
          // reason codes; never fall back to any other query path.
          currentState = null;
          setRunPill(null);
          showRouterRejection(resp || { status: "unknown", reason_codes: [] });
          addBubble("agent", "该请求未进入受控工具链（" +
            (resp && resp.status ? resp.status : "unknown") + "），详见下方路由结果。");
          hideStatus();
          updateFormState();
          return;
        }
        currentRunId = resp.run_id;
        renderedOutcomeFor = null;
        resetRunPanels();
        buildChainPanel(resp.run_id);
        if (Array.isArray(resp.steps)) {
          resp.steps.forEach(function (s) {
            if (s.step_id) setStepState(s.step_id, "pending", "");
          });
        }
        onRunState("running");
        connectEvents(resp.run_id);
        loadHistory();
      })
      .catch(function (err) {
        reportError("提交失败", err);
        currentRunId = null;
        currentState = null;
        setRunPill(null);
        updateFormState();
      });
  }

  // ------------------------------------------------------------------
  // controls
  // ------------------------------------------------------------------

  function cancelRun() {
    if (!currentRunId) return;
    api("/runs/" + encodeURIComponent(currentRunId) + "/cancel", { method: "POST" })
      .then(function () {
        showStatus("已请求取消 run " + currentRunId, false);
        refreshSnapshot();
      })
      .catch(function (err) { reportError("取消失败", err); });
  }

  function resumeRun() {
    if (!currentRunId) return;
    api("/runs/" + encodeURIComponent(currentRunId) + "/resume", { method: "POST" })
      .then(function () {
        onRunState("running");
        connectEvents(currentRunId);
      })
      .catch(function (err) {
        if (err.status === 409) {
          reportError("Resume 被拒绝（run 已处于终态）", err);
          refreshSnapshot();
        } else {
          reportError("Resume 失败", err);
        }
      });
  }

  // ------------------------------------------------------------------
  // history sidebar
  // ------------------------------------------------------------------

  function loadHistory() {
    api("/runs?limit=20").then(function (data) {
      clearChildren(els.historyList);
      var items = data && Array.isArray(data.items) ? data.items : [];
      if (!items.length) {
        els.historyList.appendChild(el("li", "muted", "暂无历史 run"));
        return;
      }
      items.forEach(function (item) {
        var li = el("li", "history-item");
        li.appendChild(el("span", "h-goal", truncate(item.goal || "(无 goal)", 60)));
        var meta = el("span", "h-meta",
          shortId(item.run_id) + " · " + (item.state || "?") + " · " +
          truncate(item.updated_at || "", 19).replace("T", " "));
        li.appendChild(meta);
        li.addEventListener("click", function () {
          if (item.run_id) loadRun(item.run_id, true);
        });
        els.historyList.appendChild(li);
      });
    }).catch(function (err) {
      reportError("加载历史失败", err);
    });
  }

  // ------------------------------------------------------------------
  // event wiring
  // ------------------------------------------------------------------

  els.askForm.addEventListener("submit", function (ev) {
    ev.preventDefault();
    if (isBusy()) {
      showStatus("run 进行中（" + currentRunId + "），请等待完成或先取消。", true);
      return;
    }
    var goal = els.goalInput.value.trim();
    if (!goal) return;
    els.goalInput.value = "";
    addBubble("user", goal);
    sendGoal(goal);
  });

  els.btnCancel.addEventListener("click", cancelRun);
  els.btnResume.addEventListener("click", resumeRun);
  els.btnRefresh.addEventListener("click", refreshSnapshot);
  els.historyRefresh.addEventListener("click", loadHistory);
  els.evidenceClose.addEventListener("click", closeModal);
  els.modal.addEventListener("click", function (ev) {
    if (ev.target === els.modal) closeModal();
  });
  document.addEventListener("keydown", function (ev) {
    if (ev.key === "Escape") closeModal();
  });

  setRunPill(null);
  loadHistory();
})();
