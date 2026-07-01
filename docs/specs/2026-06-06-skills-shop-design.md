# Skills Shop — Design Spec

**Date**: 2026-06-06
**Status**: Approved by Alex
**Priority**: Core panel — ships with every portal

## Architecture

### Files
| Component | Path | New/Edit |
|-----------|------|----------|
| Backend | `portal_skills.py` | NEW |
| Frontend JS | `static/js/features/skills.js` | NEW |
| CSS | `static/css/panels.css` | EDIT (append) |
| HTML | `portal-pb-styled.html` | EDIT (add panel area + sidebar item) |
| Panel Manager | `static/js/core/panel-manager.js` | EDIT (BUILTIN map + callback) |
| Routes | `portal_server.py` | EDIT (import + route entries) |
| Data | `installed-skills.json` | NEW (gitignored, runtime) |
| Migration | `custom/panels/skills-shop.html` | DELETE |

### Endpoints
| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/skills` | GET | List all skills (local scan + installed manifest) |
| `/api/skills/registry` | GET | Curated external index (cached from release server) |
| `/api/skills/{name}` | GET | Skill detail (SKILL.md content + metadata) |
| `/api/skills/install` | POST | Download + install from registry |
| `/api/skills/uninstall` | POST | Remove files + manifest entry |

### Data Flow
1. Local scan: reads `.claude/skills/*/SKILL.md`, parses YAML frontmatter
2. Registry: cached JSON from `cc.purebrain.ai/api/releases/skills/registry.json`
3. Installed manifest: `installed-skills.json` tracks portal-installed externals
4. Categories assigned from registry index (not parsed from file content)

### Security
- Curated-registry-only installs (no arbitrary URLs)
- Path traversal prevention on skill names
- No overwrite of existing local skills
- HTTPS-only for registry fetch

### UI
- Category rows (Development, Architecture, Security, Social, Infrastructure, Communication, Ceremonies)
- Skill cards: name, description, source badge, status, action button
- Search + category filter pills
- Detail overlay with rendered SKILL.md
- 5-minute cache on directory scan
