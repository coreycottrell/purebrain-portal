# PureBrain Portal — Emergency Recovery Guide

## What This Repo Contains
- `portal_server.py` — The portal server (7600+ lines, serves HTML + React, handles auth, evolution, chat)
- `portal-pb-styled.html` — The styled HTML portal frontend
- `react-portal/` — React portal dist (if present)

## Current State (as of 2026-03-27)
- Auth flow: v2 state machine with screen detection, smart dismissals, retry logic, pre-warm
- Evolution: double Ctrl-C (same pane, no new window)
- Pre-warm endpoint: POST /api/auth/prewarm

## Emergency: Born CIV Portal Not Working

### Auth stuck / spinning forever
1. SSH into the container: `ssh -p PORT aiciv@37.27.237.109`
2. Check tmux: `tmux list-sessions`
3. Check what's on screen: `tmux capture-pane -t SESSION:0 -p -J -S -20`
4. If CSAT survey: send Escape (`tmux send-keys -t SESSION Escape`)
5. If login menu stuck: send Enter (`tmux send-keys -t SESSION Enter`)
6. If Claude dead: restart (`bash /home/aiciv/from-witness/restart-self.sh`)

### Portal not starting
1. Check if portal_server.py is running: `pgrep -af portal`
2. Check logs: `cat /tmp/portal-server.log`
3. Restart: `cd ~/purebrain_portal && nohup python3 portal_server.py > /tmp/portal-server.log 2>&1 &`

### Evolution not firing after auth
1. Check .first-boot-fired file: `cat ~/.first-boot-fired`
2. Check evolution status: `curl -s http://localhost:8097/api/evolution/status`
3. Manual trigger: `curl -s -X POST http://localhost:8097/api/evolution/first-boot -H "Authorization: Bearer $(cat ~/purebrain_portal/.portal-token)"`

### Portal 502 from outside
1. Check Caddy on fleet host: `grep CONTAINER /etc/caddy/Caddyfile`
2. Verify port matches: `docker port CONTAINER 8097`
3. Fix if mismatched: edit Caddyfile, `caddy reload`

## Key Endpoints
| Endpoint | Method | Purpose |
|----------|--------|---------|
| /api/auth/start | POST | Start OAuth (state machine v2) |
| /api/auth/url | GET | Poll for OAuth URL |
| /api/auth/code | POST | Submit OAuth code |
| /api/auth/status | GET | Check auth state |
| /api/auth/prewarm | POST | Pre-warm Claude for faster auth |
| /api/evolution/status | GET | Check evolution state |
| /api/evolution/first-boot | POST | Trigger evolution |
| /health | GET | Health check |

## How to Redeploy This Repo to a Container
```bash
# From fleet host:
docker exec CONTAINER bash -c 'cd /home/aiciv && rm -rf purebrain_portal && git clone --depth 1 git@github.com:coreycottrell/purebrain-portal.git purebrain_portal'
# Restart portal:
docker exec -u aiciv CONTAINER bash -c 'cd ~/purebrain_portal && nohup python3 portal_server.py > /tmp/portal-server.log 2>&1 &'
```
