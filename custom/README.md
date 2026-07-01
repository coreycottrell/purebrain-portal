# custom/ -- Per-CIV Customization Directory

This directory holds all per-deployment customizations. Files here are
auto-loaded by the portal's customization shim and survive upstream updates.

## Quick Start

1. `cp config.json.template config.json` -- override portal globals
2. `cp routes.py.template routes.py` -- add custom API endpoints
3. `cp startup.py.template startup.py` -- CC Chat Bridge (auto-detects CIV identity)
4. Copy `panels/SAMPLE-PANEL.html.template` to `panels/my-panel.html` -- add a sidebar panel
5. Restart the portal

## What goes where

| File | Purpose |
|------|---------|
| `config.json` | Override MAX_TOKENS, PORTAL_VERSION, etc. |
| `routes.py` | Custom `/api/custom/*` endpoints |
| `startup.py` | CC Chat Bridge (auto-detects CIV from `~/.aiciv-identity.json`) |
| `panels/*.html` | Custom sidebar panels (one file per panel) |

## Full documentation

See `docs/ADR-001-customization-architecture.md` for architecture details.

**Note:** `*.template` files are tracked by git. Runtime files (`config.json`,
`routes.py`, `panels/*.html`) are gitignored so each CIV can customize freely.
