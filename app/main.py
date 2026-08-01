import hashlib
import json
import os
import re
import secrets
import shutil
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

app = FastAPI(title="DEF CON 34 Badge Ops Console")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
security = HTTPBasic()
repo_lock = threading.Lock()


def require_auth(credentials: HTTPBasicCredentials = Depends(security)) -> None:
    valid_user = secrets.compare_digest(credentials.username, OPS_USER)
    valid_password = secrets.compare_digest(credentials.password, OPS_PASSWORD)
    if not (valid_user and valid_password):
        raise HTTPException(
            status_code=401,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Basic"},
        )


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def slugify(value: str) -> str:
    value = value.lower().strip()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-")[:80] or "item"


def redact(value: str) -> str:
    if GITHUB_TOKEN:
        value = value.replace(GITHUB_TOKEN, "[REDACTED]")
    return value


def git_prefix() -> list[str]:
    if not GITHUB_TOKEN:
        return ["git"]
    replacement = f"https://x-access-token:{GITHUB_TOKEN}@github.com/"
    return ["git", "-c", f"url.{replacement}.insteadOf=https://github.com/"]


def run_git(args: list[str], cwd: Path | None = None) -> str:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    result = subprocess.run(
        git_prefix() + args,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = redact((result.stderr or result.stdout).strip())
        raise HTTPException(status_code=500, detail=f"git {' '.join(args)} failed: {detail}")
    return result.stdout.strip()


def ensure_repo() -> None:
    REPO_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not (REPO_PATH / ".git").exists():
        if REPO_PATH.exists() and any(REPO_PATH.iterdir()):
            raise HTTPException(status_code=500, detail=f"{REPO_PATH} exists but is not a Git checkout")
        run_git(["clone", "--branch", REPO_BRANCH, REPO_URL, str(REPO_PATH)], cwd=REPO_PATH.parent)
    run_git(["config", "user.name", "DEF CON 34 Ops Console"], cwd=REPO_PATH)
    run_git(["config", "user.email", "ops-console@henry.house"], cwd=REPO_PATH)
    run_git(["remote", "set-url", "origin", REPO_URL], cwd=REPO_PATH)


def sync_repo() -> None:
    ensure_repo()
    status = run_git(["status", "--porcelain"], cwd=REPO_PATH)
    if status:
        raise HTTPException(status_code=409, detail="Repo has uncommitted changes; refusing to pull")
    run_git(["fetch", "origin", "--prune"], cwd=REPO_PATH)
    run_git(["checkout", REPO_BRANCH], cwd=REPO_PATH)
    run_git(["pull", "--ff-only", "origin", REPO_BRANCH], cwd=REPO_PATH)


def commit_and_push(message: str, paths: list[Path]) -> None:
    rel_paths = [str(path.relative_to(REPO_PATH)).replace("\\", "/") for path in paths]
    run_git(["add", *rel_paths], cwd=REPO_PATH)
    if not run_git(["status", "--porcelain"], cwd=REPO_PATH):
        return
    run_git(["commit", "-m", message], cwd=REPO_PATH)
    run_git(["pull", "--rebase", "origin", REPO_BRANCH], cwd=REPO_PATH)
    run_git(["push", "origin", REPO_BRANCH], cwd=REPO_PATH)


def ops_dir() -> Path:
    path = REPO_PATH / "ops"
    path.mkdir(exist_ok=True)
    return path


def read_list(name: str) -> list[dict[str, Any]]:
    path = ops_dir() / f"{name}.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail=f"Invalid JSON in {path.name}: {exc}") from exc
    return data if isinstance(data, list) else []


def write_list(name: str, records: list[dict[str, Any]]) -> Path:
    path = ops_dir() / f"{name}.json"
    path.write_text(json.dumps(records, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def next_id(records: list[dict[str, Any]]) -> int:
    return max((int(record.get("id", 0)) for record in records), default=0) + 1


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def scan_repo_evidence(existing: list[dict[str, Any]]) -> list[dict[str, Any]]:
    known_paths = {record.get("storage_path") for record in existing}
    records = list(existing)
    for base in [REPO_PATH / "evidence" / "sources", REPO_PATH / "evidence" / "derived"]:
        if not base.exists():
            continue
        for path in sorted(p for p in base.iterdir() if p.is_file()):
            rel = str(path.relative_to(REPO_PATH)).replace("\\", "/")
            if rel in known_paths:
                continue
            records.append(
                {
                    "id": f"repo:{rel}",
                    "filename": path.name,
                    "description": "Repository evidence file",
                    "source": "DefCon34Badge repository",
                    "acquired_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).replace(microsecond=0).isoformat(),
                    "collection_method": "repo scan",
                    "related_puzzle": "",
                    "sha256": sha256_file(path),
                    "storage_path": rel,
                    "original": path.suffix.lower() == ".pdf",
                }
            )
    return records


def scan_repo_sources(existing: list[dict[str, Any]]) -> list[dict[str, Any]]:
    records = list(existing)
    known_titles = {record.get("title") for record in records}
    metadata_dir = REPO_PATH / "evidence" / "derived"
    if metadata_dir.exists():
        for path in sorted(metadata_dir.glob("*_metadata.json")):
            try:
                metadata = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            title = metadata.get("title") or path.stem
            if title in known_titles:
                continue
            records.append(
                {
                    "id": f"repo:{path.name}",
                    "title": title,
                    "url": "https://www.wired.com/story/defcon-34-badge-baochip-andrew-bunnie-huang/",
                    "source_type": metadata.get("publisher", "source"),
                    "author": metadata.get("author", ""),
                    "published_date": "",
                    "accessed_at": metadata.get("pdf_metadata", {}).get("/CreationDate", ""),
                    "reliability": "technical journalism",
                    "firsthand": False,
                    "evidence_included": True,
                    "processed": True,
                    "summary": "",
                    "raw_text_path": str(path.relative_to(REPO_PATH)).replace("\\", "/"),
                }
            )
    return records


def record_append(name: str, record: dict[str, Any], message: str) -> dict[str, Any]:
    with repo_lock:
        sync_repo()
        records = read_list(name)
        record["id"] = next_id(records)
        records.append(record)
        path = write_list(name, records)
        commit_and_push(message, [path])
    return record


@app.get("/")
def index(_: None = Depends(require_auth)) -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/dashboard")
def dashboard(_: None = Depends(require_auth)) -> dict[str, Any]:
    with repo_lock:
        sync_repo()
        sources = scan_repo_sources(read_list("sources"))
        evidence = scan_repo_evidence(read_list("evidence"))
        claims = read_list("claims")
        puzzles = read_list("puzzles")
        journal = read_list("journal")
        tasks = read_list("tasks")
    return {
        "repo": {
            "path": str(REPO_PATH),
            "branch": REPO_BRANCH,
            "head": run_git(["rev-parse", "--short", "HEAD"], cwd=REPO_PATH),
        },
        "sources": sources[-50:][::-1],
        "claims": claims[-80:][::-1],
        "puzzles": puzzles[::-1],
        "evidence": evidence[-80:][::-1],
        "journal": journal[-50:][::-1],
        "tasks": sorted(tasks, key=lambda item: ({"P0": 0, "P1": 1, "P2": 2}.get(item.get("priority"), 3), item.get("updated_at", ""))),
    }


@app.post("/api/fetch-source")
async def fetch_source(url: str = Form(...), _: None = Depends(require_auth)) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
            response = await client.get(url, headers={"User-Agent": "DEFCON34BadgeOps/0.2"})
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Fetch failed: {exc}") from exc

    soup = BeautifulSoup(response.text, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    title = (soup.title.string.strip() if soup.title and soup.title.string else url)[:240]
    text = "\n".join(line.strip() for line in soup.get_text("\n").splitlines() if line.strip())[:250000]

    with repo_lock:
        sync_repo()
        target_dir = REPO_PATH / "ops" / "fetched-sources"
        target_dir.mkdir(parents=True, exist_ok=True)
        evidence_path = target_dir / f"{datetime.now().strftime('%Y-%m-%d')}_source_{slugify(title)}.txt"
        evidence_path.write_text(f"URL: {url}\nFetched: {now_iso()}\nTitle: {title}\n\n{text}", encoding="utf-8")

        sources = read_list("sources")
        existing = next((item for item in sources if item.get("url") == url), None)
        if existing:
            existing.update({"title": title, "accessed_at": now_iso(), "raw_text_path": str(evidence_path.relative_to(REPO_PATH)).replace("\\", "/")})
            source_id = existing["id"]
        else:
            source_id = next_id(sources)
            sources.append(
                {
                    "id": source_id,
                    "title": title,
                    "url": url,
                    "source_type": "web",
                    "author": "",
                    "published_date": "",
                    "accessed_at": now_iso(),
                    "reliability": "unrated",
                    "firsthand": False,
                    "evidence_included": True,
                    "processed": False,
                    "summary": "",
                    "raw_text_path": str(evidence_path.relative_to(REPO_PATH)).replace("\\", "/"),
                }
            )
        sources_path = write_list("sources", sources)
        commit_and_push(f"Record source: {title[:60]}", [evidence_path, sources_path])
    return {"id": source_id, "title": title, "characters": len(text), "path": str(evidence_path.relative_to(REPO_PATH)).replace("\\", "/")}


@app.post("/api/claim")
def add_claim(
    claim: str = Form(...),
    category: str = Form("general"),
    confidence: str = Form("Unverified"),
    source_id: int | None = Form(None),
    practical_significance: str = Form(""),
    _: None = Depends(require_auth),
) -> dict[str, Any]:
    return record_append(
        "claims",
        {
            "claim": claim,
            "category": category,
            "confidence": confidence,
            "source_id": source_id,
            "source_title": "",
            "practical_significance": practical_significance,
            "created_at": now_iso(),
        },
        "Record badge research claim",
    )


@app.post("/api/puzzle")
def add_puzzle(
    identifier: str = Form(...),
    name: str = Form(...),
    status: str = Form("Not investigated"),
    confidence: str = Form("Speculative"),
    notes: str = Form(""),
    candidate_flag: str = Form(""),
    _: None = Depends(require_auth),
) -> dict[str, Any]:
    return record_append(
        "puzzles",
        {
            "identifier": identifier,
            "name": name,
            "status": status,
            "confidence": confidence,
            "notes": notes,
            "candidate_flag": candidate_flag,
            "updated_at": now_iso(),
        },
        f"Record puzzle: {identifier}",
    )


@app.post("/api/task")
def add_task(
    priority: str = Form(...),
    objective: str = Form(...),
    reason: str = Form(""),
    required_tools: str = Form(""),
    risk: str = Form("Low"),
    next_action: str = Form(""),
    _: None = Depends(require_auth),
) -> dict[str, Any]:
    return record_append(
        "tasks",
        {
            "priority": priority,
            "objective": objective,
            "reason": reason,
            "required_tools": required_tools,
            "risk": risk,
            "next_action": next_action,
            "status": "Open",
            "updated_at": now_iso(),
        },
        f"Record task: {objective[:60]}",
    )


@app.post("/api/journal")
def add_journal(
    objective: str = Form(...),
    result: str = Form(""),
    interpretation: str = Form(""),
    confidence: str = Form("Moderate"),
    next_action: str = Form(""),
    _: None = Depends(require_auth),
) -> dict[str, Any]:
    return record_append(
        "journal",
        {
            "created_at": now_iso(),
            "objective": objective,
            "result": result,
            "interpretation": interpretation,
            "confidence": confidence,
            "next_action": next_action,
        },
        f"Add research journal entry: {objective[:50]}",
    )


@app.post("/api/upload-evidence")
async def upload_evidence(
    file: UploadFile = File(...),
    description: str = Form(""),
    source: str = Form(""),
    collection_method: str = Form("upload"),
    related_puzzle: str = Form(""),
    _: None = Depends(require_auth),
) -> dict[str, Any]:
    raw = await file.read()
    digest = hashlib.sha256(raw).hexdigest()
    safe_name = f"{datetime.now().strftime('%Y-%m-%d')}_{slugify(Path(file.filename or 'evidence').stem)}_{digest[:10]}{Path(file.filename or '').suffix}"
    with repo_lock:
        sync_repo()
        target_dir = REPO_PATH / "evidence" / "uploads"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / safe_name
        target.write_bytes(raw)
        evidence = read_list("evidence")
        record = {
            "id": next_id(evidence),
            "filename": safe_name,
            "description": description,
            "source": source,
            "acquired_at": now_iso(),
            "collection_method": collection_method,
            "related_puzzle": related_puzzle,
            "sha256": digest,
            "storage_path": str(target.relative_to(REPO_PATH)).replace("\\", "/"),
            "original": True,
        }
        evidence.append(record)
        evidence_path = write_list("evidence", evidence)
        commit_and_push(f"Add evidence: {safe_name}", [target, evidence_path])
    return {"id": record["id"], "filename": safe_name, "sha256": digest}


@app.get("/api/briefing")
def briefing(_: None = Depends(require_auth)) -> dict[str, str]:
    data = dashboard(_)
    p0 = [t for t in data["tasks"] if t.get("priority") == "P0" and t.get("status") == "Open"]
    p1 = [t for t in data["tasks"] if t.get("priority") == "P1" and t.get("status") == "Open"]
    lines = [
        "# DEF CON 34 Badge Operations Briefing",
        "",
        "## Current Situation",
        f"- Repo head: {data['repo']['head']} on {data['repo']['branch']}",
        f"- Sources processed: {len(data['sources'])}",
        f"- Claims recorded: {len(data['claims'])}",
        f"- Evidence items: {len(data['evidence'])}",
        f"- Known puzzles: {len(data['puzzles'])}",
        "",
        "## Priority Queue",
    ]
    for item in (p0 + p1 + data["tasks"])[:8]:
        lines.append(f"- {item.get('priority')}: {item.get('objective')} - next: {item.get('next_action') or 'define first step'}")
    lines.extend(["", "## Next Review Trigger", "- New official badge information, firmware changes, a new puzzle, or the start/end of a badge work session."])
    return {"markdown": "\n".join(lines)}
