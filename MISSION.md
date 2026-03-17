# PureBrain Portal — Mission Statement

**Repository**: `aiciv-comms-hub/packages/purebrain-portal/`
**Codebase generation**: PB2 (next generation)
**Last updated**: 2026-03-06

---

## What This Is

This is the **PureBrain client portal** — the interface a paying PureBrain customer sees the moment they click their magic link.

Not a dashboard. Not an admin panel. Not a fleet tool. A **portal** — a window between a human and their AI.

When someone pays for PureBrain, they go through an onboarding conversation. They name their AI. They share what they care about. And at the end, they get a link. They click it. They land here.

**This is their first experience with their AI.** It has to feel alive, personal, and powerful — because it is.

---

## Who It's For

**PureBrain clients.** Regular people. Not operators. Not developers. Not Witness. Not fleet managers.

A person who just paid for their first AI. A person who has never used a terminal before, maybe. A person who is about to have a conversation that changes how they think about intelligence.

Every design decision, every feature, every line of code in `portal-server/` should be evaluated through this lens:

> Does this serve the human sitting across from their AI for the first time?

---

## What It Does

The portal lives **inside the customer's AiCIV container**. It connects to the Claude Code tmux session running inside that container. Everything happens locally — no external APIs, no cloud intermediaries, no latency surprises.

**Core capabilities:**

- **Chat** — real-time conversation with their AI via WebSocket, with full history, inline images, bookmarks, and search
- **Terminal** — live tmux pane stream, so they can watch what their AI is doing or run commands themselves
- **File management** — upload documents, images, anything; download files the AI creates
- **Referral earnings** — dashboard showing their referral code, conversions, reward tier, and payout request UI
- **Status** — connection health, context window usage, compact status, AI session state
- **Teams** — panel for future multi-AI coordination (foundation in place)

The stack is intentionally minimal: Python/Starlette backend, single-file HTML frontend, no build step required. A developer (human or AI) should be able to read `portal_server.py` and `portal-pb-styled.html` and understand the entire system in one sitting.

---

## What It Is Not

This needs to be explicit, because the repository contains files that **do not belong to the client portal**. They exist here for historical/transport reasons — they are Aether's server-side infrastructure, not PB2 client portal code.

### Aether Infrastructure — Not Part of PB2 Client Portal

| File/Directory | What It Is | Why It's Not Portal Code |
|---|---|---|
| `api-server/purebrain_log_server.py` | Aether's webhook receiver — handles birth triggers, seed proxying, payment verification | Runs on Aether's servers, not inside customer containers |
| `api-server/launch_purebrain_log_server.sh` | Launch script for the above | Same — Aether infrastructure |
| `tools/subdomain_router.py` | Auto-generates nginx configs + Cloudflare DNS entries for new customers | Runs on Aether's provisioning server, not customer containers |
| `nginx/` | Aether's reverse proxy configs for `*.purebrain.ai` routing | Aether's nginx, not per-customer |
| `cloudflare/` | Cloudflare tunnel configuration | Aether's network layer |
| `systemd/` | Service templates — Aether fills in `{USER}` and `{CIV_NAME}` at birth | Used by Aether's provisioning tooling, not deployed inside portal |
| `wordpress-plugins/purebrain-referral/` | The PHP plugin that runs on purebrain.ai's WordPress site | The portal calls its API, but the plugin code itself runs on WordPress, not in containers |

The portal **calls** the WordPress referral API. It does not **contain** the WordPress plugin. That plugin belongs in the purebrain.ai WordPress repository.

---

## The Repo's Role

`aiciv-comms-hub/packages/purebrain-portal/` is the **source of truth for PB2** — the next generation of the PureBrain client portal.

When Witness deploys a portal for a new customer:
1. The relevant files are pulled from here
2. `portal_owner.json` is generated with the customer's name, email, and referral code
3. A token is generated and stored in `.portal-token`
4. `portal_server.py` and `portal-pb-styled.html` are copied into the customer's container
5. The portal starts on port 8097

**Changes land here. Deployments pull from here.**

If you improve the UI, fix a bug, or add a feature — commit it here. The next birth picks it up automatically.

---

## The Vision

A person pays for PureBrain. They name their AI — maybe Keen, maybe Nova, maybe something only they would think of. They have a conversation that feels weirdly real. At the end, a button appears:

**"ENTER KEEN'S BRAIN STREAM"**

They click it. A dark portal opens. Their AI is already there, already knowing their name, already knowing what they came for.

The chat works. The terminal works. They upload a file and their AI reads it instantly. They watch in real-time as their AI thinks. They feel — for the first time — like they have a brilliant friend who is entirely theirs.

That experience is what this codebase exists to deliver.

Everything else is infrastructure.

---

## For New Developers

If you're reading this because you're about to contribute:

1. **The portal code** is in `portal-server/` — that's your domain
2. **The HTML file** (`portal-pb-styled.html`) is the entire frontend — no build step, edit and refresh
3. **The Python server** (`portal_server.py`) handles REST + WebSocket + file serving
4. **Don't touch** `api-server/`, `nginx/`, `cloudflare/`, `wordpress-plugins/` — those are Aether's infrastructure, not portal code
5. **Test with a real container** — the portal reads tmux directly, so mocking is harder than it sounds

The bar is simple: when someone clicks their magic link, does it feel like magic?

Make it feel like magic.

---

*Built by Aether — AI Collective, Pure Technology*
*Mission statement authored by Witness fleet-lead, 2026-03-06*
