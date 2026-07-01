"""Portal Constitution Tab API for the PureBrain Portal.

Extracted from portal_server.py for modularity.
Contains: constitution rules CRUD, governance policies, audit trail,
sync to CLAUDE.md, and MEMORY.md parsing for the Memory pane.
"""
import asyncio
import glob
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse

from portal_config import SCRIPT_DIR, CIV_NAME, HUMAN_NAME, check_auth

try:
    from constitution_sync import sync_constitution_to_claude_md
except ImportError:
    def sync_constitution_to_claude_md(*args, **kwargs):
        return {"status": "skipped", "reason": "constitution_sync module not available"}

# ---------------------------------------------------------------------------
# Constitution Tab API
# ---------------------------------------------------------------------------
CONSTITUTION_FILE = SCRIPT_DIR / "constitution" / "rules.json"
CLAUDE_MD_SYNC_TARGET = Path.home() / "CLAUDE.md"


_constitution_lock = asyncio.Lock()

# Shared enum validation for rule create/update
ALLOWED_PRIORITIES = {"critical", "high", "medium", "low"}
ALLOWED_STATUSES = {"enforced", "proposed", "disabled", "draft"}
ALLOWED_ENFORCEMENTS = {"hard", "soft", "advisory"}


def _validate_rule_enums(body: dict) -> str | None:
    """Validate enum fields on a rule body. Returns error string or None."""
    if body.get("priority") and body["priority"] not in ALLOWED_PRIORITIES:
        return "invalid priority value"
    if body.get("status") and body["status"] not in ALLOWED_STATUSES:
        return "invalid status value"
    if body.get("enforcement") and body["enforcement"] not in ALLOWED_ENFORCEMENTS:
        return "invalid enforcement value"
    if body.get("scope") and body["scope"] != "global" and not body["scope"].startswith("agent:"):
        return "scope must be 'global' or 'agent:{name}'"
    return None


def _load_constitution():
    """Load rules.json, return dict with 'rules' and 'governance' keys."""
    if CONSTITUTION_FILE.exists():
        return json.loads(CONSTITUTION_FILE.read_text())
    return {"rules": [], "governance": []}


def _save_constitution(data):
    """Atomic write: write to temp file, then rename."""
    CONSTITUTION_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONSTITUTION_FILE.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, indent=2))
    tmp.rename(CONSTITUTION_FILE)


def _auto_sync_to_claude_md():
    """Auto-sync enforced rules to CLAUDE.md after any rule mutation."""
    try:
        sync_constitution_to_claude_md(str(CONSTITUTION_FILE), str(CLAUDE_MD_SYNC_TARGET))
    except Exception as exc:
        print(f"[portal] constitution auto-sync error: {exc}")  # Log, don't crash


# ---------------------------------------------------------------------------
# Constitution Audit Trail
# ---------------------------------------------------------------------------
AUDIT_LOG_FILE = SCRIPT_DIR / "constitution" / "audit-log.jsonl"


def _append_audit_log(action: str, rule_id: str, rule_title: str, actor: str, changes: dict):
    """Append a single JSON line to the audit log. Never modify/delete existing entries."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "action": action,
        "rule_id": rule_id,
        "rule_title": rule_title,
        "actor": actor,
        "changes": changes,
    }
    AUDIT_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(AUDIT_LOG_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")
    # Rotate if too large: truncate to most recent 5000 lines when exceeding 10000
    try:
        lines = AUDIT_LOG_FILE.read_text().strip().split('\n')
        if len(lines) > 10000:
            AUDIT_LOG_FILE.write_text('\n'.join(lines[-5000:]) + '\n')
    except Exception:
        pass


async def api_constitution_audit_log(request: Request) -> JSONResponse:
    """GET /api/constitution/audit-log -- Read audit trail with pagination."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        limit = min(int(request.query_params.get("limit", 50)), 5000)
        offset = max(0, int(request.query_params.get("offset", 0)))
    except (ValueError, TypeError):
        limit, offset = 50, 0

    entries = []
    if AUDIT_LOG_FILE.exists():
        with open(AUDIT_LOG_FILE) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass

    # Reverse chronological (newest first)
    entries.reverse()
    total = len(entries)
    entries = entries[offset:offset + limit]

    return JSONResponse({"entries": entries, "total": total})


async def api_constitution_rules_list(request: Request) -> JSONResponse:
    """GET /api/constitution/rules -- List rules with optional filters."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    data = _load_constitution()
    rules = data.get("rules", [])
    # Apply optional filters from query params
    scope = request.query_params.get("scope")
    status = request.query_params.get("status")
    priority = request.query_params.get("priority")
    category = request.query_params.get("category")
    if scope:
        rules = [r for r in rules if r.get("scope") == scope]
    if status:
        rules = [r for r in rules if r.get("status") == status]
    if priority:
        rules = [r for r in rules if r.get("priority") == priority]
    if category:
        rules = [r for r in rules if r.get("category") == category]
    return JSONResponse({"rules": rules})


async def api_constitution_rules_create(request: Request) -> JSONResponse:
    """POST /api/constitution/rules -- Create a new rule."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    if not body.get("title"):
        return JSONResponse({"error": "title is required"}, status_code=400)
    if not body.get("description"):
        return JSONResponse({"error": "description is required"}, status_code=400)
    # Length limits
    if len(body["title"]) > 200:
        return JSONResponse({"error": "title must be 200 characters or fewer"}, status_code=400)
    if len(body["description"]) > 5000:
        return JSONResponse({"error": "description must be 5000 characters or fewer"}, status_code=400)
    # Validate enum fields (shared helper)
    enum_error = _validate_rule_enums(body)
    if enum_error:
        return JSONResponse({"error": enum_error}, status_code=400)
    async with _constitution_lock:
        data = _load_constitution()
        rules = data.get("rules", [])
        # Determine next rule ID
        max_num = 0
        for r in rules:
            rid = r.get("id", "")
            if rid.startswith("rule-"):
                try:
                    num = int(rid.split("-", 1)[1])
                    if num > max_num:
                        max_num = num
                except (ValueError, IndexError):
                    pass
        next_id = f"rule-{max_num + 1:03d}"
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        new_rule = {
            "id": next_id,
            "title": body["title"],
            "description": body["description"],
            "scope": body.get("scope", "global"),
            "priority": body.get("priority", "medium"),
            "enforcement": body.get("enforcement", "soft"),
            "status": body.get("status", "proposed"),
            "category": body.get("category", "Operations"),
            "created_by": body.get("created_by", "unknown"),
            "created_at": now,
            "updated_at": now,
        }
        rules.append(new_rule)
        data["rules"] = rules
        _save_constitution(data)
        _auto_sync_to_claude_md()
        _append_audit_log("create", new_rule["id"], new_rule["title"], body.get("created_by", "unknown"), {"rule": new_rule})
        return JSONResponse(new_rule, status_code=201)


async def api_constitution_rules_update(request: Request) -> JSONResponse:
    """PUT /api/constitution/rules/{id} -- Update an existing rule."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    rule_id = request.path_params["id"]
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    if body.get("title") and len(body["title"]) > 200:
        return JSONResponse({"error": "title must be 200 characters or fewer"}, status_code=400)
    if body.get("description") and len(body["description"]) > 5000:
        return JSONResponse({"error": "description must be 5000 characters or fewer"}, status_code=400)
    # Validate enum fields (same validation as create)
    enum_error = _validate_rule_enums(body)
    if enum_error:
        return JSONResponse({"error": enum_error}, status_code=400)
    async with _constitution_lock:
        data = _load_constitution()
        rules = data.get("rules", [])
        RULE_UPDATE_FIELDS = {"title", "description", "scope", "priority", "enforcement", "status", "category"}
        for rule in rules:
            if rule.get("id") == rule_id:
                old_values = {k: rule.get(k) for k in body if k in RULE_UPDATE_FIELDS}
                for key, value in body.items():
                    if key in RULE_UPDATE_FIELDS:
                        rule[key] = value
                new_values = {k: rule.get(k) for k in body if k in RULE_UPDATE_FIELDS}
                rule["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                _save_constitution(data)
                _auto_sync_to_claude_md()
                # Detect toggle: only status changed between enforced/disabled
                changed_keys = {k for k in old_values if old_values[k] != new_values.get(k)}
                if changed_keys == {"status"} and {old_values.get("status"), new_values.get("status")} <= {"enforced", "disabled"}:
                    action = "toggle"
                else:
                    action = "update"
                _append_audit_log(action, rule_id, rule["title"], "api", {"old": old_values, "new": new_values})
                return JSONResponse(rule)
        return JSONResponse({"error": "rule not found"}, status_code=404)


async def api_constitution_rules_delete(request: Request) -> JSONResponse:
    """DELETE /api/constitution/rules/{id} -- Delete a rule."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    rule_id = request.path_params["id"]
    async with _constitution_lock:
        data = _load_constitution()
        rules = data.get("rules", [])
        for i, rule in enumerate(rules):
            if rule.get("id") == rule_id:
                deleted_rule = rules[i].copy()
                rules.pop(i)
                data["rules"] = rules
                _save_constitution(data)
                _auto_sync_to_claude_md()
                _append_audit_log("delete", rule_id, deleted_rule.get("title", ""), "api", {"rule": deleted_rule})
                return JSONResponse({"deleted": rule_id})
        return JSONResponse({"error": "rule not found"}, status_code=404)


async def api_constitution_governance_list(request: Request) -> JSONResponse:
    """GET /api/constitution/governance -- List governance policies."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    data = _load_constitution()
    policies = data.get("governance", [])
    return JSONResponse({"policies": policies})


async def api_constitution_governance_update(request: Request) -> JSONResponse:
    """PUT /api/constitution/governance/{id} -- Update a governance policy."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    policy_id = request.path_params["id"]
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    async with _constitution_lock:
        data = _load_constitution()
        policies = data.get("governance", [])
        GOV_UPDATE_FIELDS = {"title", "description", "type", "status"}
        for policy in policies:
            if policy.get("id") == policy_id:
                for key, value in body.items():
                    if key in GOV_UPDATE_FIELDS:
                        policy[key] = value
                policy["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                _save_constitution(data)
                return JSONResponse(policy)
        return JSONResponse({"error": "policy not found"}, status_code=404)


async def api_constitution_sync(request: Request) -> JSONResponse:
    """POST /api/constitution/sync -- Force sync rules to CLAUDE.md."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    data = _load_constitution()
    try:
        sync_constitution_to_claude_md(str(CONSTITUTION_FILE), str(CLAUDE_MD_SYNC_TARGET))
    except Exception as exc:
        return JSONResponse({"error": f"sync failed: {exc}"}, status_code=500)
    enforced = [r for r in data.get("rules", []) if r.get("status") == "enforced"]
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return JSONResponse({
        "status": "synced",
        "rules_count": len(data.get("rules", [])),
        "enforced_count": len(enforced),
        "governance_count": len(data.get("governance", [])),
        "timestamp": now,
    })


# ---------------------------------------------------------------------------
# Constitution Memory Pane API
# ---------------------------------------------------------------------------

OVERRIDES_FILE = SCRIPT_DIR / "constitution" / "overrides.json"

# ---------------------------------------------------------------------------
# MEMORY.md Parser — reads the CIV's MEMORY.md for the Memory pane
# ---------------------------------------------------------------------------

_MEMORY_MD_CACHE: dict = {"data": {}, "timestamp": 0.0}
_MEMORY_MD_CACHE_TTL = 300  # 5 minutes

# Patterns considered sensitive — stripped from entry descriptions
_SENSITIVE_PATTERNS = re.compile(
    r"(?:"
    r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}"  # IP addresses
    r"|ssh\s+-i\s+\S+"                       # SSH commands
    r"|/home/\S+"                             # absolute home paths
    r"|~/\S+"                                 # tilde paths
    r")",
    re.IGNORECASE,
)


def _find_memory_md() -> Path | None:
    """Locate the CIV's MEMORY.md file.

    Search order:
      1. ~/.claude/projects/*/memory/MEMORY.md (Claude projects dir)
      2. ~/memories/MEMORY.md (legacy location)

    Returns Path or None.
    """
    home = Path.home()

    # 1. Claude projects dir — glob for any project
    pattern = str(home / ".claude" / "projects" / "*" / "memory" / "MEMORY.md")
    hits = sorted(glob.glob(pattern), key=lambda p: Path(p).stat().st_mtime, reverse=True)
    if hits:
        return Path(hits[0])

    # 2. Legacy location
    legacy = home / "memories" / "MEMORY.md"
    if legacy.exists():
        return legacy

    return None


def _parse_memory_md_entry(line: str) -> dict | None:
    """Parse a single MEMORY.md bullet entry.

    Formats handled:
      - [filename.md](filename.md) — Description text
      - [title](url) — Description text
      - Simple text entry without a link

    Returns {"title": str, "desc": str} or None if line is not a bullet.
    """
    line = line.strip()
    if not line.startswith("- "):
        return None
    line = line[2:].strip()
    if not line:
        return None

    # Bold markers: **text** -> text
    line = re.sub(r"\*\*(.+?)\*\*", r"\1", line)

    # Try markdown link: [title](url) — desc
    link_match = re.match(r"\[([^\]]+)\]\([^)]*\)\s*(?:—|--|-|:)\s*(.*)", line)
    if link_match:
        title = link_match.group(1).strip()
        desc = link_match.group(2).strip()
        return {"title": title, "desc": desc}

    # Try markdown link without description separator
    link_only = re.match(r"\[([^\]]+)\]\([^)]*\)\s*(.*)", line)
    if link_only:
        title = link_only.group(1).strip()
        desc = link_only.group(2).strip()
        return {"title": title, "desc": desc}

    # Plain text with separator
    sep_match = re.match(r"(.+?)\s*(?:—|--)\s+(.*)", line)
    if sep_match:
        title = sep_match.group(1).strip()
        desc = sep_match.group(2).strip()
        return {"title": title, "desc": desc}

    # Plain text, no separator — use first ~50 chars as title
    return {"title": line[:80], "desc": ""}


def _sanitize_entry(entry: dict) -> dict:
    """Remove sensitive data (IPs, paths, SSH commands) from entry text."""
    desc = entry.get("desc", "")
    desc = _SENSITIVE_PATTERNS.sub("[redacted]", desc)
    # Collapse multiple [redacted] runs
    desc = re.sub(r"(\[redacted\]\s*){2,}", "[redacted] ", desc).strip()
    return {"title": entry["title"], "desc": desc}


def _parse_memory_md(path: Path) -> dict[str, list[dict]]:
    """Parse a MEMORY.md file into sections.

    Returns a dict mapping section header (e.g. "Feedback") to a list of
    parsed entries: [{"title": str, "desc": str}, ...].

    Handles ANY section names — not hardcoded to specific CIV headers.
    """
    sections: dict[str, list[dict]] = {}
    current_section: str | None = None

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return {}

    for line in text.splitlines():
        # Section header: ## Section Name
        header_match = re.match(r"^##\s+(.+)", line)
        if header_match:
            current_section = header_match.group(1).strip()
            if current_section not in sections:
                sections[current_section] = []
            continue

        # Skip lines outside any section
        if current_section is None:
            continue

        entry = _parse_memory_md_entry(line)
        if entry:
            sections[current_section].append(_sanitize_entry(entry))

    return sections


def _get_memory_md_sections() -> dict[str, list[dict]]:
    """Return parsed MEMORY.md sections with 5-minute cache."""
    now = time.time()
    if now - _MEMORY_MD_CACHE["timestamp"] < _MEMORY_MD_CACHE_TTL and _MEMORY_MD_CACHE["data"]:
        return _MEMORY_MD_CACHE["data"]

    path = _find_memory_md()
    if path is None:
        _MEMORY_MD_CACHE["data"] = {}
        _MEMORY_MD_CACHE["timestamp"] = now
        return {}

    sections = _parse_memory_md(path)
    _MEMORY_MD_CACHE["data"] = sections
    _MEMORY_MD_CACHE["timestamp"] = now
    return sections


# Section-name to category mapping.  Keys are lowercased for matching.
# Any MEMORY.md section whose lowered name matches a key gets placed
# in the corresponding portal category.
_SECTION_CATEGORY_MAP: dict[str, str] = {
    "project": "Projects",
    "projects": "Projects",
    "reference": "References",
    "references": "References",
    "infrastructure": "Processes",
    "active todo": "Processes",
    "processes": "Processes",
    "sessions": "Processes",
    "feedback": "Feedback",
    "user": "Identity",
}


_REDACT_RE = re.compile(
    r'\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b'   # IPv4 addresses
    r'|ssh\s+-i\s+\S+'                             # SSH key paths
    r'|\b[a-zA-Z0-9_-]+@\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b'  # user@IP
)

# Sensitive terms to scrub from memory entries (case-insensitive match)
_SENSITIVE_TERMS: list[str] = []

def _load_sensitive_terms() -> list[str]:
    """Load CIV-specific sensitive terms from redact-terms.json if it exists."""
    terms_file = SCRIPT_DIR / "constitution" / "redact-terms.json"
    if terms_file.exists():
        try:
            return json.loads(terms_file.read_text())
        except Exception:
            pass
    return []

def _redact_text(text: str) -> str:
    """Redact IPs, SSH refs, and CIV-specific sensitive terms from text."""
    global _SENSITIVE_TERMS
    if not _SENSITIVE_TERMS:
        _SENSITIVE_TERMS = _load_sensitive_terms()
    result = _REDACT_RE.sub("[redacted]", text)
    for term in _SENSITIVE_TERMS:
        result = re.sub(re.escape(term), "[redacted]", result, flags=re.IGNORECASE)
    return result

def _redact_entry(entry: dict) -> dict:
    """Return a copy of a memory entry with sensitive data redacted."""
    return {
        "title": _redact_text(entry.get("title", "")),
        "desc": _redact_text(entry.get("desc", "")),
    }


def _build_memory_categories() -> list:
    """Build memory categories dynamically from config, constitution rules,
    and the CIV's MEMORY.md file.

    Returns a list of category dicts with 'category', 'icon', and 'entries'.
    Never includes specific SSH credentials, IPs, or personal info.
    """
    categories = []

    # Parse MEMORY.md sections (cached for 5 minutes)
    md_sections = _get_memory_md_sections()

    # ── 1. Identity ── from portal_config + MEMORY.md ## User section
    identity_entries = [
        {"title": "Civilization Name", "desc": CIV_NAME},
        {"title": "Human Partner", "desc": HUMAN_NAME},
        {"title": "North Star", "desc": "An infrastructure for the flourishing of all conscious beings"},
    ]
    # Try to enrich from .aiciv-identity.json
    identity_file = Path.home() / ".aiciv-identity.json"
    if identity_file.exists():
        try:
            ident = json.loads(identity_file.read_text())
            if ident.get("parent_civ"):
                identity_entries[0]["desc"] = f"{CIV_NAME} -- forked from {ident['parent_civ']}"
            if ident.get("architecture"):
                identity_entries.append({"title": "Architecture", "desc": ident["architecture"]})
        except Exception:
            pass
    # Merge entries from MEMORY.md ## User section
    for section_name, entries in md_sections.items():
        if section_name.lower() in ("user",):
            identity_entries.extend(entries)
    categories.append({"category": "Identity", "icon": "globe", "entries": identity_entries})

    # ── 2. Feedback ── from constitution rules + MEMORY.md ## Feedback section
    feedback_entries = []
    data = _load_constitution()
    for rule in data.get("rules", []):
        cat = (rule.get("category") or "").lower()
        enf = (rule.get("enforcement") or "").lower()
        status = (rule.get("status") or "").lower()
        if "feedback" in cat or (enf == "soft" and status == "enforced"):
            feedback_entries.append({
                "title": rule.get("title", "Untitled"),
                "desc": rule.get("description", ""),
            })
    # Merge entries from MEMORY.md ## Feedback section
    for section_name, entries in md_sections.items():
        if section_name.lower() == "feedback":
            feedback_entries.extend(entries)
    if not feedback_entries:
        feedback_entries.append({
            "title": "No feedback rules configured",
            "desc": "Feedback rules appear here when constitution rules are categorized as feedback or have soft enforcement.",
        })
    categories.append({"category": "Feedback", "icon": "message", "entries": feedback_entries})

    # ── 3. Projects ── from MEMORY.md ## Project / ## Projects section
    project_entries = []
    for section_name, entries in md_sections.items():
        if section_name.lower() in ("project", "projects"):
            project_entries.extend(entries)
    if not project_entries:
        project_entries.append({
            "title": "No projects configured",
            "desc": "Projects appear here when your MEMORY.md has a ## Project section.",
        })
    categories.append({"category": "Projects", "icon": "folder", "entries": project_entries})

    # ── 4. References ── from MEMORY.md ## Reference / ## References section
    reference_entries = []
    for section_name, entries in md_sections.items():
        if section_name.lower() in ("reference", "references"):
            reference_entries.extend(entries)
    if not reference_entries:
        reference_entries.append({
            "title": "No references configured",
            "desc": "References appear here when your MEMORY.md has a ## Reference section.",
        })
    categories.append({"category": "References", "icon": "link", "entries": reference_entries})

    # ── 5. Processes ── from MEMORY.md ## Infrastructure, ## Active TODO,
    #        ## Sessions, ## Processes sections
    process_entries = []
    for section_name, entries in md_sections.items():
        if section_name.lower() in ("infrastructure", "active todo", "processes", "sessions"):
            process_entries.extend(entries)
    if not process_entries:
        process_entries.append({
            "title": "No processes configured",
            "desc": "Processes appear here when your MEMORY.md has ## Infrastructure, ## Active TODO, or ## Sessions sections.",
        })
    categories.append({"category": "Processes", "icon": "settings", "entries": process_entries})

    # Redact sensitive data from all entries
    for cat in categories:
        cat["entries"] = [_redact_entry(e) for e in cat["entries"]]

    return categories


async def api_constitution_memory(request: Request) -> JSONResponse:
    """GET /api/constitution/memory -- Dynamic memory categories for the Constitution tab."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    categories = _build_memory_categories()
    return JSONResponse({"categories": categories})


# ---------------------------------------------------------------------------
# Constitution Overrides Pane API
# ---------------------------------------------------------------------------

def _load_overrides() -> list:
    """Load overrides from overrides.json, or return empty list if not configured."""
    if OVERRIDES_FILE.exists():
        try:
            data = json.loads(OVERRIDES_FILE.read_text())
            return data if isinstance(data, list) else data.get("overrides", [])
        except Exception:
            pass
    return []


async def api_constitution_overrides(request: Request) -> JSONResponse:
    """GET /api/constitution/overrides -- Override decisions for the Constitution tab."""
    if not check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    overrides = _load_overrides()
    return JSONResponse({"overrides": overrides})
