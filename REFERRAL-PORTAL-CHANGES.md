# Referral Panel Changes for portal-pb-styled.html

**Status**: Pending — do NOT modify portal-pb-styled.html until the other agent working on it is done.
**Date**: 2026-03-12
**Context**: Referral system migrated from dead WP endpoints to SQLite-backed portal server.

---

## What Needs Changing

The referral panel is around lines 3735–3804 in `portal-pb-styled.html`. Search for `Refer & Earn`.

### 1. Update API Base URL

Find any reference to `https://purebrain.ai/wp-json/pb-referral/v1` and replace with `/api/referral`.

The portal server now hosts all referral endpoints locally. The page already proxies through the portal, so relative paths work perfectly.

### 2. Remove X-WP-Nonce Headers

Remove all `'X-WP-Nonce': '665280eb4e'` headers from fetch calls. The new SQLite endpoints do not require nonces.

### 3. Update Dashboard Fetch

Old:
```javascript
fetch('https://purebrain.ai/wp-json/pb-referral/v1/dashboard?code=' + code)
```

New:
```javascript
fetch('/api/referral/dashboard?code=' + code)
```

### 4. Update Register Fetch

Old:
```javascript
fetch('https://purebrain.ai/wp-json/pb-referral/v1/register', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-WP-Nonce': '665280eb4e' },
    body: JSON.stringify({ name, email })
})
```

New:
```javascript
fetch('/api/referral/register', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, email })
})
```

### 5. Update Lookup Fetch

Old:
```javascript
fetch('https://purebrain.ai/wp-json/pb-referral/v1/lookup?email=' + email)
```

New:
```javascript
fetch('/api/referral/code/' + encodeURIComponent(email))
```

Note: The new endpoint is `GET /api/referral/code/{email}` (path param, not query param).
Response shape: `{ referral_code, referral_link }`

### 6. Update PayPal Email Save

Old:
```javascript
fetch('https://purebrain.ai/wp-json/pb-referral/v1/paypal-email', { ... })
```

New:
```javascript
fetch('/api/referral/paypal-email', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email: userEmail, paypal_email: paypalEmail })
})
```

### 7. New Response Shape for /dashboard

The SQLite dashboard returns:
```json
{
  "referral_code": "PB-ABCD",
  "referral_link": "https://purebrain.ai/refer/?code=PB-ABCD",
  "email": "user@example.com",
  "name": "Jane Smith",
  "paypal_email": "jane@paypal.com",
  "total_referrals": 3,
  "completed": 2,
  "pending": 1,
  "earnings": 10.00,
  "total_clicks": 47,
  "history": [
    {
      "referred_name": "Bob",
      "referred_email": "bob@example.com",
      "status": "completed",
      "created_at": "2026-03-10T...",
      "earnings": 5.0
    }
  ],
  "reward_tiers": [
    { "label": "Per Completed Referral", "reward": "$5 cash" },
    { "label": "10+ Referrals", "reward": "Bonus $50" },
    { "label": "25+ Referrals", "reward": "Bonus $150" }
  ]
}
```

Key difference from old WP response:
- `total_clicks` is now a top-level field (not inside history items as `click_count`)
- `earnings` is a float, not a string
- `history[].earnings` replaces old `history[].earnings` — same name, now always a float

### 8. New Register Response Shape

```json
{
  "ok": true,
  "referral_code": "PB-ABCD",
  "referral_link": "https://purebrain.ai/refer/?code=PB-ABCD",
  "existing": false,
  "message": "Registration successful!"
}
```

Old response used `data.code` to signal errors (WP REST error format). New: check `data.error` instead of `data.code`.

---

## New Endpoints Available (not in old WP plugin)

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `POST /api/referral/track` | POST | Log a referral link click |
| `POST /api/referral/complete` | POST | Mark referral as completed (for integration) |
| `GET /api/referral/leaderboard` | GET | Top referrers leaderboard |

---

## No Auth Required on Referral Endpoints

The new endpoints do NOT require the portal Bearer token. They are public-facing (matching the behavior of the old WP REST API). This makes them work on both CF Pages (no auth) and in the portal.

---

## Files Changed

- `/home/jared/purebrain_portal/portal_server.py` — 6 new SQLite endpoints replacing 3 dead proxies
- `/home/jared/purebrain_portal/referrals.db` — auto-created on server startup
- `/home/jared/projects/AI-CIV/aether/exports/cf-pages-deploy/refer/index.html` — endpoints updated
