"""Portal Skills Shop API for the PureBrain Portal.

Core module for browsing, installing, and managing Claude Code skills.
Provides endpoints for:
  - Listing local + installed skills with metadata
  - Fetching curated external skills registry
  - Installing skills from the registry
  - Uninstalling portal-installed skills
  - Viewing skill details (rendered SKILL.md)
"""
import asyncio
import json
import os
import re
import shutil
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse

from portal_config import (
    SCRIPT_DIR,
    PORTAL_VERSION,
    check_auth,
    sanitize_error,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Skills directory — Claude Code standard location
_SKILLS_DIR = Path.home() / ".claude" / "skills"

# Manifest of portal-installed skills (survives updates via _PRESERVED_FILES)
_INSTALLED_MANIFEST = SCRIPT_DIR / "installed-skills.json"

# Registry cache from release server
_REGISTRY_CACHE_FILE = SCRIPT_DIR / ".skills-registry-cache.json"
_REGISTRY_CACHE_TTL = 3600  # 1 hour

# Release server URL for curated registry
_REGISTRY_URL = os.environ.get(
    "SKILLS_REGISTRY_URL",
    "https://cc.purebrain.ai/api/releases/skills/registry.json",
)

# In-memory cache for local skill scan (avoid re-reading disk on every request)
_local_cache: dict = {"data": [], "timestamp": 0.0}
_LOCAL_CACHE_TTL = 300  # 5 minutes

# Categories for skill classification
CATEGORIES = [
    "Development",
    "Architecture",
    "Security",
    "Social & Content",
    "Infrastructure",
    "Communication",
    "Data & AI",
    "Ceremonies",
    "Uncategorized",
]

# Keyword-to-category mapping for auto-classification of local skills
_CATEGORY_KEYWORDS = {
    "Development": ["tdd", "test", "debug", "refactor", "code", "coder", "lint", "error"],
    "Architecture": ["brainstorm", "plan", "decompos", "design", "architect", "complexity"],
    "Security": ["security", "audit", "fortress", "vulnerab", "injection", "owasp"],
    "Social & Content": ["bluesky", "bsky", "linkedin", "blog", "image", "content", "social", "marketing"],
    "Infrastructure": ["git", "deploy", "docker", "infra", "fleet", "vps", "netlify", "github"],
    "Communication": ["email", "telegram", "hub", "comms", "cross-civ", "liaison"],
    "Ceremonies": ["ceremony", "gratitude", "seasonal", "shadow", "mirror", "dream", "night-watch"],
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_manifest() -> list[dict]:
    """Load the installed-skills manifest."""
    try:
        if _INSTALLED_MANIFEST.exists():
            return json.loads(_INSTALLED_MANIFEST.read_text())
    except Exception:
        pass
    return []


def _save_manifest(manifest: list[dict]) -> None:
    """Save the installed-skills manifest."""
    _INSTALLED_MANIFEST.write_text(json.dumps(manifest, indent=2))


def _parse_frontmatter(skill_path: Path) -> dict:
    """Parse YAML frontmatter from a SKILL.md file."""
    try:
        text = skill_path.read_text(errors="replace")
    except Exception:
        return {}

    if not text.startswith("---"):
        # No frontmatter — extract name from first heading
        for line in text.split("\n"):
            if line.startswith("# "):
                return {"name": line[2:].strip()}
        return {}

    # Parse YAML frontmatter between --- markers
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}

    fm = {}
    for line in parts[1].strip().split("\n"):
        if ":" in line:
            key, val = line.split(":", 1)
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            fm[key] = val
    return fm


def _classify_skill(name: str, description: str) -> str:
    """Auto-classify a skill into a category based on name + description."""
    combined = f"{name} {description}".lower()
    for category, keywords in _CATEGORY_KEYWORDS.items():
        if any(kw in combined for kw in keywords):
            return category
    return "Uncategorized"


def _scan_local_skills() -> list[dict]:
    """Scan .claude/skills/ for all local skills. Cached for 5 minutes."""
    now = time.time()
    if now - _local_cache["timestamp"] < _LOCAL_CACHE_TTL and _local_cache["data"]:
        return _local_cache["data"]

    skills = []
    if not _SKILLS_DIR.exists():
        return skills

    manifest = _load_manifest()
    installed_names = {s["name"] for s in manifest}

    for skill_dir in sorted(_SKILLS_DIR.iterdir()):
        if not skill_dir.is_dir():
            continue
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.exists():
            continue

        name = skill_dir.name
        fm = _parse_frontmatter(skill_file)
        description = fm.get("description", "")
        source = "local"

        # Check if this was installed by the portal
        for installed in manifest:
            if installed["name"] == name:
                source = installed.get("source", "external")
                break

        category = _classify_skill(name, description)

        skills.append({
            "name": name,
            "display_name": fm.get("name", name),
            "description": description,
            "category": category,
            "source": source,
            "installed": True,
            "path": str(skill_dir),
            "has_supporting_files": len(list(skill_dir.glob("*.md"))) > 1,
        })

    _local_cache["data"] = skills
    _local_cache["timestamp"] = now
    return skills


def _invalidate_cache() -> None:
    """Force refresh on next scan."""
    _local_cache["timestamp"] = 0.0


# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------

async def api_skills_list(request: Request) -> JSONResponse:
    """GET /api/skills — List all local skills with metadata."""
    if not check_auth(request):
        return JSONResponse({"error": "Unauthorized"}, 401)

    skills = _scan_local_skills()

    # Group by category for the frontend
    by_category = {}
    for s in skills:
        cat = s["category"]
        if cat not in by_category:
            by_category[cat] = []
        by_category[cat].append(s)

    return JSONResponse({
        "skills": skills,
        "by_category": by_category,
        "total": len(skills),
        "categories": [c for c in CATEGORIES if c in by_category],
    })


async def api_skills_registry(request: Request) -> JSONResponse:
    """GET /api/skills/registry — Fetch curated external skills index."""
    if not check_auth(request):
        return JSONResponse({"error": "Unauthorized"}, 401)

    # Check cache first
    try:
        if _REGISTRY_CACHE_FILE.exists():
            cache_data = json.loads(_REGISTRY_CACHE_FILE.read_text())
            if time.time() - cache_data.get("fetched_at", 0) < _REGISTRY_CACHE_TTL:
                return JSONResponse(cache_data)
    except Exception:
        pass

    # Fetch from release server
    try:
        # Built-in shared key — no .env configuration needed
        _update_token = "Hq-Of6ktPmQ-xDsJ4SjqbgGFZGIUR1oEushkZsghODY"
        headers = {"User-Agent": f"PureBrain-Portal/{PORTAL_VERSION}"}
        headers["X-Portal-Token"] = _update_token
        req = urllib.request.Request(_REGISTRY_URL, headers=headers)
        resp = urllib.request.urlopen(req, timeout=15)
        registry = json.loads(resp.read().decode())
    except Exception as e:
        # Return cache even if stale
        try:
            if _REGISTRY_CACHE_FILE.exists():
                return JSONResponse(json.loads(_REGISTRY_CACHE_FILE.read_text()))
        except Exception:
            pass
        return JSONResponse({
            "error": f"Could not fetch registry: {sanitize_error(e, 'skills registry')}",
            "skills": [],
        })

    # Mark installed status by comparing against local skills
    local_names = {s["name"] for s in _scan_local_skills()}
    for skill in registry.get("skills", []):
        skill["installed"] = skill.get("name", "") in local_names

    # Cache it
    registry["fetched_at"] = time.time()
    try:
        _REGISTRY_CACHE_FILE.write_text(json.dumps(registry, indent=2))
    except Exception:
        pass

    return JSONResponse(registry)


async def api_skills_detail(request: Request) -> JSONResponse:
    """GET /api/skills/detail/{name} — Get full skill content."""
    if not check_auth(request):
        return JSONResponse({"error": "Unauthorized"}, 401)

    name = request.path_params.get("name", "")
    if not name or ".." in name or "/" in name:
        return JSONResponse({"error": "Invalid skill name"}, 400)

    skill_dir = _SKILLS_DIR / name
    skill_file = skill_dir / "SKILL.md"

    if not skill_file.exists():
        return JSONResponse({"error": f"Skill '{name}' not found"}, 404)

    try:
        content = skill_file.read_text(errors="replace")
        fm = _parse_frontmatter(skill_file)

        # List supporting files
        supporting = []
        for f in sorted(skill_dir.glob("*.md")):
            if f.name != "SKILL.md":
                supporting.append(f.name)

        return JSONResponse({
            "name": name,
            "display_name": fm.get("name", name),
            "description": fm.get("description", ""),
            "content": content,
            "supporting_files": supporting,
            "category": _classify_skill(name, fm.get("description", "")),
            "path": str(skill_dir),
        })
    except Exception as e:
        return JSONResponse({"error": sanitize_error(e, "skill detail")}, 500)


async def api_skills_install(request: Request) -> JSONResponse:
    """POST /api/skills/install — Install a skill from the curated registry.

    Body: {"name": "skill-name", "source": "superpowers", "url": "https://raw..."}
    """
    if not check_auth(request):
        return JSONResponse({"error": "Unauthorized"}, 401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, 400)

    name = body.get("name", "").strip()
    source = body.get("source", "external")
    url = body.get("url", "")
    files = body.get("files", {})  # {filename: url} for supporting files

    # Validation
    if not name:
        return JSONResponse({"error": "Missing 'name' field"}, 400)

    # Path traversal prevention
    if ".." in name or "/" in name or "\\" in name:
        return JSONResponse({"error": "Invalid skill name (path traversal detected)"}, 400)

    # Check if skill already exists locally (not installed by portal)
    skill_dir = _SKILLS_DIR / name
    manifest = _load_manifest()
    installed_names = {s["name"] for s in manifest}

    if skill_dir.exists() and name not in installed_names:
        return JSONResponse({
            "error": f"Skill '{name}' already exists as a local skill. Cannot overwrite.",
            "status": "exists_local",
        }, 409)

    # Download the skill
    if not url:
        return JSONResponse({"error": "Missing 'url' field"}, 400)

    try:
        skill_dir.mkdir(parents=True, exist_ok=True)

        # Download main SKILL.md
        req = urllib.request.Request(
            url, headers={"User-Agent": f"PureBrain-Portal/{PORTAL_VERSION}"}
        )
        resp = urllib.request.urlopen(req, timeout=30)
        content = resp.read().decode()

        # Basic content validation — must look like a skill file
        if len(content) < 10:
            raise ValueError("Downloaded content is too small to be a valid skill")

        (skill_dir / "SKILL.md").write_text(content)

        # Download supporting files
        for filename, file_url in (files or {}).items():
            if ".." in filename or "/" in filename:
                continue  # skip dangerous filenames
            try:
                freq = urllib.request.Request(
                    file_url,
                    headers={"User-Agent": f"PureBrain-Portal/{PORTAL_VERSION}"},
                )
                fresp = urllib.request.urlopen(freq, timeout=30)
                (skill_dir / filename).write_text(fresp.read().decode())
            except Exception as fe:
                print(f"[skills] Warning: could not download supporting file {filename}: {fe}")

        # Update manifest
        entry = {
            "name": name,
            "source": source,
            "url": url,
            "installed_at": datetime.now(timezone.utc).isoformat(),
            "version": "1.0",
        }

        # Remove old entry if re-installing
        manifest = [s for s in manifest if s["name"] != name]
        manifest.append(entry)
        _save_manifest(manifest)
        _invalidate_cache()

        try:
            from portal_activity import log_activity
            log_activity(f"Skill installed: {name}", source, "system")
        except Exception:
            pass

        return JSONResponse({
            "status": "installed",
            "name": name,
            "source": source,
            "path": str(skill_dir),
            "files_downloaded": 1 + len(files or {}),
        })

    except Exception as e:
        # Clean up on failure
        if skill_dir.exists() and name not in installed_names:
            shutil.rmtree(skill_dir, ignore_errors=True)
        return JSONResponse({
            "error": f"Install failed: {sanitize_error(e, 'skill install')}",
            "status": "failed",
        }, 500)


async def api_skills_uninstall(request: Request) -> JSONResponse:
    """POST /api/skills/uninstall — Remove a portal-installed skill.

    Body: {"name": "skill-name"}
    Only removes skills that were installed by the portal (tracked in manifest).
    """
    if not check_auth(request):
        return JSONResponse({"error": "Unauthorized"}, 401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, 400)

    name = body.get("name", "").strip()
    if not name:
        return JSONResponse({"error": "Missing 'name' field"}, 400)

    # Path traversal prevention
    if ".." in name or "/" in name or "\\" in name:
        return JSONResponse({"error": "Invalid skill name"}, 400)

    # Check manifest — only uninstall portal-installed skills
    manifest = _load_manifest()
    entry = next((s for s in manifest if s["name"] == name), None)

    if not entry:
        return JSONResponse({
            "error": f"Skill '{name}' was not installed by the portal. Cannot uninstall local skills.",
            "status": "not_portal_installed",
        }, 403)

    # Remove files
    skill_dir = _SKILLS_DIR / name
    if skill_dir.exists():
        shutil.rmtree(skill_dir, ignore_errors=True)

    # Update manifest
    manifest = [s for s in manifest if s["name"] != name]
    _save_manifest(manifest)
    _invalidate_cache()

    return JSONResponse({
        "status": "uninstalled",
        "name": name,
    })
