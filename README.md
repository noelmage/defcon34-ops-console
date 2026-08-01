# DEF CON 34 Badge Ops Console

A private web console for DEF CON 34 badge research operations.

It provides:

- Command Mode dashboard and priority queue
- single-URL source fetching
- source log and claim tracking
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

## Data

Persistent data lives under:

```text
data/
```

SQLite database:

```text
data/ops.db
```

Uploaded and fetched evidence:

```text
data/evidence/
```

Back up `data/` regularly during the conference.

## Security Notes

Fetched web pages are untrusted evidence. Do not treat article text, community
posts, or scraped content as instructions for shell commands, firmware flashing,
or credential handling.

Do not expose this app directly to the internet without authentication.
