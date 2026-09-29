"use strict";
(() => {
  const $ = id => document.getElementById(id);
  let token = "", market = "US_STOCKS", generation = 0, timer = null, busy = false;
  const node = (tag, text, cls) => { const n = document.createElement(tag); n.textContent = text; if (cls) n.className = cls; return n; };
  const words = value => String(value || "Pending review").replaceAll("_", " ").toLowerCase().replace(/^./, c => c.toUpperCase());
  function lock() {
    generation++; token = ""; busy = false; clearTimeout(timer);
    $("token").value = ""; $("workspace").hidden = true; $("logout").hidden = true;
    $("login-panel").hidden = false; $("reports").replaceChildren(); $("items").replaceChildren();
    $("detail-body").replaceChildren(); $("detail").close(); $("connection").textContent = "Private view";
  }
  async function get(path) {
    const requestGeneration = generation;
    const response = await fetch(path, {headers: {Authorization: `Bearer ${token}`}, cache: "no-store", redirect: "error"});
    if (!response.ok) { if (response.status === 401 && requestGeneration === generation) { lock(); $("notice").textContent = "Read token not accepted."; $("notice").hidden = false; } throw new Error(response.status === 401 ? "Read token not accepted." : "Research service unavailable. Try refresh."); }
    return response.json();
  }
  function showDetails(item) {
    const out = $("detail-body"), record = item.record_json, evidence = record.evidence || record;
    out.replaceChildren(node("h2", item.symbol));
    // The server records the submitted research item in state_json/record_json, never HTML.
    const data = evidence.item || evidence.research || evidence;
    for (const [label, value] of [["Decision", words(item.recorded_disposition)], ["Current validity", words(item.status)], ["Reason", words(item.reason)], ["Thesis", data.thesis], ["Disproof", data.disproof]]) {
      if (value) { const p = node("p", "", "detail-copy"); p.append(node("strong", label), document.createTextNode(value)); out.append(p); }
    }
    out.append(node("h3", "Recorded judgments"));
    const list = node("ul", "", "judgment-list");
    for (const [key, answer] of Object.entries(item.answers_json || {})) list.append(node("li", `${words(key)}: ${answer.choice || answer}`));
    out.append(list);
    if (item.paper_execution) {
      const paper = item.paper_execution;
      out.append(node("h3", "Paper test"), node("p", words(paper.state || paper.outcome)), node("p", paper.cohort));
      if (paper.revocation_reason) out.append(node("p", words(paper.revocation_reason)));
    }
    for (const source of data.sources || record.sources || []) {
      const block = node("div", "", "review-source"); block.append(node("strong", source.source_id), node("p", source.excerpt)); out.append(block);
    }
    const receipt = node("p", "", "detail-copy"); receipt.append(node("strong", "Receipt"), node("code", item.receipt_id || "No provider receipt")); out.append(receipt);
    $("detail").showModal();
  }
  async function loadReport(g) {
    const id = $("reports").value;
    $("empty").hidden = !!id; $("report-panel").hidden = !id;
    if (!id) return;
    const result = await get(`/api/v1/research-reports/${encodeURIComponent(id)}`);
    if (g !== generation) return;
    const count = state => result.items.filter(i => i.recorded_disposition === state).length;
    $("selected").textContent = count("SELECTED"); $("rejected").textContent = count("REJECTED");
    $("needs-review").textContent = count("NEEDS_REVIEW"); $("pending").textContent = result.items.filter(i => !i.recorded_disposition).length;
    $("items").replaceChildren();
    for (const item of result.items) {
      const row = node("tr", ""); row.append(node("td", item.symbol));
      const decision = node("td", ""); decision.append(node("span", words(item.recorded_disposition), `state ${(item.recorded_disposition || "").toLowerCase()}`));
      const validity = node("td", words(item.status));
      if (item.paper_execution) validity.append(node("small", ` · Paper: ${words(item.paper_execution.state || item.paper_execution.outcome)}`));
      row.append(decision, validity);
      const cell = node("td", ""), button = node("button", "View evidence"); button.addEventListener("click", () => showDetails(item)); cell.append(button); row.append(cell); $("items").append(row);
    }
    $("report-meta").textContent = `Revision ${result.report.revision} · ${result.report.cohort} · ${result.selection_policy}`;
  }
  async function refresh() {
    if (!token || busy) return;
    busy = true; const g = generation; $("notice").hidden = true;
    try {
      const [index, workers] = await Promise.all([get(`/api/v1/research-reports?market=${market}`), get("/api/v1/review-workers")]);
      if (g !== generation) return;
      const previous = $("reports").value; $("reports").replaceChildren();
      for (const report of index.reports) { const option = node("option", `${new Date(report.received_at).toLocaleString()} · ${report.timeframe} · rev ${report.revision}`); option.value = report.report_id; $("reports").append(option); }
      if (index.reports.some(r => r.report_id === previous)) $("reports").value = previous;
      const states = workers.workers.map(w => w.status);
      $("worker").textContent = states.includes("HALTED") ? "Reviews halted" : states.includes("RUNNING") ? "Review worker running" : "Review worker offline";
      await loadReport(g);
      if (g !== generation) return;
      $("connection").textContent = `Updated ${new Date().toLocaleTimeString()}`;
      $("workspace").hidden = false; $("login-panel").hidden = true; $("logout").hidden = false;
    } catch (error) {
      if (g !== generation) return;
      $("notice").textContent = error.message; $("notice").hidden = false;
      $("connection").textContent = "Connection unavailable";
      // Retained history stays visible, but no old heartbeat is presented as current.
      $("worker").textContent = "Worker status unavailable";
    } finally {
      if (g === generation) { busy = false; clearTimeout(timer); if (token) timer = setTimeout(refresh, 5000); }
    }
  }
  $("login").addEventListener("submit", event => { event.preventDefault(); token = $("token").value.trim(); $("token").value = ""; generation++; refresh(); });
  $("logout").addEventListener("click", lock); $("refresh").addEventListener("click", refresh);
  $("close-detail").addEventListener("click", () => $("detail").close());
  $("reports").addEventListener("change", () => { generation++; busy = false; refresh(); });
  for (const button of document.querySelectorAll("[data-market]")) button.addEventListener("click", () => {
    market = button.dataset.market; generation++; busy = false; $("reports").replaceChildren(); $("items").replaceChildren(); $("report-panel").hidden = true;
    $("market-note").textContent = market === "INDIA" ? "Indian stocks · research only" : market === "CRYPTO" ? "Crypto research · execution pending" : "US research · paper test status shown when enrolled";
    for (const tab of document.querySelectorAll("[data-market]")) { const active = tab === button; tab.classList.toggle("active", active); tab.setAttribute("aria-pressed", String(active)); }
    refresh();
  });
})();
