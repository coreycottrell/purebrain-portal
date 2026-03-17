# Agent Lifecycle Hooks — PureBrain Portal

## What This Does

When Aether spawns a subagent (via the `Task` tool), the portal's Agent Roster panel updates in real-time to show:

- Green pulse dot + "ACTIVE" badge on active agents
- The task description Aether gave the agent
- Last run timestamp when the agent goes idle
- Active agents sorted to the top of the roster

---

## Architecture

```
Claude Code (hooks) → update_agent_status.sh → POST /api/agents/status → SQLite DB
                                                                              ↑
Portal frontend (every 5s when Agents panel open) ─────────────────────── GET /api/agents
```

The portal frontend polls `/api/agents` every 5 seconds when the Agents panel is visible. No WebSocket required.

---

## API Endpoint

### POST /api/agents/status

No auth required (so hook scripts work without a bearer token).

**Request body:**
```json
{
  "agent": "dept-accounting-finance",
  "status": "active",
  "task": "Building Q1 financial model"
}
```

**Status values:** `active` | `idle` | `working` | `offline`

**Response:**
```json
{ "ok": true, "agent": "dept-accounting-finance", "status": "active", "updated": "2026-03-16T22:00:00.000000" }
```

When status is set to `idle`, the `task` field is cleared automatically and `last_completed` is recorded.

---

## Shell Script

```bash
# Mark agent active with task description
./update_agent_status.sh dept-accounting-finance active "Building Q1 financial model"

# Mark agent as working (shows blue pulse instead of green)
./update_agent_status.sh browser-vision-tester working "Auditing pay-test-2 flow"

# Mark agent idle (clears task, records completion time)
./update_agent_status.sh dept-accounting-finance idle
```

From anywhere on the server:
```bash
/home/jared/purebrain_portal/update_agent_status.sh <agent-id> <status> [task]
```

---

## Wiring to Claude Code Hooks

Claude Code supports lifecycle hooks via `.claude/settings.json`. Add hooks to fire `update_agent_status.sh` when the `Task` tool is called.

### Hook File Location

Create or edit: `/home/jared/projects/AI-CIV/aether/.claude/settings.json`

### Hook Configuration

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Task",
        "hooks": [
          {
            "type": "command",
            "command": "/home/jared/purebrain_portal/hooks/pre_task_hook.sh"
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "Task",
        "hooks": [
          {
            "type": "command",
            "command": "/home/jared/purebrain_portal/hooks/post_task_hook.sh"
          }
        ]
      }
    ]
  }
}
```

### Hook Scripts

**`/home/jared/purebrain_portal/hooks/pre_task_hook.sh`** — fires before a Task tool call:

```bash
#!/bin/bash
# Pre-task hook: reads the Task tool input from stdin (JSON) and marks the agent active.
#
# Claude Code passes tool input as JSON on stdin. Example:
# { "description": "Analyze Q1 data", "prompt": "...", "subagent_model": "..." }
#
# We extract the description and try to infer the agent from it.
# For a simpler approach, mark a generic "active-agent" entry.

INPUT=$(cat)
DESCRIPTION=$(echo "$INPUT" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print(d.get('description','Running task...'))
except:
    print('Running task...')
" 2>/dev/null || echo "Running task...")

# Mark a generic "active-subagent" entry so the portal shows activity
/home/jared/purebrain_portal/update_agent_status.sh "active-subagent" active "$DESCRIPTION"
```

**`/home/jared/purebrain_portal/hooks/post_task_hook.sh`** — fires after a Task tool call:

```bash
#!/bin/bash
# Post-task hook: marks the subagent idle when the Task tool completes.
/home/jared/purebrain_portal/update_agent_status.sh "active-subagent" idle
```

### Named Agent Hooks (Better Approach)

For named agents (e.g., `dept-systems-technology`), Aether can call the script directly:

```bash
# At the start of an agent invocation (inside the agent's first action):
/home/jared/purebrain_portal/update_agent_status.sh dept-systems-technology active "Wiring agent lifecycle hooks"

# At the end of the agent's work:
/home/jared/purebrain_portal/update_agent_status.sh dept-systems-technology idle
```

Or call the API directly via curl:
```bash
curl -s -X POST http://localhost:8097/api/agents/status \
  -H "Content-Type: application/json" \
  -d '{"agent":"dept-systems-technology","status":"active","task":"Wiring agent lifecycle hooks"}'
```

---

## Quick Test

```bash
# Mark an agent active
curl -X POST http://localhost:8097/api/agents/status \
  -H "Content-Type: application/json" \
  -d '{"agent":"dept-accounting-finance","status":"active","task":"Testing lifecycle hooks"}'

# Verify it appears in the agent list
curl -s http://localhost:8097/api/agents \
  -H "Authorization: Bearer $(grep PORTAL_BEARER /home/jared/purebrain_portal/.env 2>/dev/null | cut -d= -f2)" \
  | python3 -c "import sys,json; agents=json.load(sys.stdin)['agents']; [print(a['id'],a['status'],a.get('current_task','')) for a in agents if a['status']!='idle']"

# Mark it idle
curl -X POST http://localhost:8097/api/agents/status \
  -H "Content-Type: application/json" \
  -d '{"agent":"dept-accounting-finance","status":"idle"}'
```

---

## Notes

- The `/api/agents/status` endpoint has no auth requirement intentionally — hook scripts run in contexts without portal bearer tokens.
- The DB migration runs automatically on server startup. No manual schema changes needed.
- New/unknown agent IDs are auto-inserted as placeholder entries so hooks never fail silently.
- The portal polls every 5 seconds only when the Agents panel is open — no background polling when it's closed.
