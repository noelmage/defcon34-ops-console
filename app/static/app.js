let state = {};

const $ = (id) => document.getElementById(id);

function toast(message) {
  const el = $("toast");
  el.textContent = message;
  el.classList.add("show");
  setTimeout(() => el.classList.remove("show"), 2400);
}

async function postForm(url, form) {
  const response = await fetch(url, { method: "POST", body: new FormData(form) });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || response.statusText);
  }
  return response.json();
}

function item(title, body, meta = "") {
  return `<div class="item"><strong>${escapeHtml(title)}</strong><div>${escapeHtml(body || "")}</div><div class="meta">${escapeHtml(meta)}</div></div>`;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

function table(headers, records, fields) {
  if (!records.length) return '<p class="hint">No records yet.</p>';
  const head = headers.map((h) => `<th>${escapeHtml(h)}</th>`).join("");
  const rows = records.map((record) => `<tr>${fields.map((field) => `<td>${escapeHtml(record[field] ?? "")}</td>`).join("")}</tr>`).join("");
  return `<table><thead><tr>${head}</tr></thead><tbody>${rows}</tbody></table>`;
}

async function loadDashboard() {
  const response = await fetch("/api/dashboard");
  state = await response.json();
  $("sourceCount").textContent = state.sources.length;
  $("claimCount").textContent = state.claims.length;
  $("evidenceCount").textContent = state.evidence.length;
  $("puzzleCount").textContent = state.puzzles.length;
  $("repoStatus").textContent = state.repo ? `Repo ${state.repo.branch}@${state.repo.head}` : "";

  $("tasksList").innerHTML = state.tasks.map((t) =>
    item(`${t.priority}: ${t.objective}`, t.next_action, `${t.status} | risk: ${t.risk} | tools: ${t.required_tools}`)
  ).join("") || '<p class="hint">No priority tasks yet.</p>';

  $("claimsList").innerHTML = state.claims.map((c) =>
    item(c.claim, c.practical_significance, `${c.confidence} | ${c.category} | ${c.source_title || "no source"}`)
  ).join("") || '<p class="hint">No claims recorded yet.</p>';

  $("sourcesList").innerHTML = table(["ID", "Title", "URL", "Accessed", "Reliability"], state.sources, ["id", "title", "url", "accessed_at", "reliability"]);
  $("evidenceList").innerHTML = table(["File", "Description", "SHA-256", "Puzzle", "Acquired"], state.evidence, ["filename", "description", "sha256", "related_puzzle", "acquired_at"]);
  $("puzzlesList").innerHTML = table(["Identifier", "Name", "Status", "Confidence", "Candidate Flag", "Updated"], state.puzzles, ["identifier", "name", "status", "confidence", "candidate_flag", "updated_at"]);
  $("journalList").innerHTML = state.journal.map((j) =>
    item(j.objective, `${j.result}\n${j.interpretation}`, `${j.created_at} | ${j.confidence} | next: ${j.next_action}`)
  ).join("") || '<p class="hint">No journal entries yet.</p>';
}

document.querySelectorAll(".tab").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active-panel"));
    button.classList.add("active");
    $(button.dataset.tab).classList.add("active-panel");
  });
});

[
  ["fetchSourceForm", "/api/fetch-source", "Source fetched"],
  ["claimForm", "/api/claim", "Claim recorded"],
  ["evidenceForm", "/api/upload-evidence", "Evidence uploaded and hashed"],
  ["puzzleForm", "/api/puzzle", "Puzzle added"],
  ["taskForm", "/api/task", "Task added"],
  ["journalForm", "/api/journal", "Journal entry added"],
].forEach(([formId, endpoint, message]) => {
  $(formId).addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      await postForm(endpoint, event.currentTarget);
      event.currentTarget.reset();
      toast(message);
      await loadDashboard();
    } catch (error) {
      toast(error.message);
    }
  });
});

$("refreshBtn").addEventListener("click", loadDashboard);
$("briefingBtn").addEventListener("click", async () => {
  const response = await fetch("/api/briefing");
  const data = await response.json();
  $("briefingOutput").textContent = data.markdown;
});

loadDashboard();
