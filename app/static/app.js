let state = {};
let activeDocument = null;
const $ = (id) => document.getElementById(id);
const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

function toast(message) { const el = $("toast"); el.textContent = message; el.classList.add("show"); setTimeout(() => el.classList.remove("show"), 2600); }
function markdown(text) { return `<pre>${escapeHtml(text)}</pre>`; }
function table(records) {
  if (!records.length) return '<p class="hint">No repository evidence files yet.</p>';
  const rows = records.map((file) => `<tr><td>${escapeHtml(file.filename)}</td><td>${escapeHtml(file.path)}</td><td><code>${escapeHtml(file.sha256)}</code></td><td>${escapeHtml(file.bytes)}</td></tr>`).join("");
  return `<table><thead><tr><th>File</th><th>Repository path</th><th>SHA-256</th><th>Bytes</th></tr></thead><tbody>${rows}</tbody></table>`;
}
async function postForm(url, form) { const response = await fetch(url, { method: "POST", body: new FormData(form) }); if (!response.ok) { const body = await response.json().catch(() => ({})); throw new Error(body.detail || response.statusText); } return response.json(); }

async function loadDocument(key, target) {
  const response = await fetch(`/api/documents/${key}`); if (!response.ok) throw new Error("Could not load repository document");
  const document = await response.json(); if (target) $(target).innerHTML = markdown(document.markdown); return document;
}
async function loadDashboard() {
  const response = await fetch("/api/dashboard"); if (!response.ok) throw new Error("Could not refresh repository"); state = await response.json();
  $("sourceCount").textContent = state.counts.sources; $("claimCount").textContent = state.counts.claims; $("evidenceCount").textContent = state.counts.evidence; $("journalCount").textContent = state.counts.journal_entries;
  $("repoStatus").textContent = `GitHub source of truth: ${state.repo.branch}@${state.repo.head}`; $("operationsView").innerHTML = markdown(state.operations); $("evidenceList").innerHTML = table(state.evidence);
  const select = $("documentSelect"); const current = select.value; select.innerHTML = state.documents.map((doc) => `<option value="${escapeHtml(doc.key)}">${escapeHtml(doc.title)}</option>`).join(""); if (current) select.value = current;
  await Promise.all([loadDocument("hardware", "knowledgeView"), loadDocument("sources", "sourcesView"), loadDocument("journal", "journalView")]);
}
document.querySelectorAll(".tab").forEach((button) => button.addEventListener("click", () => { document.querySelectorAll(".tab").forEach((b) => b.classList.remove("active")); document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active-panel")); button.classList.add("active"); $(button.dataset.tab).classList.add("active-panel"); }));
[["fetchSourceForm", "/api/fetch-source", "Source added to the repository"], ["observationForm", "/api/observation", "Observation appended to the knowledge base"], ["taskForm", "/api/task", "Task appended to the operations briefing"], ["evidenceForm", "/api/upload-evidence", "Evidence hashed and registered"], ["journalForm", "/api/journal", "Journal entry appended"]].forEach(([formId, endpoint, message]) => { $(formId).addEventListener("submit", async (event) => { event.preventDefault(); try { await postForm(endpoint, event.currentTarget); event.currentTarget.reset(); toast(message); await loadDashboard(); } catch (error) { toast(error.message); } }); });
$("refreshBtn").addEventListener("click", () => loadDashboard().catch((error) => toast(error.message)));
$("loadDocumentBtn").addEventListener("click", async () => { try { activeDocument = await loadDocument($("documentSelect").value); $("documentEditor").value = activeDocument.markdown; $("documentPath").textContent = `${activeDocument.path} at ${activeDocument.head.slice(0, 7)}`; } catch (error) { toast(error.message); } });
$("saveDocumentBtn").addEventListener("click", async () => { if (!activeDocument || activeDocument.key !== $("documentSelect").value) return toast("Load the document before saving it"); const form = new FormData(); form.set("markdown", $("documentEditor").value); form.set("expected_head", activeDocument.head); try { const response = await fetch(`/api/documents/${activeDocument.key}`, { method: "POST", body: form }); if (!response.ok) { const body = await response.json().catch(() => ({})); throw new Error(body.detail || response.statusText); } toast("Saved to GitHub main"); await loadDashboard(); activeDocument = await loadDocument($("documentSelect").value); $("documentEditor").value = activeDocument.markdown; } catch (error) { toast(error.message); } });
loadDashboard().catch((error) => toast(error.message));
