# ADR-002: Custom Directory Git Strategy for PureBrain Portal

**Status:** Proposed
**Date:** 2026-03-23
**Author:** architect-agent
**Supersedes:** None (extends ADR-001)

## Context

ADR-001 established the overlay architecture: a `custom/` directory where CIVs place their customizations (routes, config, panels), auto-loaded by a shim in `portal_server.py`. The repo (github.com/coreycottrell/purebrain-portal) is the source of truth -- people clone/pull it to update their portal.

The question: how should the repo handle the `custom/` directory so that (a) git pull never overwrites CIV customizations, (b) new CIVs immediately understand the expected format, and (c) the repo remains the authoritative source for updates?

## Decision Drivers

1. **git pull must be safe.** A CIV operator running `git pull` must never lose their custom routes, config, or panels.
2. **New CIVs must understand the format immediately.** Prose documentation alone is insufficient -- people learn from working examples.
3. **Templates must stay current.** If the shim's expected format changes, the example must change in the same commit.
4. **Consistency.** The repo already uses `.template` suffix for `SAMPLE-PANEL.html.template`.
5. **Simplicity.** Non-technical operators must understand the setup.

## Considered Options

### Option A: Template files with `.template` suffix (CHOSEN)

Ship `routes.py.template`, `config.json.template`, and `SAMPLE-PANEL.html.template` in the repo. CIVs copy them, remove the suffix, and modify. The actual runtime files (`.py`, `.json`, `.html`) remain gitignored.

### Option B: Ship a base `routes.py` with empty routes list

Include a real `custom/routes.py` that the shim loads, containing an empty `routes = []`. CIVs extend it.

**Rejected.** `git pull` would overwrite or conflict with the CIV's modified `routes.py`. This is exactly the problem the overlay architecture was designed to eliminate. Operators would need to `git stash`/merge on every update -- the same pain as the old monolithic approach.

### Option C: No files, documentation only

Don't include any custom files. Document the format in ADR-001 and let CIVs create files from scratch.

**Rejected.** ADR-001 is 489 lines. Nobody will reconstruct the correct format from prose on first try. A working template is worth more than a thousand words of documentation.

### Option D: `.example.py` naming convention

Same as A but with `.example` suffix instead of `.template`.

**Rejected.** The repo already uses `.template` for the sample panel. Consistency wins. No technical difference.

### Option E: `routes.d/` directory with auto-discovery

Use a directory of route files, each auto-discovered and loaded. Ship a README in the directory.

**Rejected.** Over-engineered for the use case. Introduces import order complexity, namespace collision risk, and per-file error isolation concerns. CIVs have ONE set of custom routes, not a plugin ecosystem. A single `routes.py` with internal organization is sufficient. If modular routes become necessary later, that is a future ADR.

## Decision Outcome

**Chosen Option:** A -- Template files with `.template` suffix

### Repository Structure

```
custom/
  .gitkeep                          # Ensures directory exists on clone
  README.md                         # One-line pointer to ADR-001
  routes.py.template                # Documented example of custom routes
  config.json.template              # Documented example of config overrides
  panels/
    .gitkeep                        # Ensures panels/ directory exists
    SAMPLE-PANEL.html.template      # Documented example of a custom panel
```

### .gitignore Rules

```gitignore
# CIV-specific custom overlay files (per-deployment, not shared)
custom/config.json
custom/routes.py
custom/startup.py
custom/quickfire.json
custom/panels/*.html
custom/__pycache__/

# Track templates and scaffolding (these ARE the repo's concern)
!custom/.gitkeep
!custom/README.md
!custom/routes.py.template
!custom/config.json.template
!custom/panels/.gitkeep
!custom/panels/SAMPLE-PANEL.html.template
```

### CIV Setup Flow

```bash
# After cloning or pulling the repo:
cd purebrain_portal/custom
cp routes.py.template routes.py       # Edit with your CIV's endpoints
cp config.json.template config.json   # Edit with your CIV's overrides
# Panels: cp panels/SAMPLE-PANEL.html.template panels/my-panel.html
```

### git pull Behavior

| Scenario | Result |
|----------|--------|
| CIV has custom/routes.py | Untouched (gitignored). Template may update separately. |
| CIV has no custom/routes.py | Template available. Shim is a no-op. |
| Upstream improves template | Template updates cleanly. CIV's files unaffected. |
| New CIV clones repo | Gets templates + directories. Copy, modify, done. |
| CIV wants to see new conventions | `diff routes.py routes.py.template` after pull. |

## Consequences

**Positive:**
- Zero risk of git pull overwriting customizations (different filenames, gitignored runtime files)
- Working examples ship with the repo (templates are runnable after copy)
- Templates and shim stay in sync (same repo, same commits)
- Consistent with existing `.template` convention
- Non-technical operators: copy file, edit values, restart portal

**Negative:**
- One extra step for CIVs (copy template to runtime file). Acceptable -- it is a one-time action per file.
- Templates could drift from actual CIV usage patterns over time. Mitigation: templates are minimal and focus on the contract (imports, `routes` list), not specific business logic.

## Implementation Notes for Coder Agent

1. Create `custom/.gitkeep`, `custom/panels/.gitkeep`
2. Create `custom/README.md` (3 lines: title, copy instruction, ADR link)
3. Create `custom/routes.py.template` from the example in this ADR (stripped-down, well-commented)
4. Create `custom/config.json.template` with safe defaults and a `_comment` field
5. Create `custom/panels/SAMPLE-PANEL.html.template` per ADR-001's panel format
6. Update `.gitignore` to match the rules above (add negation patterns for templates)
7. Ensure the existing `custom/routes.py` (Flux2's real routes) remains gitignored and is NOT committed
8. Verify: `git status` should show templates as tracked, runtime files as ignored
