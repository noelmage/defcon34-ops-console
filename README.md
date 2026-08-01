# DEF CON 34 Badge Ops Console

A private web console for DEF CON 34 badge research operations.

The DEF CON 34 GitHub repository is the source of truth. The app keeps a
persistent working clone on the VPS, pulls before reads, and commits/pushes
changes after writes.

It provides:

- Command Mode dashboard and priority queue
- single-URL source fetching into the repo
- direct reading and editing of the agent-owned Markdown documents
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

## Hostinger Docker Manager

The repository carries two Compose files for two different environments:

- `docker-compose.yml` builds from the local checkout for development.
- `docker-compose.yaml` runs the published GitHub Container Registry image for
  Hostinger's **Compose from URL** workflow.

Pushing `master` publishes `ghcr.io/noelmage/defcon34-ops-console:latest`
through GitHub Actions. The GitHub Container Registry package must be public
so Hostinger can pull it without registry credentials. Runtime credentials
remain Hostinger environment variables; never place them in this repository.

## Data And Sync

Persistent clone/cache data lives under:

```text
data/
```

The console reads and writes the established Markdown documents under `docs/`.
Quick actions append to the source register, operations briefing, knowledge bases,
evidence inventory, and research journal; the Markdown Files view edits those
same documents directly. Fetched pages are stored under:

```text
evidence/derived/
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
