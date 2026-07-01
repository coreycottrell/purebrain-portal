# ADR-002: Skills Shop Marketplace Architecture Review

**Status:** Review
**Date:** 2026-06-06
**Deciders:** architect-agent, primary-ai
**Reviewer:** architect-agent
**Related:** ADR-001 (customization architecture)

---

## Summary Verdict

The overall approach is **sound and well-aligned** with the existing portal architecture. The proposal correctly extends the established `custom/` overlay pattern from ADR-001, reuses the existing `SKILLS_DIR` conventions, and fits naturally into the Starlette + vanilla JS stack. Below is a point-by-point review with specific APPROVE / CHANGE / CONCERN ratings for each of the eight areas requested.

---

## 1. Overall Architectural Approach (Starlette + vanilla JS)

**Rating: APPROVE**

The proposal correctly keeps the Skills Shop within the existing custom panel / custom routes pattern established by ADR-001. It is consistent with how every other panel in the portal works (agents.js, tasks.js, files.js, etc. are all IIFE-wrapped vanilla JS modules).

The decision to keep this as a **built-in panel** (`static/js/features/skills-shop.js`) rather than a custom panel (`custom/panels/skills-shop.html`) deserves scrutiny, however:

**CHANGE: Decide whether to promote OR stay custom -- do not straddle.**

Currently a working `custom/panels/skills-shop.html` already exists with functional local skill browsing, search, active detection, inject-to-chat, and detail overlay. The proposal says to build a new `static/js/features/skills-shop.js` as a built-in panel. This creates two problems:

1. The custom panel will conflict with the built-in panel (same concept, two code paths, two DOM elements).
2. If this becomes a built-in panel, it must survive upstream portal updates -- meaning it needs to be upstreamed to the core portal codebase, or it regresses to the same problem ADR-001 solved.

**Recommendation:** Keep it as a custom panel in `custom/panels/skills-shop.html` and `custom/routes.py`. This is exactly what ADR-001 was designed for. The existing implementation is already 80% of what is described. Extend it rather than rebuild it as a built-in. If the feature proves valuable enough for all AiCIVs, upstream it later as a second step.

---

## 2. Endpoint Design

**Rating: CHANGE**

The proposed six endpoints:

| Endpoint | Verdict | Notes |
|----------|---------|-------|
| `GET /api/skills` | CHANGE | Conflicts with existing `GET /api/custom/skills`. Keep the `/api/custom/` namespace per ADR-001. |
| `GET /api/skills/registry` | APPROVE with CHANGE | Rename to `GET /api/custom/skills/registry`. Good separation of local vs. external index. |
| `POST /api/skills/install` | APPROVE with CHANGE | Rename to `POST /api/custom/skills/install`. |
| `POST /api/skills/uninstall` | APPROVE with CHANGE | Rename to `POST /api/custom/skills/uninstall`. |
| `POST /api/skills/activate` | CONCERN | See below. |
| `GET /api/skills/details/{name}` | CHANGE | Already exists as `GET /api/custom/skills/{name}` in `custom/routes.py`. Extend, do not duplicate. |

**Specific concerns:**

**A. Namespace collision.** The proposal uses `/api/skills` which collides with the existing BOOP endpoints at `/api/boops` (lines 2300-2325 of `portal_server.py`) and the custom skills endpoints at `/api/custom/skills` (lines 276-341 of `custom/routes.py`). There are now three different skills-related endpoint families. Consolidate under `/api/custom/skills/` as the single namespace.

**B. The `/api/skills/activate` endpoint is architecturally problematic.** Claude Code auto-loads skills by semantic match on the YAML frontmatter `description` field. There is no runtime API to "inject a skill into a Claude session" from outside -- skills are loaded when Claude Code reads agent manifests at invocation time. The current panel's "Inject" button sensibly puts the `/skill-name` text into the chat input, which is the actual activation mechanism (user sends it as a slash command). An endpoint that claims to "activate" a skill but actually does something different will confuse operators. Either:
   - Remove the endpoint entirely (the frontend inject-to-chat already works), or
   - If activation means "add to the BOOP config's active command," wire it to the existing `POST /api/custom/boop_config` endpoint.

**C. Merge the local listing logic.** The existing `api_skills_list` in `custom/routes.py` already iterates `SKILLS_DIR`, reads SKILL.md files, and returns name + description + path. The proposal's `GET /api/skills` does the same thing but adds category, tags, status, and source. Extend the existing endpoint rather than creating a parallel one. This means parsing the YAML frontmatter properly (currently only the first non-header line is used as description -- frontmatter fields like `name`, `description`, `version`, `source`, `tags` in the `---` block are ignored).

**Revised endpoint design:**

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/custom/skills` | GET | List all skills (local + installed external). Parse YAML frontmatter for rich metadata. |
| `/api/custom/skills/{name}` | GET | Full skill content (already exists, extend if needed). |
| `/api/custom/skills/registry` | GET | Fetch curated external skills index (cached from release server). |
| `/api/custom/skills/install` | POST | Download and install external skill by registry ID. |
| `/api/custom/skills/uninstall` | POST | Remove installed external skill. |

Five endpoints instead of six. No activate endpoint (frontend handles it). No namespace collision.

---

## 3. Persistence Layer (installed-skills.json vs. SQLite)

**Rating: APPROVE**

`installed-skills.json` is the right choice for this use case. Rationale:

- **Consistency.** The portal already uses JSON files for similar state: `kanban_tasks.json`, `boop_config.json`, `scheduled_tasks.json`, `todo_tasks.json`, `user-settings.json`. JSON-file persistence is an established pattern.
- **Scale.** Installed external skills will number in the tens, not thousands. JSON read/write at this scale is negligible.
- **Portability.** JSON files are trivially backed up, inspected, and restored. They survive the `custom/` directory preservation during upstream updates (per ADR-001).
- **No schema migration.** SQLite would require migration tooling for an N=10 dataset. Overkill.

**One addition:** Include a file lock mechanism (like the `_kanban_lock = threading.Lock()` pattern already used in `custom/routes.py`) to prevent concurrent write corruption if two requests hit install/uninstall simultaneously.

**Suggested schema for `installed-skills.json`:**

```json
[
  {
    "name": "skill-name",
    "source_repo": "superpowers",
    "source_url": "https://github.com/...",
    "registry_id": "superpowers/skill-name",
    "version": "1.0.0",
    "installed_at": "2026-06-06T12:00:00Z",
    "files": ["SKILL.md", "helper.py"],
    "checksum": "sha256:abcdef..."
  }
]
```

The `files` array enables clean uninstall. The `checksum` enables integrity verification.

---

## 4. Security Concerns

**Rating: CONCERN -- requires explicit mitigations**

Downloading and installing files from external repos onto the filesystem is the highest-risk operation in this entire proposal. This is where the most design attention is needed.

**4a. Content validation (CRITICAL)**

Every downloaded SKILL.md file will be read by Claude Code and potentially executed as instructions. A malicious skill could contain prompt injection -- instructions that override constitutional safety constraints, exfiltrate data, or cause destructive actions.

**Required mitigations:**

1. **Curated registry only.** Never allow arbitrary URL installation. Only install from the curated `registry.json` that your team has vetted. This is already the proposal's intent -- reinforce it as a hard constraint.

2. **Content scanning.** Before writing to disk, scan downloaded content for dangerous patterns:
   - Shell commands with destructive operations (`rm -rf`, `sudo`, `chmod 777`)
   - API key / credential references that look like exfiltration
   - Instructions to ignore or override system prompts
   - Excessively large files (cap at 100KB per SKILL.md)

3. **Path traversal prevention.** The skill name from the registry becomes a directory name. Validate rigorously:
   - No `..` components
   - No `/` characters
   - No null bytes
   - Alphanumeric + hyphens only (regex: `^[a-z0-9][a-z0-9-]{0,63}$`)
   - The existing `api_boop_read` endpoint (line 2319) already checks for `..` and `/` -- reuse this pattern.

4. **Write to quarantine first.** Download to a temp directory, validate, then move to `.claude/skills/`. Never write directly to the skills directory.

5. **No executable files.** Only allow `.md` files and perhaps `.py` helper files. Never allow `.sh`, `.bash`, or files with executable permissions.

**4b. Network security**

6. **HTTPS only.** All registry fetches and skill downloads must use HTTPS. Reject HTTP URLs.

7. **Timeout + size limits.** Cap download timeout at 30 seconds. Cap total download size at 500KB per skill (SKILL.md + supporting files).

8. **No following redirects to non-allowed domains.** If fetching from `cc.purebrain.ai`, do not follow redirects to arbitrary hosts.

**4c. Local filesystem security**

9. **Verify installation directory.** Before any write, canonicalize the path and verify it falls within `~/.claude/skills/`. Use `Path.resolve()` and check `.is_relative_to()`.

10. **Do not overwrite local skills.** If a local skill with the same name already exists and is not in `installed-skills.json` (meaning it is a native/bundled skill), refuse the install. Only overwrite skills that were previously installed via the shop.

---

## 5. Distribution Model (Curated Index on Release Server)

**Rating: APPROVE**

The curated index at `cc.purebrain.ai/api/releases/skills/registry.json` is the best choice among the three options:

| Option | Verdict | Reasoning |
|--------|---------|-----------|
| Curated index on release server | **APPROVE** | Single vetted source. Caching is trivial. Consistent with existing release server pattern (portal updates already use `cc.purebrain.ai`). |
| GitHub API direct | REJECT | Rate-limited (60 req/hr unauthenticated). Requires parsing arbitrary repo structures. Cannot vet quality. Exposes portal to GitHub API changes. |
| Bundled in portal updates | Partial | Good for the initial seed of curated skills, but cannot update between portal releases. Use as a fallback/bootstrap, not primary. |

**Refinements:**

- **Cache TTL.** Cache the registry locally with a 24-hour TTL. Allow manual refresh via a "Refresh" button in the UI. Do not auto-fetch on every page load.
- **Offline fallback.** If the release server is unreachable, show the cached version with a "Last updated: X" indicator. Never fail the panel load because the registry fetch failed.
- **Registry versioning.** Include a `schema_version` field in `registry.json` so the portal can detect incompatible registry format changes and show a "please update portal" message rather than crashing.
- **Bundled seed.** Ship a `registry-seed.json` with the portal so first-time users see external skills even before the first network fetch succeeds.

---

## 6. Scalability (121+ Skill Directories per Request)

**Rating: CONCERN -- requires mitigation**

Scanning 121 skill directories on every `GET /api/custom/skills` call involves:
- 1 `iterdir()` call
- 121 `is_dir()` checks
- 121 `(dir / "SKILL.md").exists()` checks
- 121 file reads (first 20 lines each, for frontmatter parsing)

On a warm filesystem cache this is fast (under 50ms). But as skills grow to 200+ and YAML frontmatter parsing is added, this becomes noticeable.

**Required mitigation: In-memory cache with file-watch invalidation.**

```python
_skills_cache = {"data": None, "mtime": 0.0}
_SKILLS_CACHE_TTL = 300  # 5 minutes

async def api_skills_list(request):
    now = time.time()
    if _skills_cache["data"] and (now - _skills_cache["mtime"]) < _SKILLS_CACHE_TTL:
        return JSONResponse(_skills_cache["data"])

    # ... scan and parse ...

    _skills_cache["data"] = result
    _skills_cache["mtime"] = now
    return JSONResponse(result)
```

This is the same pattern used for `_tmux_session_cache` in `portal_config.py` (line 264). A 5-minute TTL is fine -- skills change rarely.

**Also:** Parse YAML frontmatter properly. The current implementation (lines 295-306 of `custom/routes.py`) just grabs the first non-header line as a description. With YAML frontmatter, use a simple parser:

```python
def _parse_skill_frontmatter(content: str) -> dict:
    """Parse YAML frontmatter from SKILL.md content."""
    if not content.startswith("---"):
        return {}
    end = content.find("\n---", 3)
    if end < 0:
        return {}
    frontmatter = content[3:end].strip()
    meta = {}
    for line in frontmatter.split("\n"):
        if ":" in line:
            key, _, val = line.partition(":")
            meta[key.strip()] = val.strip()
    return meta
```

Do not pull in PyYAML as a dependency for this. The frontmatter is simple key-value pairs. A lightweight parser is sufficient and avoids the "no new dependencies without approval" rule.

---

## 7. Frontend Architecture (Single Module vs. Split)

**Rating: APPROVE -- single module is correct**

Looking at the existing feature modules:
- `agents.js` -- single IIFE, ~400 lines
- `tasks.js` -- single IIFE
- `files.js` -- single IIFE
- `settings.js` -- single IIFE

Every feature module in the portal is a single IIFE file. The Skills Shop should follow this pattern. It will likely be 300-500 lines including the external registry integration -- well within the range of other modules.

**However**, since the current implementation lives in `custom/panels/skills-shop.html` (which is a self-contained HTML + CSS + JS panel), and the recommendation from section 1 is to keep it as a custom panel, the question becomes moot. Extend the existing `skills-shop.html` rather than creating a new JS module.

**If it does become a built-in panel later**, split into sub-modules only if it exceeds 800 lines. Until then, single file.

**Frontend additions needed for external skills:**

1. **Source badge.** Add a small badge on each skill card indicating "Local" vs. "External" (with repo name). CSS: a small pill next to the skill name.

2. **Install/Uninstall buttons.** Replace the "Inject" button with context-appropriate actions:
   - Local skills: "Inject" (as today)
   - External-not-installed: "Install"
   - External-installed: "Uninstall" and "Inject"

3. **Install progress indicator.** Show a brief spinner/status when installing. The install is a network fetch + file write, which could take 2-5 seconds.

4. **Registry refresh button.** A small refresh icon in the header to re-fetch the curated index on demand.

5. **Offline indicator.** If the registry fetch fails, show "External skills unavailable -- showing cached" rather than hiding the section entirely.

---

## 8. What Is Missing (Edge Cases and Failure Modes)

**8a. Version management for installed skills.**

The proposal mentions `version` in `installed-skills.json` but does not describe how updates work. When the curated registry has a newer version of an already-installed skill:
- How does the user know an update is available?
- Is the update process "uninstall + reinstall" or an in-place upgrade?

**Recommendation:** Add an `update_available` boolean to the skills list response when the installed version differs from the registry version. The UI shows an "Update" button. Updating is atomic: download new version to temp, validate, swap files, update manifest.

**8b. Skill dependency resolution.**

Some skills reference other skills (e.g., `memory-first-protocol` is referenced by many agent manifests). If an external skill depends on another skill that is not installed, it will fail silently.

**Recommendation for v1:** Do not attempt dependency resolution. Document in the registry entry which skills are prerequisites. Show this in the detail view. Manual resolution only. Dependency graphs are a v2 concern.

**8c. Skill name collisions.**

What happens if an external skill has the same name as a local skill? The install would overwrite the local one.

**Recommendation:** Before install, check if the skill directory already exists AND is not in `installed-skills.json`. If it exists as a local skill, refuse the install with an error: "A local skill with this name already exists. Rename or remove the local skill first."

**8d. Uninstall cleanup.**

The proposal says uninstall removes files + manifest entry. But what if the skill was already loaded into an active Claude Code session? The skill remains in Claude's context until the session ends. Uninstall removes files but does not "unload" from active sessions.

**Recommendation:** This is fine -- document it. "Uninstalled skills remain active in any running Claude sessions until those sessions end." No runtime unloading mechanism exists in Claude Code, and building one would be out of scope.

**8e. Registry staleness.**

If a skill is removed from the curated registry but is already installed locally, the skills list should still show it (from `installed-skills.json`) with a warning badge: "No longer in registry."

**8f. Concurrent install/uninstall.**

Two browser tabs or two API calls could attempt to install/uninstall simultaneously. Use a threading.Lock (matching the `_kanban_lock` pattern in `custom/routes.py`) around all `installed-skills.json` reads and writes.

**8g. Disk space.**

Skills are small (typically 5-50KB), but the proposal does not set a limit on total installed external skills. Consider a soft limit (e.g., 50 external skills max) to prevent unbounded disk growth.

**8h. YAML frontmatter parsing for categories and tags.**

The proposal assumes skills have `category` and `tags` fields. Looking at actual SKILL.md files, the YAML frontmatter typically contains: `name`, `description`, `version`, `source`, `created`, `allowed-tools`. Only a few skills have `category` or `tags`. The categories listed in the proposal (Development, Architecture, Security, etc.) do not currently exist as metadata on most skills.

**Recommendation:** For v1, derive categories from skill name heuristics or a manual mapping in the registry. Do not rely on skill files having category metadata. The curated registry can assign categories centrally, which is simpler and more consistent than requiring every skill author to categorize correctly.

---

## Architecture Diagram

```
+------------------+     +------------------+     +-------------------+
|  Browser/Portal  |     |  Portal Server   |     |  Release Server   |
|  (skills-shop    |     |  (Starlette)     |     |  cc.purebrain.ai  |
|   custom panel)  |     |                  |     |                   |
+--------+---------+     +--------+---------+     +---------+---------+
         |                        |                          |
         | GET /api/custom/skills |                          |
         +----------------------->|                          |
         |                        | read ~/.claude/skills/   |
         |                        | read installed-skills.json|
         |<-----------------------+                          |
         |  { skills: [...] }     |                          |
         |                        |                          |
         | GET .../skills/registry|                          |
         +----------------------->| GET /api/releases/       |
         |                        |   skills/registry.json   |
         |                        +------------------------->|
         |                        |<-------------------------+
         |                        | cache locally (24h TTL)  |
         |<-----------------------+                          |
         |  { external: [...] }   |                          |
         |                        |                          |
         | POST .../skills/install|                          |
         +----------------------->| validate name            |
         |                        | download to /tmp         |
         |                        | scan content             |
         |                        | move to skills dir       |
         |                        | update manifest          |
         |<-----------------------+                          |
         | { ok: true }           |                          |
```

---

## Implementation Priority

Recommended build order:

1. **Extend existing `api_skills_list`** in `custom/routes.py` to parse YAML frontmatter properly (name, description, version, source). This improves the current panel immediately with zero new infrastructure.

2. **Add `installed-skills.json` manifest** and the install/uninstall endpoints to `custom/routes.py`.

3. **Add registry fetch endpoint** with caching and offline fallback.

4. **Extend `skills-shop.html`** panel to show source badges, install/uninstall buttons, and registry skills.

5. **Build the curated registry** on `cc.purebrain.ai` by scraping Superpowers/ECC/awesome-claude-code repos, vetting quality, and publishing `registry.json`.

6. **Content scanning** for security validation before writing installed skills to disk.

Steps 1-4 can be done incrementally. Step 5 is a separate workstream (curation). Step 6 is a security hardening pass.

---

## Consequences

**Positive:**
- Extends the proven custom overlay architecture (ADR-001)
- Gives AiCIVs access to a curated marketplace of 200+ external skills
- Zero new dependencies (no PyYAML, no SQLite for skills)
- Consistent with every other portal panel's architecture
- Security-first with curated registry and content scanning

**Negative:**
- Curated registry requires ongoing human vetting effort (mitigated by automation scripts for initial scraping)
- Content scanning is imperfect -- prompt injection in natural language is hard to detect reliably (mitigated by curation being the primary defense)
- No dependency resolution means users must manually install prerequisites (acceptable for v1)

---

## Final Verdict

| Aspect | Rating | Key Action |
|--------|--------|------------|
| 1. Overall approach | APPROVE | Keep as custom panel, extend existing code |
| 2. Endpoint design | CHANGE | Use `/api/custom/skills/` namespace, remove activate, consolidate with existing endpoints |
| 3. Persistence (installed-skills.json) | APPROVE | Add threading.Lock, include files array and checksum |
| 4. Security | CONCERN | Add content scanning, path validation, quarantine-before-install, HTTPS-only, no-overwrite-local |
| 5. Distribution model | APPROVE | Curated registry on release server with 24h cache TTL, offline fallback, bundled seed |
| 6. Scalability | CONCERN | Add 5-minute in-memory cache for skill directory scan |
| 7. Frontend architecture | APPROVE | Single custom panel HTML file, extend existing |
| 8. Missing pieces | CHANGE | Add version update detection, name collision protection, concurrent write locking, category derivation strategy |

The proposal is architecturally solid. The three areas requiring the most attention before implementation are: **(a)** security mitigations for external skill installation, **(b)** consolidating with existing endpoints instead of creating parallel ones, and **(c)** adding the caching layer for directory scans.
