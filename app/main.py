import hashlib
import os
import re
import secrets
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from bs4 import BeautifulSoup
from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles

APP_DIR = Path(__file__).resolve().parent
ROOT_DIR = APP_DIR.parent
STATIC_DIR = APP_DIR / "static"

OPS_USER = os.getenv("DEFCON_OPS_USER", "admin")
OPS_PASSWORD = os.getenv("DEFCON_OPS_PASSWORD", "change-me")
REPO_URL = os.getenv("OPS_REPO_URL", "https://github.com/noelmage/DefCon34Badge.git")
REPO_BRANCH = os.getenv("OPS_REPO_BRANCH", "main")
REPO_PATH = Path(os.getenv("OPS_REPO_PATH", ROOT_DIR / "data" / "DefCon34Badge"))
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")

# These are the agent-owned Markdown artifacts. The console has no second ledger.
DOCUMENTS = {
    "operations": ("Operations briefing", "docs/operations/dashboard.md"),
    "sources": ("Source register", "docs/research/sources.md"),
    "hardware": ("Hardware knowledge base", "docs/knowledge-base/hardware.md"),
    "software": ("Software knowledge base", "docs/knowledge-base/software.md"),
    "reverse": ("Reverse-engineering knowledge base", "docs/knowledge-base/reverse-engineering.md"),
    "assessment": ("Initial WIRED assessment", "docs/research/2026-08-01-wired-initial-assessment.md"),
    "evidence": ("Evidence inventory", "docs/operations/evidence-inventory.md"),
    "journal": ("Research journal", "docs/journal/research-journal.md"),
    "decisions": ("Decision log", "docs/journal/decision-log.md"),
}

app = FastAPI(title="DEF CON 34 Badge Ops Console")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
security = HTTPBasic()
repo_lock = threading.Lock()


def require_auth(credentials: HTTPBasicCredentials = Depends(security)) -> None:
    if not (
        secrets.compare_digest(credentials.username, OPS_USER)
        and secrets.compare_digest(credentials.password, OPS_PASSWORD)
    ):
        raise HTTPException(401, "Authentication required", {"WWW-Authenticate": "Basic"})


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:80] or "item"


def markdown_cell(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).replace("|", "\\|") or "-"


def redact(value: str) -> str:
    return value.replace(GITHUB_TOKEN, "[REDACTED]") if GITHUB_TOKEN else value


def git_prefix() -> list[str]:
    if not GITHUB_TOKEN:
        return ["git"]
    token_url = f"https://x-access-token:{GITHUB_TOKEN}@github.com/"
    return ["git", "-c", f"url.{token_url}.insteadOf=https://github.com/"]


def run_git(args: list[str], cwd: Path | None = None) -> str:
    result = subprocess.run(
        git_prefix() + args,
        cwd=cwd,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise HTTPException(500, f"git {' '.join(args)} failed: {redact((result.stderr or result.stdout).strip())}")
    return result.stdout.strip()


def ensure_repo() -> None:
    REPO_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not (REPO_PATH / ".git").exists():
        if REPO_PATH.exists() and any(REPO_PATH.iterdir()):
            raise HTTPException(500, f"{REPO_PATH} exists but is not a Git checkout")
        run_git(["clone", "--branch", REPO_BRANCH, REPO_URL, str(REPO_PATH)], REPO_PATH.parent)
    run_git(["config", "user.name", "DEF CON 34 Ops Console"], REPO_PATH)
    run_git(["config", "user.email", "ops-console@henry.house"], REPO_PATH)
    run_git(["remote", "set-url", "origin", REPO_URL], REPO_PATH)


def sync_repo() -> None:
    ensure_repo()
    if run_git(["status", "--porcelain"], REPO_PATH):
        raise HTTPException(409, "The working clone has uncommitted changes; refresh it manually before continuing")
    run_git(["fetch", "origin", "--prune"], REPO_PATH)
    run_git(["checkout", REPO_BRANCH], REPO_PATH)
    run_git(["pull", "--ff-only", "origin", REPO_BRANCH], REPO_PATH)


def commit_and_push(message: str, paths: list[Path]) -> None:
    rel_paths = [str(path.relative_to(REPO_PATH)).replace("\\", "/") for path in paths]
    run_git(["add", *rel_paths], REPO_PATH)
    if not run_git(["status", "--porcelain"], REPO_PATH):
        return
    run_git(["commit", "-m", message], REPO_PATH)
    run_git(["pull", "--rebase", "origin", REPO_BRANCH], REPO_PATH)
    run_git(["push", "origin", REPO_BRANCH], REPO_PATH)


def document_path(key: str) -> Path:
    if key not in DOCUMENTS:
        raise HTTPException(404, "Unknown project document")
    return REPO_PATH / DOCUMENTS[key][1]


def read_document(key: str) -> str:
    path = document_path(key)
    if not path.exists():
        raise HTTPException(404, f"Missing repository document: {path.relative_to(REPO_PATH)}")
    return path.read_text(encoding="utf-8")


def append_markdown(path: Path, content: str) -> None:
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(old.rstrip() + "\n\n" + content.strip() + "\n", encoding="utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def count_matches(text: str, pattern: str) -> int:
    return len(re.findall(pattern, text, flags=re.MULTILINE))


def evidence_files() -> list[dict[str, str]]:
    files: list[dict[str, str]] = []
    for folder in ("evidence/sources", "evidence/derived", "evidence/uploads"):
        base = REPO_PATH / folder
        if not base.exists():
            continue
        for path in sorted((item for item in base.rglob("*") if item.is_file()), reverse=True):
            files.append({
                "path": str(path.relative_to(REPO_PATH)).replace("\\", "/"),
                "filename": path.name,
                "sha256": sha256_file(path),
                "bytes": str(path.stat().st_size),
            })
    return files


@app.get("/")
def index(_: None = Depends(require_auth)) -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/dashboard")
def dashboard(_: None = Depends(require_auth)) -> dict[str, Any]:
    with repo_lock:
        sync_repo()
        docs = [{"key": key, "title": title, "path": path} for key, (title, path) in DOCUMENTS.items()]
        operations = read_document("operations")
        sources = read_document("sources")
        assessment = read_document("assessment")
        journal = read_document("journal")
        evidence = evidence_files()
        head = run_git(["rev-parse", "--short", "HEAD"], REPO_PATH)
    return {
        "repo": {"branch": REPO_BRANCH, "head": head},
        "documents": docs,
        "counts": {
            "sources": count_matches(sources, r"^\|\s*SRC-"),
            "claims": count_matches(assessment, r"^\|\s*C-\d+"),
            "evidence": len(evidence),
            "journal_entries": count_matches(journal, r"^##\s+20\d\d-\d\d-\d\d"),
        },
        "operations": operations,
        "evidence": evidence,
    }


@app.get("/api/documents/{key}")
def get_document(key: str, _: None = Depends(require_auth)) -> dict[str, str]:
    with repo_lock:
        sync_repo()
        return {"key": key, "title": DOCUMENTS[key][0], "path": DOCUMENTS[key][1], "markdown": read_document(key), "head": run_git(["rev-parse", "HEAD"], REPO_PATH)}


@app.post("/api/documents/{key}")
def save_document(key: str, markdown: str = Form(...), expected_head: str = Form(...), _: None = Depends(require_auth)) -> dict[str, str]:
    with repo_lock:
        sync_repo()
        current_head = run_git(["rev-parse", "HEAD"], REPO_PATH)
        if not secrets.compare_digest(expected_head, current_head):
            raise HTTPException(409, "This document changed in GitHub. Refresh it before saving your edits.")
        path = document_path(key)
        path.write_text(markdown.rstrip() + "\n", encoding="utf-8")
        commit_and_push(f"Update {DOCUMENTS[key][0].lower()}", [path])
        return {"path": DOCUMENTS[key][1], "head": run_git(["rev-parse", "HEAD"], REPO_PATH)}


@app.post("/api/fetch-source")
async def fetch_source(url: str = Form(...), _: None = Depends(require_auth)) -> dict[str, str]:
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
            response = await client.get(url, headers={"User-Agent": "DEFCON34BadgeOps/0.3"})
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Fetch failed: {exc}") from exc
    soup = BeautifulSoup(response.text, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    title = (soup.title.string.strip() if soup.title and soup.title.string else url)[:240]
    body = "\n".join(line.strip() for line in soup.get_text("\n").splitlines() if line.strip())[:250000]
    with repo_lock:
        sync_repo()
        target = REPO_PATH / "evidence" / "derived" / f"{today()}_web_{slugify(title)}_text.txt"
        target.write_text(f"URL: {url}\nFetched: {now_iso()}\nTitle: {title}\n\n{body}\n", encoding="utf-8")
        source_doc = document_path("sources")
        source_id = f"SRC-{count_matches(read_document('sources'), r'^\\|\\s*SRC-') + 1:03d}"
        append_markdown(source_doc, f"| {source_id} | {markdown_cell(title)} | Web page | Unrecorded | {url} | Unknown | {today()} | Unknown | Yes | Unrated | Fetched; review pending |")
        commit_and_push(f"Acquire source: {title[:60]}", [target, source_doc])
    return {"source_id": source_id, "path": str(target.relative_to(REPO_PATH)).replace("\\", "/")}


@app.post("/api/observation")
def add_observation(
    knowledge_base: str = Form(...),
    claim: str = Form(...),
    claim_state: str = Form("Unverified"),
    confidence: str = Form("Low"),
    evidence: str = Form(""),
    next_step: str = Form(""),
    _: None = Depends(require_auth),
) -> dict[str, str]:
    if knowledge_base not in {"hardware", "software", "reverse"}:
        raise HTTPException(400, "Choose a knowledge-base document")
    entry = "\n".join([
        f"## Observation - {today()}",
        "",
        f"- **Claim:** {claim.strip()}",
        f"- **Claim state:** {claim_state}",
        f"- **Confidence:** {confidence}",
        f"- **Evidence:** {evidence.strip() or 'Not yet recorded'}",
        f"- **Next step:** {next_step.strip() or 'Review and corroborate'}",
    ])
    with repo_lock:
        sync_repo()
        path = document_path(knowledge_base)
        append_markdown(path, entry)
        commit_and_push(f"Add {knowledge_base} observation", [path])
    return {"path": DOCUMENTS[knowledge_base][1]}


@app.post("/api/task")
def add_task(
    priority: str = Form(...), objective: str = Form(...), reason: str = Form(""), tools: str = Form(""),
    risk: str = Form("Low"), next_action: str = Form(""), stop_condition: str = Form(""),
    _: None = Depends(require_auth),
) -> dict[str, str]:
    row = "| " + " | ".join(markdown_cell(value) for value in [priority, objective, reason, tools, "Unestimated", risk, next_action, stop_condition]) + " |"
    with repo_lock:
        sync_repo()
        path = document_path("operations")
        content = read_document("operations")
        marker = "## Puzzle and Flag Status"
        if marker not in content:
            raise HTTPException(500, "The operations briefing no longer has its expected Priority Queue structure")
        path.write_text(content.replace(marker, row + "\n\n" + marker, 1), encoding="utf-8")
        commit_and_push(f"Add {priority} task: {objective[:50]}", [path])
    return {"path": DOCUMENTS["operations"][1]}


@app.post("/api/journal")
def add_journal(objective: str = Form(...), result: str = Form(""), interpretation: str = Form(""), next_action: str = Form(""), _: None = Depends(require_auth)) -> dict[str, str]:
    entry = "\n".join([
        f"## {today()} - {objective.strip()}", "", f"- **Result:** {result.strip() or 'Pending'}",
        f"- **Interpretation:** {interpretation.strip() or 'Not yet assessed'}", f"- **Next action:** {next_action.strip() or 'Review'}",
    ])
    with repo_lock:
        sync_repo()
        path = document_path("journal")
        append_markdown(path, entry)
        commit_and_push(f"Journal: {objective[:55]}", [path])
    return {"path": DOCUMENTS["journal"][1]}


@app.post("/api/upload-evidence")
async def upload_evidence(file: UploadFile = File(...), description: str = Form(""), source: str = Form(""), related_item: str = Form(""), _: None = Depends(require_auth)) -> dict[str, str]:
    raw = await file.read()
    digest = hashlib.sha256(raw).hexdigest()
    original_name = Path(file.filename or "evidence.bin")
    safe_name = f"{today()}_{slugify(original_name.stem)}_{digest[:10]}{original_name.suffix.lower()}"
    with repo_lock:
        sync_repo()
        target = REPO_PATH / "evidence" / "uploads" / safe_name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        inventory = document_path("evidence")
        evidence_id = f"EV-{len(evidence_files()) + 1:03d}"
        append_markdown(inventory, "| " + " | ".join(markdown_cell(value) for value in [evidence_id, safe_name, f"{description} {source}".strip(), now_iso(), "Dashboard upload", related_item, "Unknown", digest, "Original", f"evidence/uploads/{safe_name}"]) + " |")
        commit_and_push(f"Add evidence: {safe_name}", [target, inventory])
    return {"path": str(target.relative_to(REPO_PATH)).replace("\\", "/"), "sha256": digest}
