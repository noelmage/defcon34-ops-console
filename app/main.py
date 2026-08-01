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
from authlib.integrations.starlette_client import OAuth, OAuthError
from bs4 import BeautifulSoup
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

APP_DIR = Path(__file__).resolve().parent
ROOT_DIR = APP_DIR.parent
STATIC_DIR = APP_DIR / "static"

REPO_URL = os.getenv("OPS_REPO_URL", "https://github.com/noelmage/DefCon34Badge.git")
REPO_BRANCH = os.getenv("OPS_REPO_BRANCH", "main")
REPO_PATH = Path(os.getenv("OPS_REPO_PATH", ROOT_DIR / "data" / "DefCon34Badge"))
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
GOOGLE_ALLOWED_EMAIL = os.getenv("GOOGLE_ALLOWED_EMAIL", "").strip().lower()
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "https://dc34.henry.house/auth/google/callback")
SESSION_SECRET = os.getenv("SESSION_SECRET", "")

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
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET or "oauth-not-configured",
    session_cookie="dc34_session",
    max_age=12 * 60 * 60,
    same_site="lax",
    https_only=True,
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
repo_lock = threading.Lock()
oauth = OAuth()
oauth.register(
    name="google",
    client_id=GOOGLE_CLIENT_ID,
    client_secret=GOOGLE_CLIENT_SECRET,
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)


class GitCommandError(RuntimeError):
    def __init__(self, args: list[str], output: str):
        self.args = args
        self.output = output
        super().__init__(f"git {' '.join(args)} failed: {output}")


def oauth_is_configured() -> bool:
    return all((GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_ALLOWED_EMAIL, SESSION_SECRET))


def require_auth(request: Request) -> str:
    if not oauth_is_configured():
        raise HTTPException(503, "Google OAuth is not configured")
    email = str(request.session.get("email", "")).strip().lower()
    if not email or not secrets.compare_digest(email, GOOGLE_ALLOWED_EMAIL):
        raise HTTPException(401, "Sign in with the allowed Google account")
    return email


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
        raise GitCommandError(args, redact((result.stderr or result.stdout).strip()))
    return result.stdout.strip()


@app.exception_handler(GitCommandError)
def git_command_error(_: object, exc: GitCommandError) -> JSONResponse:
    return JSONResponse(status_code=502, content={"detail": str(exc)})


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


def relative_path(path: Path) -> str:
    return str(path.relative_to(REPO_PATH)).replace("\\", "/")


def document_version(path: Path) -> str:
    return run_git(["rev-parse", f"HEAD:{relative_path(path)}"], REPO_PATH)


def is_push_race(error: GitCommandError) -> bool:
    return any(marker in error.output.lower() for marker in ("non-fast-forward", "fetch first", "[rejected]"))


def is_rebase_conflict(error: GitCommandError) -> bool:
    return any(marker in error.output.lower() for marker in ("conflict", "could not apply", "resolve all conflicts"))


def abort_and_reset() -> None:
    subprocess.run(git_prefix() + ["rebase", "--abort"], cwd=REPO_PATH, capture_output=True, check=False)
    run_git(["fetch", "origin", "--prune"], REPO_PATH)
    run_git(["reset", "--hard", f"origin/{REPO_BRANCH}"], REPO_PATH)


def write_pending_change(message: str, paths: list[Path], snapshots: dict[Path, bytes]) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    pending = REPO_PATH / "docs" / "operations" / "pending-conflicts" / f"{stamp}_{slugify(message)}.md"
    markdown_paths = [path for path in paths if path.suffix.lower() in {".md", ".markdown"}]
    artifact_paths = [path for path in paths if path not in markdown_paths]
    parts = [
        f"# Pending dashboard change - {today()}",
        "",
        f"- **Intent:** {message}",
        "- **Reason:** GitHub changed the same document while this dashboard action was being saved.",
        "- **Status:** Review and merge this pending change into the current document deliberately.",
    ]
    if artifact_paths:
        parts.extend(["", "## Preserved artifacts", ""])
        parts.extend(f"- {relative_path(path)}" for path in artifact_paths)
    for path in markdown_paths:
        parts.extend(["", f"## Intended content: {relative_path(path)}", "", snapshots[path].decode("utf-8", errors="replace").rstrip()])
    pending.parent.mkdir(parents=True, exist_ok=True)
    pending.write_text("\n".join(parts).rstrip() + "\n", encoding="utf-8")
    return pending


def preserve_conflict(message: str, paths: list[Path], snapshots: dict[Path, bytes]) -> None:
    abort_and_reset()
    artifacts = [path for path in paths if path.suffix.lower() not in {".md", ".markdown"}]
    for path in artifacts:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(snapshots[path])
    pending = write_pending_change(message, paths, snapshots)
    recovery_paths = [*artifacts, pending]
    run_git(["add", *(relative_path(path) for path in recovery_paths)], REPO_PATH)
    run_git(["commit", "-m", f"Preserve pending change: {message[:50]}"], REPO_PATH)
    run_git(["push", "origin", REPO_BRANCH], REPO_PATH)
    raise HTTPException(409, f"A concurrent GitHub edit needs review. The pending change is saved at {relative_path(pending)}.")


def commit_and_push(message: str, paths: list[Path]) -> None:
    rel_paths = [str(path.relative_to(REPO_PATH)).replace("\\", "/") for path in paths]
    run_git(["add", *rel_paths], REPO_PATH)
    if not run_git(["status", "--porcelain"], REPO_PATH):
        return
    run_git(["commit", "-m", message], REPO_PATH)
    snapshots = {path: path.read_bytes() for path in paths}
    for attempt in range(2):
        try:
            run_git(["pull", "--rebase", "origin", REPO_BRANCH], REPO_PATH)
        except GitCommandError as exc:
            if not is_rebase_conflict(exc):
                raise
            preserve_conflict(message, paths, snapshots)
        try:
            run_git(["push", "origin", REPO_BRANCH], REPO_PATH)
            return
        except GitCommandError as exc:
            if attempt == 0 and is_push_race(exc):
                continue
            preserve_conflict(message, paths, snapshots)


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


def recent_commits(limit: int = 8) -> list[dict[str, Any]]:
    output = run_git(
        ["log", f"-{limit}", "--name-only", "--format=%x1e%H%x1f%aI%x1f%s"],
        REPO_PATH,
    )
    commits: list[dict[str, Any]] = []
    for record in output.split("\x1e"):
        lines = [line for line in record.strip().splitlines() if line.strip()]
        if not lines:
            continue
        sha, timestamp, subject = lines[0].split("\x1f", maxsplit=2)
        commits.append({
            "sha": sha[:7],
            "timestamp": timestamp,
            "subject": subject,
            "paths": lines[1:5],
        })
    return commits


def markdown_log_entries(key: str, pattern: str, kind: str, limit: int = 4) -> list[dict[str, str]]:
    text = read_document(key)
    matches = list(re.finditer(pattern, text, flags=re.MULTILINE))
    entries: list[dict[str, str]] = []
    for index, match in enumerate(matches):
        body_end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[match.end():body_end]
        summary = re.search(r"^- \*\*(?:Result|Conclusion|Reason|Objective):\*\*\s*(.+)$", body, flags=re.MULTILINE)
        entries.append({
            "kind": kind,
            "title": match.group(1).strip(),
            "summary": summary.group(1).strip() if summary else body.strip().splitlines()[0].lstrip("- ").strip() if body.strip() else "",
            "path": DOCUMENTS[key][1],
        })
    return list(reversed(entries[-limit:]))


def activity_feed() -> dict[str, list[dict[str, Any]]]:
    return {
        "commits": recent_commits(),
        "journal": markdown_log_entries("journal", r"^##\s+(.+)$", "Journal"),
        "decisions": markdown_log_entries("decisions", r"^###\s+(.+)$", "Decision"),
    }


@app.get("/", response_model=None)
def index(request: Request):
    if not oauth_is_configured():
        return HTMLResponse("Google OAuth is not configured yet.", status_code=503)
    if str(request.session.get("email", "")).strip().lower() != GOOGLE_ALLOWED_EMAIL:
        return RedirectResponse("/auth/google/login", status_code=302)
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/auth/google/login")
async def google_login(request: Request) -> RedirectResponse:
    if not oauth_is_configured():
        raise HTTPException(503, "Google OAuth is not configured")
    return await oauth.google.authorize_redirect(request, GOOGLE_REDIRECT_URI, nonce=secrets.token_urlsafe(24))


@app.get("/auth/google/callback")
async def google_callback(request: Request) -> RedirectResponse:
    if not oauth_is_configured():
        raise HTTPException(503, "Google OAuth is not configured")
    try:
        token = await oauth.google.authorize_access_token(request)
        userinfo = token.get("userinfo") or await oauth.google.parse_id_token(request, token)
    except OAuthError as exc:
        raise HTTPException(401, f"Google sign-in failed: {exc.error}") from exc

    email = str(userinfo.get("email", "")).strip().lower()
    if not userinfo.get("email_verified") or not secrets.compare_digest(email, GOOGLE_ALLOWED_EMAIL):
        request.session.clear()
        raise HTTPException(403, "This Google account is not allowed to access the ops console")

    request.session.clear()
    request.session["email"] = email
    request.session["name"] = str(userinfo.get("name", ""))
    return RedirectResponse("/", status_code=302)


@app.post("/auth/logout")
def google_logout(request: Request, _: str = Depends(require_auth)) -> JSONResponse:
    request.session.clear()
    return JSONResponse({"ok": True})


@app.get("/api/dashboard")
def dashboard(_: str = Depends(require_auth)) -> dict[str, Any]:
    with repo_lock:
        sync_repo()
        docs = [{"key": key, "title": title, "path": path} for key, (title, path) in DOCUMENTS.items()]
        operations = read_document("operations")
        sources = read_document("sources")
        assessment = read_document("assessment")
        journal = read_document("journal")
        evidence = evidence_files()
        head = run_git(["rev-parse", "--short", "HEAD"], REPO_PATH)
        activity = activity_feed()
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
        "activity": activity,
    }


@app.get("/api/documents/{key}")
def get_document(key: str, _: str = Depends(require_auth)) -> dict[str, str]:
    with repo_lock:
        sync_repo()
        path = document_path(key)
        return {"key": key, "title": DOCUMENTS[key][0], "path": DOCUMENTS[key][1], "markdown": read_document(key), "head": run_git(["rev-parse", "HEAD"], REPO_PATH), "version": document_version(path)}


@app.post("/api/documents/{key}")
def save_document(key: str, markdown: str = Form(...), expected_version: str = Form(...), _: str = Depends(require_auth)) -> dict[str, str]:
    with repo_lock:
        sync_repo()
        path = document_path(key)
        if not secrets.compare_digest(expected_version, document_version(path)):
            raise HTTPException(409, "This document changed in GitHub. Your draft is still in the editor; reload and merge it before saving.")
        path.write_text(markdown.rstrip() + "\n", encoding="utf-8")
        commit_and_push(f"Update {DOCUMENTS[key][0].lower()}", [path])
        return {"path": DOCUMENTS[key][1], "head": run_git(["rev-parse", "HEAD"], REPO_PATH), "version": document_version(path)}


@app.post("/api/fetch-source")
async def fetch_source(url: str = Form(...), _: str = Depends(require_auth)) -> dict[str, str]:
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
        source_id = f"SRC-{count_matches(read_document('sources'), r'^\|\s*SRC-') + 1:03d}"
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
    _: str = Depends(require_auth),
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
    _: str = Depends(require_auth),
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
def add_journal(objective: str = Form(...), result: str = Form(""), interpretation: str = Form(""), next_action: str = Form(""), _: str = Depends(require_auth)) -> dict[str, str]:
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
async def upload_evidence(file: UploadFile = File(...), description: str = Form(""), source: str = Form(""), related_item: str = Form(""), _: str = Depends(require_auth)) -> dict[str, str]:
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
