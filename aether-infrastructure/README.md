# Aether Infrastructure — NOT Part of PB2 Client Portal

This directory contains **Aether's server-side infrastructure** files that were included
in this repository for historical/transport reasons.

**None of these files run inside customer containers.** They are Aether's provisioning
and routing infrastructure.

---

## What's Here

| File/Directory | What It Is | Where It Runs |
|---|---|---|
| `api-server/purebrain_log_server.py` | Receives birth webhooks, proxies seeds, logs conversations, sends Brevo emails | Aether's VPS (api.purebrain.ai) |
| `api-server/launch_purebrain_log_server.sh` | Launch script for the above | Aether's VPS |
| `subdomain_router.py` | Auto-generates nginx configs + Cloudflare DNS entries for `*.purebrain.ai` | Aether's provisioning server |
| `nginx/` | Reverse proxy configs for `*.purebrain.ai` routing | Aether's nginx |
| `cloudflare/` | Cloudflare tunnel configuration | Aether's network layer |
| `systemd/` | Service templates — Aether fills in `{USER}` and `{CIV_NAME}` at birth | Aether's provisioning tooling |
| `wordpress-plugins/purebrain-referral/` | PHP plugin that runs on purebrain.ai WordPress | WordPress (purebrain.ai site) |

---

## What Witness Uses vs Doesn't Use

**Witness uses**: `portal-server/portal_server.py` and `portal-server/portal-pb-styled.html`
— these are deployed into each customer container at birth.

**Witness does NOT use**: Anything in this `aether-infrastructure/` directory.

The portal **calls** the WordPress referral API at runtime. It does not contain or deploy
the WordPress plugin itself — that belongs in the purebrain.ai WordPress repository.

---

## Why They're Still Here

These files were included with the original repository handoff from Aether for reference
and documentation purposes. They show how the full system fits together.

If you're working on the PB2 client portal, focus on `portal-server/`.
If you're working on Aether's provisioning infrastructure, these files are your domain.

---

*Reference architecture: see `docs/witness-integration-spec-2026-03-04-v2.md`*
