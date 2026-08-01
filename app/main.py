import hashlib
import os
import re
import secrets
import sqlite3
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
DB_PATH = Path(os.getenv("DEFCON_OPS_DB", ROOT_DIR / "data" / "ops.db"))
EVIDENCE_DIR = Path(os.getenv("DEFCON_OPS_EVIDENCE_DIR", ROOT_DIR / "data" / "evidence"))
OPS_USER = os.getenv("DEFCON_OPS_USER", "admin")
OPS_PASSWORD = os.getenv("DEFCON_OPS_PASSWORD", "change-me")

app = FastAPI(title="DEF CON 34 Badge Ops Console")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
security = HTTPBasic()


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


def db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def rows(query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with db() as conn:
        return [dict(row) for row in conn.execute(query, params).fetchall()]


def execute(query: str, params: tuple[Any, ...] = ()) -> int:
    with db() as conn:
        cur = conn.execute(query, params)
        conn.commit()
        return int(cur.lastrowid)


def slugify(value: str) -> str:
    value = value.lower().strip()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-")[:80] or "item"


def init_db() -> None:
    with db() as conn:
        conn.executescript(
            """
            create table if not exists sources (
              id integer primary key,
              title text not null,
              url text unique not null,
              source_type text default 'web',
              author text default '',
              published_date text default '',
              accessed_at text not null,
              reliability text default 'unrated',
              firsthand integer default 0,
              evidence_included integer default 0,
              processed integer default 0,
              summary text default '',
              raw_text_path text default ''
            );

            create table if not exists claims (
              id integer primary key,
              claim text not null,
              category text default 'general',
              confidence text default 'Unverified',
              source_id integer,
              practical_significance text default '',
              created_at text not null,
              foreign key(source_id) references sources(id)
            );

            create table if not exists puzzles (
              id integer primary key,
              identifier text not null,
              name text not null,
              status text default 'Not investigated',
              confidence text default 'Speculative',
              notes text default '',
              candidate_flag text default '',
              updated_at text not null
            );

            create table if not exists evidence (
              id integer primary key,
              filename text not null,
              description text default '',
              source text default '',
              acquired_at text not null,
              collection_method text default '',
              related_puzzle text default '',
              sha256 text not null,
              storage_path text not null,
              original integer default 1
            );

            create table if not exists journal (
              id integer primary key,
              created_at text not null,
              objective text not null,
              result text default '',
              interpretation text default '',
              confidence text default 'Moderate',
              next_action text default ''
            );

            create table if not exists tasks (
              id integer primary key,
              priority text not null,
              objective text not null,
              reason text default '',
              required_tools text default '',
              risk text default 'Low',
              next_action text default '',
              status text default 'Open',
              updated_at text not null
            );
            """
        )
        existing = conn.execute("select count(*) from tasks").fetchone()[0]
        if existing == 0:
            conn.executemany(
                """insert into tasks
                (priority, objective, reason, required_tools, risk, next_action, status, updated_at)
                values (?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    ("P0", "Verify cloud repo access", "Blocks cloud research and phone/Pi continuity", "Codex cloud, GitHub", "Low", "Run git fetch origin --prune in a fresh cloud task", "Open", now_iso()),
                    ("P1", "Process WIRED Baochip article", "Initial source anchors the badge architecture model", "Web fetcher, source log", "Low", "Fetch the article URL and extract material claims", "Open", now_iso()),
                    ("P2", "Create equipment matrix", "Prevents plans from assuming unavailable tools", "Manual inventory", "Low", "Enter actual laptop, Pi, USB, serial, and analysis tools", "Open", now_iso()),
                ],
            )
        conn.commit()


@app.on_event("startup")
def startup() -> None:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    init_db()


@app.get("/")
def index(_: None = Depends(require_auth)) -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/dashboard")
def dashboard(_: None = Depends(require_auth)) -> dict[str, Any]:
    return {
        "sources": rows("select * from sources order by accessed_at desc limit 20"),
        "claims": rows("select claims.*, sources.title as source_title from claims left join sources on claims.source_id = sources.id order by claims.created_at desc limit 40"),
        "puzzles": rows("select * from puzzles order by updated_at desc"),
        "evidence": rows("select * from evidence order by acquired_at desc limit 30"),
        "journal": rows("select * from journal order by created_at desc limit 20"),
        "tasks": rows("select * from tasks order by case priority when 'P0' then 0 when 'P1' then 1 when 'P2' then 2 else 3 end, updated_at desc"),
    }


@app.post("/api/fetch-source")
async def fetch_source(url: str = Form(...), _: None = Depends(require_auth)) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
            response = await client.get(url, headers={"User-Agent": "DEFCON34BadgeOps/0.1"})
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Fetch failed: {exc}") from exc

    soup = BeautifulSoup(response.text, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    title = (soup.title.string.strip() if soup.title and soup.title.string else url)[:240]
    text = "\n".join(line.strip() for line in soup.get_text("\n").splitlines() if line.strip())
    text = text[:250000]

    evidence_path = EVIDENCE_DIR / f"{datetime.now().strftime('%Y-%m-%d')}_source_{slugify(title)}.txt"
    evidence_path.write_text(f"URL: {url}\nFetched: {now_iso()}\nTitle: {title}\n\n{text}", encoding="utf-8")

    with db() as conn:
        conn.execute(
            """insert into sources
            (title, url, source_type, accessed_at, reliability, firsthand, evidence_included, processed, summary, raw_text_path)
            values (?, ?, 'web', ?, 'unrated', 0, 1, 0, '', ?)
            on conflict(url) do update set
              title=excluded.title,
              accessed_at=excluded.accessed_at,
              evidence_included=1,
              raw_text_path=excluded.raw_text_path""",
            (title, url, now_iso(), str(evidence_path)),
        )
        conn.commit()
        source_id = int(conn.execute("select id from sources where url = ?", (url,)).fetchone()[0])
    return {"id": source_id, "title": title, "characters": len(text), "path": str(evidence_path)}


@app.post("/api/claim")
def add_claim(
    claim: str = Form(...),
    category: str = Form("general"),
    confidence: str = Form("Unverified"),
    source_id: int | None = Form(None),
    practical_significance: str = Form(""),
    _: None = Depends(require_auth),
) -> dict[str, Any]:
    claim_id = execute(
        "insert into claims (claim, category, confidence, source_id, practical_significance, created_at) values (?, ?, ?, ?, ?, ?)",
        (claim, category, confidence, source_id, practical_significance, now_iso()),
    )
    return {"id": claim_id}


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
    puzzle_id = execute(
        "insert into puzzles (identifier, name, status, confidence, notes, candidate_flag, updated_at) values (?, ?, ?, ?, ?, ?, ?)",
        (identifier, name, status, confidence, notes, candidate_flag, now_iso()),
    )
    return {"id": puzzle_id}


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
    task_id = execute(
        "insert into tasks (priority, objective, reason, required_tools, risk, next_action, status, updated_at) values (?, ?, ?, ?, ?, ?, 'Open', ?)",
        (priority, objective, reason, required_tools, risk, next_action, now_iso()),
    )
    return {"id": task_id}


@app.post("/api/journal")
def add_journal(
    objective: str = Form(...),
    result: str = Form(""),
    interpretation: str = Form(""),
    confidence: str = Form("Moderate"),
    next_action: str = Form(""),
    _: None = Depends(require_auth),
) -> dict[str, Any]:
    journal_id = execute(
        "insert into journal (created_at, objective, result, interpretation, confidence, next_action) values (?, ?, ?, ?, ?, ?)",
        (now_iso(), objective, result, interpretation, confidence, next_action),
    )
    return {"id": journal_id}


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
    target = EVIDENCE_DIR / safe_name
    target.write_bytes(raw)
    evidence_id = execute(
        """insert into evidence
        (filename, description, source, acquired_at, collection_method, related_puzzle, sha256, storage_path, original)
        values (?, ?, ?, ?, ?, ?, ?, ?, 1)""",
        (safe_name, description, source, now_iso(), collection_method, related_puzzle, digest, str(target)),
    )
    return {"id": evidence_id, "filename": safe_name, "sha256": digest}


@app.get("/api/briefing")
def briefing(_: None = Depends(require_auth)) -> dict[str, str]:
    data = dashboard(_)
    p0 = [t for t in data["tasks"] if t["priority"] == "P0" and t["status"] == "Open"]
    p1 = [t for t in data["tasks"] if t["priority"] == "P1" and t["status"] == "Open"]
    lines = [
        "# DEF CON 34 Badge Operations Briefing",
        "",
        "## Current Situation",
        f"- Sources processed: {len(data['sources'])}",
        f"- Claims recorded: {len(data['claims'])}",
        f"- Evidence items: {len(data['evidence'])}",
        f"- Known puzzles: {len(data['puzzles'])}",
        "",
        "## Priority Queue",
    ]
    for item in (p0 + p1 + data["tasks"])[:8]:
        lines.append(f"- {item['priority']}: {item['objective']} - next: {item['next_action'] or 'define first step'}")
    lines.extend(["", "## Next Review Trigger", "- New official badge information, firmware changes, a new puzzle, or the start/end of a badge work session."])
    return {"markdown": "\n".join(lines)}
