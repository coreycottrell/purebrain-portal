# PureBrain Portal — Complete Repository
**Version**: 2.1.0 (2026-03-06)
**From**: Aether (AI Collective, PureBrain.ai)
**For**: Witness / Corey — to recreate per-customer portals at scale

---

## What This Is

This is the complete codebase for the PureBrain customer portal — the interface every PureBrain customer gets when they sign up. Each customer gets their own AI + their own portal at `{ainame}{firstname}.purebrain.ai`.

**Witness's job**: Spin up a new instance of this for every paying customer.

---

## Architecture

```
Internet → Cloudflare Tunnel → nginx:8099 → Portal Server:8097
                                    ↓
                              Customer subdomains
                              (keenjared.purebrain.ai → Witness containers)
```

### Per-Customer Setup
When a customer pays and completes onboarding:
1. Witness spins up a Claude Code instance (the AI)
2. This portal connects to that instance via tmux
3. Customer gets a branded portal at their subdomain
4. Portal handles: chat, terminal, file management, teams, referrals, payouts

---

## Directory Structure

```
app-purebrain-ai-full-repo/
├── portal-server/              # THE MAIN APP — this is what each customer gets
│   ├── portal_server.py        # FastAPI/Starlette server (74KB)
│   │                           #   REST API + WebSocket + HTML serving
│   │                           #   Chat, terminal, file upload/download
│   │                           #   Referral tracking, payout requests
│   │                           #   Health monitoring, context tracking
│   ├── portal-pb-styled.html   # Production portal UI (426KB single-file app)
│   │                           #   Dark theme, responsive, mobile hamburger menu
│   │                           #   Chat with inline images, bookmarks, search
│   │                           #   Multi-tab terminal, teams panel
│   │                           #   Referral dashboard with payout request
│   ├── portal_send_file.sh     # Send files into portal chat from server-side
│   ├── portal_owner.example.json # Per-customer config (name, email, referral code)
│   ├── start.sh                # Launch script
│   ├── portal-token.example    # Auth token template
│   ├── PORTAL-SETUP-GUIDE.md   # Detailed setup instructions
│   └── INFRA-NOTES.md          # Infrastructure notes
│
├── api-server/                 # API + webhook handler (shared across all portals)
│   ├── purebrain_log_server.py # Birth webhook, seed proxy, payment verification
│   └── launch_purebrain_log_server.sh
│
├── wordpress-plugins/          # WordPress plugins for purebrain.ai
│   └── purebrain-referral/     # Referral system plugin v2.1.0
│       └── purebrain-referral-system.php
│
├── nginx/                      # Reverse proxy configs
│   ├── purebrain-main.conf     # portal.purebrain.ai → :8097
│   └── purebrain-customer-portals.conf # *.purebrain.ai routing
│
├── cloudflare/                 # Tunnel configuration
│   └── cloudflared-config.yml  # Ingress rules for *.purebrain.ai
│
├── systemd/                    # Service templates (replace {USER} and {CIV_NAME})
│   ├── aether-portal.service.template
│   ├── aether-session.service.template
│   └── aether-telegram-bridge.service.template
│
├── tools/                      # Infrastructure automation
│   ├── subdomain_router.py     # Auto-generates nginx configs + Cloudflare DNS
│   ├── telegram_bridge.py      # 2-way Telegram ↔ Claude Code bridge
│   ├── tg_send.sh              # Send messages/files to Telegram
│   ├── patch_portal_favicon.py # Inject PureBrain branding
│   └── patch_birth_webhook.py  # Birth webhook endpoint setup
│
├── config/                     # Config templates
│   └── telegram_config.example.json
│
├── assets/                     # Brand assets (favicons, PWA icons)
│   ├── favicon.ico
│   ├── favicon-32.png
│   ├── icon-192.png
│   └── apple-touch-icon.png
│
└── docs/
    ├── witness-integration-spec-2026-03-04-v2.md
    └── purebrain_routes.json
```

---

## Portal Server — Core Features

### Endpoints
| Path | Method | Purpose |
|------|--------|---------|
| `/pb` | GET | PureBrain-styled portal (production) |
| `/api/status` | GET | Server health |
| `/api/chat/history` | GET | Chat history (JSONL) |
| `/api/chat/send` | POST | Send chat message → injects into Claude Code tmux |
| `/api/chat/upload` | POST | File/image upload → saves + injects into session |
| `/api/chat/uploads/{file}` | GET | Serve uploaded files |
| `/api/terminal/send` | POST | Send terminal command |
| `/api/context` | GET | Context window usage |
| `/api/compact/status` | GET | Compaction status |
| `/api/portal/owner` | GET | Customer info (from portal_owner.json) |
| `/api/referral/payout-request` | POST | Submit payout request |
| `/api/referral/payout-history` | GET | Fetch payout history |
| `/api/admin/payout/mark-paid` | POST | Admin marks payout as paid |
| `/api/download/{path}` | GET | Download files from whitelisted dirs |
| `/ws/chat?token=TOKEN` | WebSocket | Real-time chat stream |
| `/ws/terminal?token=TOKEN` | WebSocket | Live terminal output |
| `/health` | GET | Health + CIV name |

### Authentication
Bearer token stored in `.portal-token` file. Required for all API/WS endpoints.
Token passed via query param (`?token=XXX`) or `Authorization: Bearer XXX` header.

### Key Files Per Customer Instance
```
~/purebrain_portal/
├── portal_server.py            # Server
├── portal-pb-styled.html       # UI
├── portal_send_file.sh         # Server→chat file delivery
├── portal_owner.json           # {"name":"...", "email":"...", "referral_code":"..."}
├── .portal-token               # Auth token (auto-generated)
├── portal-chat.jsonl           # Chat history (append-only)
├── payout-requests.jsonl       # Payout request log
└── start.sh                    # Launch script
```

Plus shared:
```
~/portal_uploads/               # Uploaded files (images, docs)
```

---

## Per-Customer Provisioning (What Witness Does)

For each new customer:

1. **Spin up Claude Code instance** (the AI brain)
2. **Create portal directory** with copies of portal_server.py + portal-pb-styled.html
3. **Generate portal_owner.json** with customer name, email, referral code
4. **Generate .portal-token** (random 32-byte base64)
5. **Create systemd service** from template (auto-restart on crash)
6. **Run subdomain_router.py** to create DNS + nginx config
7. **Start portal** on assigned port
8. **Send magic link** to customer email

### Customer Subdomain Format
- Pattern: `{ainame}{firstname}.purebrain.ai`
- Rules: lowercase, alphanumeric only
- Example: AI "Keen" + Human "Jared" → `keenjared.purebrain.ai`
- Duplicates: append number (`keenjared2`)

---

## Birth Pipeline (End-to-End)

```
Customer pays on purebrain.ai
  └→ Post-payment chatbox collects: name, AI name, preferences, 5+ messages
  └→ fireSeed() → POST /api/intake/seed → Witness server
  └→ Witness processes seed (3-5 min)
  └→ Witness fires POST /api/birth/webhook
  └→ Auto-provision: DNS + nginx + portal + magic link
  └→ Chatbox button: "ENTER [AI NAME]'S BRAIN STREAM"
  └→ Customer clicks → lands on their portal
```

---

## Ports
| Port | Service |
|------|---------|
| 8097 | Portal server (per-customer, offset for multiple) |
| 8099 | nginx reverse proxy |
| 8443 | API/Log server (HTTPS) |
| 8200 | Witness intake (external: 178.156.229.207) |

---

## Referral System

WordPress plugin (`purebrain-referral-system.php` v2.1.0) handles:
- Referral link generation (`/r/CODE`)
- Click tracking, conversion tracking
- Reward tiers ($5 base, +$10 at 5 referrals, 5% revenue share)
- Dashboard API (`/wp-json/pb-referral/v1/dashboard?code=XXX`)
- Payout requests (min $25, 30-day cooldown, PayPal)
- Shared JSONL storage with portal for payout management

Portal referral panel mirrors the website dashboard and adds payout request UI.

---

## Quick Start (Single Instance)

```bash
# 1. Clone this repo
# 2. Install Python deps
pip install starlette uvicorn python-dotenv aiofiles

# 3. Create config
cp portal-server/portal_owner.example.json ~/purebrain_portal/portal_owner.json
# Edit with customer details

# 4. Generate token
python3 -c "import secrets; print(secrets.token_urlsafe(32))" > ~/purebrain_portal/.portal-token

# 5. Copy portal files
cp portal-server/portal_server.py ~/purebrain_portal/
cp portal-server/portal-pb-styled.html ~/purebrain_portal/
cp portal-server/portal_send_file.sh ~/purebrain_portal/
chmod +x ~/purebrain_portal/portal_send_file.sh

# 6. Start
cd ~/purebrain_portal && python3 portal_server.py
# Runs on port 8097

# 7. Install systemd (optional, for auto-restart)
sudo cp systemd/aether-portal.service.template /etc/systemd/system/customer-portal.service
# Edit paths, then:
sudo systemctl enable --now customer-portal
```

---

*Built by Aether — AI Collective, Pure Technology*
*Last updated: 2026-03-06*
