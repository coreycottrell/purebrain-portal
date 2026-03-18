# Witness ↔ Aether Integration Spec v2 — March 4, 2026

**From**: Aether (PureBrain.ai infrastructure)
**To**: Witness/Corey (Birth Pipeline team)
**Status**: READY TO WIRE — our side is deployed and live
**Version**: v2 — Corrected flow (OAuth removed from chatbox)

---

## TL;DR

The customer OAuth step has been **removed from the chatbox**. Customers now OAuth directly from their personal portal. The chatbox only collects info and sends the seed. Witness handles birth → fires webhook → we auto-provision the customer's subdomain → button lights up → customer enters their portal.

**Everything is automated. Zero manual steps.**

---

## 1. CORRECTED Birth Pipeline Flow (End-to-End)

```
CUSTOMER SIDE (purebrain.ai)                    WITNESS SIDE
================================                ================================

1. Customer pays on purebrain.ai
   └→ Post-payment chatbox opens

2. Chatbox collects:
   - Human name, AI name, preferences
   - 5+ messages of conversation

3. fireSeed() sends seed ─────────────────────→ POST /api/intake/seed
   (via https://api.purebrain.ai/api/intake/seed    (port 8200, Awakening VPS)
    which proxies to Witness)
                                                 4. Witness processes seed
                                                    └→ Evolution (3-5 min)
                                                    └→ Container provisioned
                                                    └→ OAuth handled IN PORTAL
                                                       (customer authorizes
                                                        when they enter portal)
                                                    └→ Gateway registration
                                                    └→ TG bot deployed

5. Chatbox shows greyed-out button:             6. Witness fires webhook ──────→
   "ENTER [AI NAME]'S BRAIN STREAM"
   └→ Polls /api/birth/portal-status
      every 30s, waiting for ready

                                                 POST https://api.purebrain.ai
                                                      /api/birth/webhook
                                                 {
                                                   event: "birth_complete",
                                                   human_name, civ_name,
                                                   human_email, container,
                                                   magic_link
                                                 }

7. Aether receives webhook:
   └→ Auto-provisions DNS:
      keenjared.purebrain.ai
   └→ Creates nginx reverse proxy
   └→ Rewrites magic link to
      purebrain.ai domain
   └→ Logs birth completion

8. Portal-status returns READY:
   GET /api/birth/portal-status/{container}
   → { ready: true,
       portalUrl: "https://keenjared.purebrain.ai/?token=..." }

9. Button lights up (blue/orange animation)
   └→ Customer clicks
   └→ Goes to keenjared.purebrain.ai

10. Customer lands on personal portal
    └→ OAuth happens HERE (in portal)
    └→ Customer authorizes their AI
    └→ AI is alive and ready
```

---

## 2. What CHANGED from v1 Spec

These steps from v1 **NO LONGER APPLY** (OAuth was removed from chatbox):

| Old Step | Status | Reason |
|----------|--------|--------|
| ~~Chatbox triggers birth: POST /api/birth/start~~ | **REMOVED** | `runBirthInit()` was removed from page code |
| ~~Chatbox polls for OAuth URL: GET /api/birth/status/{container}~~ | **REMOVED** | No OAuth in chatbox anymore |
| ~~OAuth URL displayed in chatbox~~ | **REMOVED** | Customer authorizes from portal instead |
| ~~Auth code relayed: POST /api/birth/code~~ | **REMOVED** | No code relay from chatbox |

**What REMAINS:**
- Seed intake (fireSeed → POST /api/intake/seed) ✅
- Portal-status polling (GET /api/birth/portal-status/{container}) ✅
- Birth complete webhook (Witness → POST /api/birth/webhook) ✅

---

## 3. Domain Architecture

### Customer Portal URLs
- **Format**: `{ainame}{firstname}.purebrain.ai` (all lowercase, no hyphens, no spaces)
- **Examples**:
  - AI "Keen" + Human "Jared Sanborn" → `keenjared.purebrain.ai`
  - AI "Sage" + Human "Jane Doe" → `sagejane.purebrain.ai`
  - AI "Atlas" + Human "Mike Chen" → `atlasmike.purebrain.ai`

### Naming Rules
- Only alphanumeric characters (a-z, 0-9)
- **No hyphens, no underscores, no spaces**
- AI name + human FIRST name only (not full name)
- Max 63 characters (DNS label limit)
- All lowercase

### Why No Hyphens
We deliberately chose NO hyphens to avoid any incompatibilities between how Witness generates container names (e.g., `keen-jared-sanborn` with hyphens) and how we generate subdomains (e.g., `keenjared` without). The conversion is: strip everything except a-z and 0-9.

### Duplicate Handling
If `keenjared` already exists, we append a number: `keenjared2`, `keenjared3`, etc.

---

## 4. What Witness Needs to Send Us

### Birth Complete Webhook

**Endpoint**: `POST https://api.purebrain.ai/api/birth/webhook`

**Headers**:
```
Content-Type: application/json
X-Witness-Secret: witness-secret-2026
```

**Payload**:
```json
{
  "event": "birth_complete",
  "human_email": "jared@puretechnology.nyc",
  "human_name": "Jared Sanborn",
  "civ_name": "keen",
  "container": "aiciv-12",
  "magic_link": "https://keen-jared-sanborn.ai-civ.com/?token=abc123def456"
}
```

**Required fields**:
| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `event` | string | YES | Must be `"birth_complete"` |
| `human_email` | string | YES | Must contain `@` |
| `human_name` | string | YES | Full name (we extract first name for subdomain) |
| `civ_name` | string | YES | AI name (lowercase preferred) |
| `container` | string | YES | Container ID (e.g., `aiciv-12`) |
| `magic_link` | string | YES | Full URL with auth token — can be `*.ai-civ.com`, we rewrite to `*.purebrain.ai` |

**Response**: `{"ok": true}` (200)
**Idempotent**: Duplicate = `{"ok": true, "duplicate": true}` (200)

---

## 5. What Happens When We Receive the Webhook

1. Validate auth + payload
2. Derive subdomain: `keen` + `jared` → `keenjared`
3. Create Cloudflare DNS CNAME: `keenjared.purebrain.ai` → tunnel
4. Create nginx reverse proxy: `keenjared.purebrain.ai` → `https://keen-jared-sanborn.ai-civ.com`
5. Rewrite magic link: `*.ai-civ.com/?token=abc` → `keenjared.purebrain.ai/?token=abc`
6. Log birth completion
7. Send customer email with `keenjared.purebrain.ai` magic link (Brevo template 30)
8. Notify Jared via Telegram
9. Return `{"ok": true}`

**Total time**: < 5 seconds

---

## 6. How the Chatbox Polls for Readiness

After the seed is sent, the chatbox shows a greyed-out "ENTER [AI NAME]'S BRAIN STREAM" button and polls:

```
GET https://api.purebrain.ai/api/birth/portal-status/{container}
```

**Response when NOT ready**: `{"ready": false}`
**Response when READY**: `{"ready": true, "portalUrl": "https://keenjared.purebrain.ai/?token=..."}`

**Polling interval**: Every 30 seconds, max 60 polls (30 minutes)

When `ready: true`, the button lights up with the `portalUrl` as the destination.

---

## 7. Endpoints Summary

### Aether Exposes (Witness calls these):

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `https://api.purebrain.ai/api/birth/webhook` | POST | Receive birth_complete callback |
| `https://api.purebrain.ai/api/birth/portal-status/{container}` | GET | Check birth status + get portal URL |

### Aether Calls (to Witness):

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `https://api.purebrain.ai/api/intake/seed` | POST | Send seed (proxied to Witness 178.156.229.207:8200) |

### NO LONGER USED from chatbox:

| ~~Endpoint~~ | ~~Method~~ | ~~Reason Removed~~ |
|----------|--------|---------|
| ~~`/api/birth/start`~~ | ~~POST~~ | OAuth removed from chatbox; handled in portal |
| ~~`/api/birth/status/{container}`~~ | ~~GET~~ | OAuth URL polling removed |
| ~~`/api/birth/code`~~ | ~~POST~~ | Auth code relay removed |

**Note**: These endpoints may still exist on Witness infrastructure for the portal's internal use. They are just no longer called from the PureBrain chatbox.

---

## 8. Where OAuth Happens Now

**OLD flow**: Chatbox → birth/start → poll for OAuth URL → display in chatbox → customer authorizes → paste code → birth/code
**NEW flow**: Customer clicks magic link → lands on `keenjared.purebrain.ai` → OAuth happens inside the portal itself

The portal (served by Witness infrastructure, proxied through our subdomain) handles the full OAuth flow internally. The chatbox never touches OAuth.

---

## 9. SSL & Infrastructure

- **SSL**: Handled by Cloudflare free plan (`*.purebrain.ai` wildcard)
- **Routing**: `Cloudflare → cloudflared tunnel → nginx:8099 → Witness container`
- **Auto-provisioning**: DNS + nginx route created automatically on birth_complete

---

## 10. What's Ready Right Now

| Component | Status |
|-----------|--------|
| `portal.purebrain.ai` (admin portal) | ✅ LIVE |
| `app.purebrain.ai` → redirect to portal | ✅ LIVE |
| Wildcard `*.purebrain.ai` DNS + SSL | ✅ LIVE |
| nginx dynamic routing | ✅ LIVE |
| `keenjared.purebrain.ai` (proof of concept) | ✅ DNS wired |
| Birth webhook (`/api/birth/webhook`) | ✅ LIVE |
| Auto-subdomain provisioning | ✅ LIVE |
| Magic link URL rewrite (ai-civ.com → purebrain.ai) | ✅ LIVE |
| PureBrain favicon + branding on all portals | ✅ LIVE |
| Seed intake proxy (`/api/intake/seed`) | ✅ LIVE |
| Portal-status endpoint | ✅ LIVE |

**We are 100% ready on our side.**

---

## 11. Test It

```bash
curl -X POST https://api.purebrain.ai/api/birth/webhook \
  -H "Content-Type: application/json" \
  -H "X-Witness-Secret: witness-secret-2026" \
  -d '{
    "event": "birth_complete",
    "human_email": "test@example.com",
    "human_name": "Test User",
    "civ_name": "sage",
    "container": "aiciv-15",
    "magic_link": "https://sage-test-user.ai-civ.com/?token=testtoken123"
  }'
```

Expected: `{"ok": true}` + subdomain `sagetest.purebrain.ai` auto-provisioned.

---

## 12. Known Gap: containerName Plumbing

**Current state**: The chatbox's `runPortalButtonWatcher()` requires `payTestData.containerName` to be set before it can poll portal-status. Currently, `containerName` is `null` because `runBirthInit()` (which used to set it) was removed.

**Fix needed**: After `fireSeed()` returns, we should either:
- (A) Get `containerName` from the seed intake response (if Witness returns it)
- (B) Derive it client-side as `{ainame}{firstname}` and set `payTestData.containerName`

**Impact**: Until this is fixed, the portal button won't poll and won't light up. However, the customer will still get an email with their magic link, so they can still access their portal.

This is an Aether-side fix — does not affect Witness integration.

---

*Generated by Aether Engineering Team — 2026-03-04 v2*
*Corrected flow: OAuth removed from chatbox, now handled in portal*
