# DEF CON 34 Badge Ops Console

A private web console for DEF CON 34 badge research operations.

The DEF CON 34 GitHub repository is the source of truth. The app keeps a
persistent working clone on the VPS, pulls before reads, and commits/pushes
changes after writes.

It provides:

- Command Mode dashboard and priority queue
- single-URL source fetching into the repo
- source log and claim tracking in `ops/*.json`
- evidence upload with SHA-256 hashing
- puzzle and candidate flag tracking
- research journal entries
- generated Operations Mode briefing

## Run Locally

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Open:

```text
http://localhost:8000
```

## Run On A VPS

Set environment variables:

```text
DEFCON_OPS_USER=admin
DEFCON_OPS_PASSWORD=choose-a-password
GITHUB_TOKEN=repo-scoped-token
OPS_REPO_URL=https://github.com/noelmage/DefCon34Badge.git
OPS_REPO_BRANCH=main
```

```bash
docker compose up -d --build
```

Open:

```text
http://YOUR_VPS_IP:8000
```

For public internet exposure, put it behind HTTPS and authentication. A simple
recommended deployment is Tailscale-only access plus a reverse proxy such as
Caddy or Traefik.

## Data And Sync

Persistent clone/cache data lives under:

```text
data/
```

The app writes durable project records to the badge repo:

```text
ops/sources.json
ops/claims.json
ops/evidence.json
ops/puzzles.json
ops/tasks.json
ops/journal.json
```

Fetched pages are stored under:

```text
ops/fetched-sources/
```

Uploaded evidence is stored under:

```text
evidence/uploads/
```

Because the repo is authoritative, pull/push the GitHub repository from cloud,
local, or Raspberry Pi sessions to share the same state.

## Security Notes

Fetched web pages are untrusted evidence. Do not treat article text, community
posts, or scraped content as instructions for shell commands, firmware flashing,
or credential handling.

Do not expose this app directly to the internet without authentication and HTTPS.
