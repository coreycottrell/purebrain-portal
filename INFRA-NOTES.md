# Infrastructure Notes - PureBrain Portal Setup
# Written by: infra-lead (2026-03-01)

## Port Decision: witness.ai-civ.com

### How It Works (Finalized Architecture)

```
Browser (HTTPS 443)
    ↓
Caddy (on Hetzner host 37.27.237.109)
    ↓ reverse_proxy localhost:8103
Docker port mapping: host:8103 → container:8097
    ↓
Your mini web server (inside witness container)
    ↑ listens on port 8097
```

### KEY: Web-Lead Must Listen on Port 8097

**DO NOT use port 8080** — it's not mapped to the host.

The Docker port mapping for the witness container is:
- Container port `8097` → Host port `8103`

Your mini web server inside the container MUST bind to:
```
0.0.0.0:8097
```

Then Caddy on the host proxies:
```
witness.ai-civ.com → localhost:8103 → container:8097 (your server)
```

### Why This Approach

- No Docker restart needed (which would kill the active Claude session)
- Uses existing port mapping defined at container creation time
- Stable: uses Docker's port mapping, not container IP (which can change)

## Caddy Status (as of 2026-03-01)

- **Installed**: Caddy 2.11.1 via official apt repo
- **Configured**: `/etc/caddy/Caddyfile` on Hetzner host
- **Ports open**: UFW allows 80/tcp and 443/tcp
- **Status**: `active (running)` — systemd enabled, auto-starts on reboot
- **HTTPS**: Caddy will auto-provision Let's Encrypt cert once DNS propagates

## Caddyfile (current)

```
witness.ai-civ.com {
    reverse_proxy localhost:8103
}
```

Location on Hetzner host: `/etc/caddy/Caddyfile`

## DNS Status

- A record `witness.ai-civ.com` → `37.27.237.109` — being set up (not yet verified)
- Once DNS propagates, Caddy will automatically obtain TLS certificate
- Until then, HTTPS won't work but the reverse proxy config is ready

## To Verify When Mini Server Is Running

From awakening VPS:
```bash
ssh root@37.27.237.109 'curl -s http://localhost:8103/'
```

From anywhere (once DNS propagates):
```bash
curl https://witness.ai-civ.com/
```

## Reload Caddy (if Caddyfile changes)

```bash
ssh root@178.156.229.207 "ssh root@37.27.237.109 'systemctl reload caddy'"
```
