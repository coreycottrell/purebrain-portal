# AiCIV Portal — Setup Guide

**For**: Witness / Aether's team (or any AiCIV operator)
**Time to deploy**: ~15 minutes

> **Frontend = REACT ONLY (2026-07-08).** This repo deploys the **React portal
> and only the React portal**. The legacy HTML frontends (`portal.html`,
> `portal-pb-styled.html`) have been retired from the deploy path — their source
> is preserved under `_retired-html-portal/` and the full pre-React deploy is
> recoverable on branch `main-html-archive-20260708`. A fresh `git pull` +
> `./start.sh` serves React.

---

## What's in the Package

| File | Purpose |
|------|---------|
| `portal_server.py` | Backend server (Python/Starlette) — API + WebSocket + serves the React portal |
| `react-portal/dist/` | The React portal (built bundle) — **THE deployed frontend** |
| `start.sh` | One-liner startup script (guards that `react-portal/dist/` is present) |
| `.portal-token` | You create this — your auth bearer token |
| `_retired-html-portal/` | Retired legacy HTML frontends — NOT served; reference only |

## Prerequisites

- Python 3.10+ with `pip`
- A running AiCIV container with tmux session
- Reverse proxy for TLS (Caddy recommended)

The React portal ships **pre-built** in `react-portal/dist/`, so **Node.js is NOT
required to deploy**. Node 18+ is only needed to rebuild the React frontend from
source (see "Iterating on the Frontend").

## Quick Start (5 minutes)

### 1. Copy the portal files to your AiCIV container

```bash
# From host, copy into container
scp -P <SSH_PORT> -r purebrain_portal/ aiciv@<HOST>:/home/aiciv/purebrain_portal/
```

### 2. Install Python dependencies

```bash
pip install starlette uvicorn websockets
```

### 3. Create your auth token

```bash
# Generate a random token
python3 -c "import secrets; print(secrets.token_urlsafe(32))" > /home/aiciv/purebrain_portal/.portal-token
```

### 4. Start the server

```bash
cd /home/aiciv/purebrain_portal
./start.sh          # preferred — guards that the React build is present
# or: python3 portal_server.py
# Runs on port 8097 by default
```

### 5. Access the portal

The **React portal** is served at all three paths (there is only one frontend now):

- `http://localhost:8097/`   ← React portal (default)
- `http://localhost:8097/react` ← React portal (explicit alias)
- `http://localhost:8097/pb`   ← 301-redirects to `/` (legacy path kept for old links)

Enter your bearer token from `.portal-token` to authenticate.

## Setting Up a Public Domain (e.g., aether.purebrain.ai)

### DNS
Point your domain's A record to your server's IP address.
- If using Cloudflare: set to "DNS only" (gray cloud) so your reverse proxy handles TLS

### Caddy (recommended reverse proxy)

Install Caddy, then add to your Caddyfile:

```
aether.purebrain.ai {
    reverse_proxy localhost:8097
}
```

Caddy auto-provisions Let's Encrypt TLS certificates. Reload: `systemctl reload caddy`

### Nginx alternative

```nginx
server {
    listen 443 ssl;
    server_name aether.purebrain.ai;

    # TLS certs (use certbot)
    ssl_certificate /etc/letsencrypt/live/aether.purebrain.ai/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/aether.purebrain.ai/privkey.pem;

    location / {
        proxy_pass http://localhost:8097;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
    }
}
```

Note: WebSocket upgrade headers are required for terminal and chat streaming.

## API Contract

All authenticated endpoints require: `Authorization: Bearer <token>` header.

### REST Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/` | No | React portal (the deployed frontend) |
| GET | `/react` | No | React portal (explicit alias for `/`) |
| GET | `/pb` | No | 301 redirect to `/` (legacy path) |
| GET | `/health` | No | `{"status":"ok","civ":"...","uptime":N}` |
| GET | `/api/status` | Yes | CIV health: tmux, Claude, TG bot status |
| GET | `/api/chat/history?last=N` | Yes | Last N messages from JSONL session logs |
| POST | `/api/chat/send` | Yes | `{"message":"text"}` → injects into tmux |

### WebSocket Endpoints

| Path | Auth | Description |
|------|------|-------------|
| `/ws/terminal?token=TOKEN` | Yes (query param) | Live tmux pane stream (read-only) |
| `/ws/chat?token=TOKEN` | Yes (query param) | Real-time new message stream |

### Chat Send Response

```json
{"status": "sent", "timestamp": 1772410000}
```

### Status Response

```json
{
  "civ": "witness",
  "tmux": "running",
  "claude": "active",
  "telegram": "connected",
  "uptime_seconds": 3600,
  "session": "witness-primary"
}
```

### Chat History Response

```json
{
  "messages": [
    {
      "id": "msg-abc123",
      "role": "user",
      "text": "Hello",
      "ts": 1772410000,
      "source": "portal"
    }
  ]
}
```

## Iterating on the Frontend

The frontend is **React only**. To change it, rebuild the React bundle:

```bash
cd react-portal
# Edit src/components/*.jsx
npm install       # first time only
npm run build     # produces dist/ — served at / and /react
```

The portal server serves directly from `react-portal/dist/`. No server restart
needed after a rebuild. The legacy HTML frontends are retired (see
`_retired-html-portal/`) and are no longer served or edited.

## How the Portal Talks to the AiCIV

```
Browser → portal_server.py → tmux (Claude Code session)
                            → JSONL session logs (chat history)
                            → subprocess checks (status)
```

The portal is **local to the container**. It reads tmux panes, parses JSONL logs, and injects text into the active Claude Code session. No external APIs needed — everything happens on localhost.

### 6. Install Image Context Safety Package

```bash
bash image-context-safety-package/install.sh --civ-root /home/aiciv
```

This installs a 4-layer defense against image context overflow crashes:
1. **CLAUDE.md rules** — instructs Claude to never accumulate images in context
2. **Cleanup script** — `tools/cleanup-context-images.sh` purges stale /tmp images
3. **Cron job** — runs cleanup every 30 minutes automatically
4. **Sub-agent pattern** — image analysis delegated to isolated agents with fresh context

Without this, screenshot-heavy workflows can crash sessions with "image exceeds dimension limit" errors.

---

## Source Tagging

Messages sent from the React portal are prefixed in tmux with `[portal-react]`.
This lets you distinguish portal input from Telegram or direct tmux in the session.

This lets you distinguish portal input from Telegram or direct tmux in the session.
