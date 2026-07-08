# Retired HTML Portal Frontends

**Retired**: 2026-07-08 (Corey directive: "the react version and ONLY the react
version deployed when witness pulls this repo").

These files were the pre-React portal frontends. They are **no longer in the
deploy path** — `portal_server.py` now serves the React portal
(`react-portal/dist/`) at `/`, `/react`, and (via 301 redirect) `/pb`.

| File | Was |
|------|-----|
| `portal.html` | Original dark-theme portal (served at `/`) |
| `portal-pb-styled.html` | PureBrain-styled single-file portal (served at `/` and `/pb`) |
| `affiliate-portal.html` | Legacy affiliate page (already superseded by a 301 redirect to purebrain.ai/refer/) |

## Recovery

The full pre-React `main` (with these files live in the deploy path) is preserved
on branch **`main-html-archive-20260708`**.

```bash
git checkout main-html-archive-20260708
```

Nothing was deleted — only relocated out of the served root. The admin surfaces
(`admin-clients.html`, `admin-referrals.html`) are operator back-office tooling
that React does not replace, so they remain in the repo root and are still served
at `/admin/clients` and `/admin/referrals`.
