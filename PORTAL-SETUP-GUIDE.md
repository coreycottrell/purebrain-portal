# AiCIV Portal — Setup Guide

**For**: Aether's team (or any AiCIV operator)
**Time to deploy**: ~15 minutes

---

## What's in the Package

| File | Purpose |
|------|---------|
| `portal_server.py` | Backend server (Python/Starlette, ~470 lines) |
| `portal.html` | Original dark theme portal (300 lines, zero deps) |
| `portal-pb-styled.html` | PureBrain-styled portal (980 lines, zero deps) |
| `react-portal/` | React + Vite version (20 components, full PB frontend) |
| `start.sh` | One-liner startup script |
| `.portal-token` | You create this — your auth bearer token |

## Prerequisites

- Python 3.10+ with `pip`
- Node.js 18+ (only if using React version)
- A running AiCIV container with tmux session
- Reverse proxy for TLS (Caddy recommended)

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
python3 portal_server.py
# Runs on port 8097 by default
```

### 5. Access the portal

- Original: `http://localhost:8097/`
- PureBrain-styled: `http://localhost:8097/pb`
- React version: `http://localhost:8097/react`

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
| GET | `/` | No | Original HTML portal |
| GET | `/pb` | No | PureBrain-styled portal |
| GET | `/react` | No | React portal |
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

### HTML versions
Just edit `portal.html` or `portal-pb-styled.html` directly. Refresh browser. No build step.

### React version
```bash
cd react-portal
# Edit src/components/*.jsx
npm run build    # Produces dist/ served at /react
```

The portal server auto-serves from `react-portal/dist/`. No server restart needed after rebuild.

## How the Portal Talks to the AiCIV

```
Browser → portal_server.py → tmux (Claude Code session)
                            → JSONL session logs (chat history)
                            → subprocess checks (status)
```

The portal is **local to the container**. It reads tmux panes, parses JSONL logs, and injects text into the active Claude Code session. No external APIs needed — everything happens on localhost.

## Source Tagging

Messages sent from the portal are prefixed in tmux:
- `[portal]` — from HTML versions
- `[portal-react]` — from React version

This lets you distinguish portal input from Telegram or direct tmux in the session.
