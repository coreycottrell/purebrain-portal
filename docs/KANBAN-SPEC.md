# 🎨 feature-designer: Kanban Board System Spec

**Agent**: feature-designer
**Domain**: AI-Human Collaboration Task Management
**Date**: 2026-04-17

---

# Kanban Board System Specification

**Status**: Ready for implementation
**Location**: `custom/panels/kanban.html` (frontend) + `custom/routes.py` (backend)
**Replaces**: Current localStorage-only kanban

---

## 1. Data Model

### Task Schema

```json
{
  "id": "kb-1713400000-a1b2c",
  "title": "Implement RankAnything landing page",
  "description": "Build the marketing landing page with pricing tiers and demo embed.",
  "status": "todo | inprogress | done | archived",
  "priority": "urgent | high | normal | low",
  "project": "RankAnything",
  "assigned_to": "human | flux2 | both",
  "created_by": "human | flux2",
  "tags": ["frontend", "launch"],
  "position": 0,
  "created_at": "2026-04-17T10:30:00Z",
  "updated_at": "2026-04-17T10:30:00Z",
  "completed_at": null,
  "notes": [
    {
      "id": "n-1713400100-x1y2z",
      "author": "flux2",
      "text": "Finished hero section, need Alex to review copy.",
      "created_at": "2026-04-17T12:00:00Z"
    }
  ]
}
```

### Field Details

| Field | Type | Required | Default | Notes |
|-------|------|----------|---------|-------|
| `id` | string | auto | `kb-{timestamp}-{rand5}` | Generated on creation |
| `title` | string | yes | -- | Max 200 chars |
| `description` | string | no | `""` | Markdown-friendly, max 2000 chars |
| `status` | enum | yes | `"todo"` | `todo`, `inprogress`, `done`, `archived` |
| `priority` | enum | no | `"normal"` | `urgent`, `high`, `normal`, `low` |
| `project` | string | no | `""` | Free-text project label |
| `assigned_to` | enum | no | `"both"` | `human`, `flux2`, `both` |
| `created_by` | enum | auto | varies | `human` (from UI), `flux2` (from API) |
| `tags` | string[] | no | `[]` | Free-text tags for filtering |
| `position` | int | auto | append-to-end | Order within column (0 = top) |
| `created_at` | ISO8601 | auto | now | Set on creation |
| `updated_at` | ISO8601 | auto | now | Updated on every write |
| `completed_at` | ISO8601 | auto | null | Set when status moves to `done` |
| `notes` | array | no | `[]` | Threaded comments from human or AI |

### Design Decisions

- **`assigned_to`**: Lets both parties see at a glance "who owns this." The AI can self-assign tasks it creates, and the human can reassign.
- **`created_by`**: Audit trail. Know whether a task came from the human's brain or the AI's initiative.
- **`position`**: Integer for ordering within a column. Enables drag-and-drop reorder without timestamp hacks.
- **`notes`**: Simple threaded comments. The AI can leave status updates; the human can reply. Avoids needing a separate comms channel for per-task discussion.
- **`archived`**: Fourth status. Done tasks accumulate fast -- archiving keeps the board clean without losing history.
- **No subtasks in v1**: Subtasks add significant complexity (recursive rendering, completion tracking). Defer to v2 if needed. For now, use description checklists (`- [ ] item`).

---

## 2. Storage

### Recommendation: JSON file

**File**: `custom/data/kanban.json`

**Why JSON over SQLite:**

| Concern | JSON file | SQLite |
|---------|-----------|--------|
| AI reads/writes programmatically | Direct `json.load/dump` | Needs sqlite3 queries |
| Human reads (debug) | Open in any editor | Need DB browser |
| Concurrent access | File lock (fcntl) | Built-in but overkill |
| Query complexity | Simple list filtering | Full SQL, not needed |
| Backup | `cp kanban.json kanban.bak` | `sqlite3 .dump` |
| Dependencies | None (stdlib) | None (stdlib), but more code |
| Task volume | Fine for < 1000 tasks | Only needed at scale |

For a personal AI-human workspace, task count will stay well under 1000. JSON is simpler, more transparent, and the AI can read/write it directly from the filesystem without going through HTTP if needed.

### File Locking Strategy

Use `fcntl.flock` for advisory locking to prevent race conditions between the portal server (handling UI requests) and any direct file writes from the AI agent:

```python
import fcntl, json
from pathlib import Path

KANBAN_FILE = Path(__file__).parent / "data" / "kanban.json"

def read_tasks():
    if not KANBAN_FILE.exists():
        return []
    with open(KANBAN_FILE, "r") as f:
        fcntl.flock(f, fcntl.LOCK_SH)
        data = json.load(f)
        fcntl.flock(f, fcntl.LOCK_UN)
    return data

def write_tasks(tasks):
    KANBAN_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(KANBAN_FILE, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        json.dump(tasks, f, indent=2)
        fcntl.flock(f, fcntl.LOCK_UN)
```

### Backup

The portal startup hook (or a cron) copies `kanban.json` to `kanban.json.bak` daily. One backup is enough -- this is a task board, not a database of record.

---

## 3. API Endpoints

All endpoints under `/api/custom/kanban/`. All require auth (`check_auth`). All return JSON.

### 3.1 List Tasks

```
GET /api/custom/kanban/tasks
```

**Query params** (all optional):
- `status` -- filter by status (`todo`, `inprogress`, `done`, `archived`)
- `project` -- filter by project name (exact match)
- `assigned_to` -- filter by assignee (`human`, `flux2`, `both`)
- `priority` -- filter by priority
- `tag` -- filter by tag (tasks containing this tag)

**Response:**
```json
{
  "tasks": [...],
  "counts": { "todo": 3, "inprogress": 2, "done": 5, "archived": 12 }
}
```

### 3.2 Create Task

```
POST /api/custom/kanban/tasks
```

**Body:**
```json
{
  "title": "Research ELO algorithms",
  "description": "Compare Glicko-2 vs classic ELO for RankAnything",
  "priority": "high",
  "project": "RankAnything",
  "assigned_to": "flux2",
  "created_by": "flux2",
  "tags": ["research"]
}
```

**Response:** `201` with full task object (id, timestamps auto-populated).

**Note on `created_by`**: The UI always sets `"human"`. The AI sets `"flux2"`. No enforcement needed -- this is trust-based, not security-critical.

### 3.3 Get Single Task

```
GET /api/custom/kanban/tasks/{id}
```

**Response:** Full task object or `404`.

### 3.4 Update Task

```
PATCH /api/custom/kanban/tasks/{id}
```

**Body:** Partial update -- only include fields to change.

```json
{
  "status": "inprogress",
  "assigned_to": "flux2"
}
```

**Response:** Updated full task object.

**Special behavior:**
- Setting `status` to `done` auto-sets `completed_at` to now.
- Setting `status` away from `done` clears `completed_at`.
- Always updates `updated_at`.

### 3.5 Delete Task

```
DELETE /api/custom/kanban/tasks/{id}
```

**Response:** `200 {"ok": true}` or `404`.

### 3.6 Move Task (Status Change)

```
POST /api/custom/kanban/tasks/{id}/move
```

**Body:**
```json
{
  "status": "inprogress",
  "position": 0
}
```

This is the drag-and-drop endpoint. Moves a task to a new column and optionally sets its position within that column. Other tasks in the target column shift down.

**Response:** Updated task object.

### 3.7 Reorder Tasks

```
POST /api/custom/kanban/reorder
```

**Body:**
```json
{
  "status": "todo",
  "task_ids": ["kb-123", "kb-456", "kb-789"]
}
```

Sets position values for all tasks in a column based on the provided order. Used when the human drags to reorder within a column.

**Response:** `200 {"ok": true}`

### 3.8 Add Note to Task

```
POST /api/custom/kanban/tasks/{id}/notes
```

**Body:**
```json
{
  "author": "flux2",
  "text": "Completed the API design. Moving to implementation."
}
```

**Response:** Updated task with new note appended.

### 3.9 Bulk Create (AI convenience)

```
POST /api/custom/kanban/tasks/bulk
```

**Body:**
```json
{
  "tasks": [
    { "title": "Task 1", "project": "X", "created_by": "flux2" },
    { "title": "Task 2", "project": "X", "created_by": "flux2" }
  ]
}
```

**Response:** `201` with array of created tasks.

This lets the AI decompose work into multiple tasks in one call.

### 3.10 Get Projects List

```
GET /api/custom/kanban/projects
```

**Response:**
```json
{
  "projects": [
    { "name": "RankAnything", "count": 5 },
    { "name": "Portal", "count": 3 }
  ]
}
```

Derived from distinct `project` values across all tasks. Useful for filter dropdowns.

---

## 4. UI Feature Set

### 4.1 Column Layout (existing, enhanced)

Keep the current three visible columns: **Todo**, **In Progress**, **Done**. Archived tasks are hidden from the board (accessible via filter).

### 4.2 Filter Bar

Add a filter bar between the header and the board:

```
[All Projects v] [All Priorities v] [All Assignees v] [tag search...] [Clear Filters]
```

- **Project dropdown**: Populated from `/api/custom/kanban/projects`. "All Projects" default.
- **Priority dropdown**: urgent/high/normal/low/all.
- **Assignee dropdown**: human/flux2/both/all. Visual indicator: human icon, robot icon, or both.
- **Tag search**: Text input that filters tasks containing the typed tag.
- **Clear Filters**: Resets all filters.

Filters apply client-side on the loaded task set (no need for server round-trips since task count is small).

### 4.3 Enhanced Task Cards

Current cards show title, description, priority badge, and project badge. Add:

- **Assignee indicator**: Small icon in card meta row -- person icon for human, robot icon for flux2, handshake icon for both.
- **Note count badge**: If task has notes, show a small speech-bubble icon with count.
- **Age indicator**: Subtle text like "2d" (2 days old) on cards in Todo/InProgress to surface stale tasks.
- **Created-by indicator**: Tiny "H" or "F" badge showing who created the task.

### 4.4 Task Detail View

Clicking a card opens an expanded modal (or slide-out panel) showing:

- All task fields (editable)
- **Notes thread**: Chronological list of notes with author and timestamp. Add-note input at the bottom.
- **Activity log**: "Created by flux2 on Apr 17" / "Moved to In Progress on Apr 18" (derived from timestamps).

### 4.5 Quick Add

The existing "+ Add Task" button opens the modal. Enhance:

- **Keyboard shortcut**: `n` key (when not in an input field) opens the add modal.
- **Quick-add from top**: A persistent slim input at the top of the Todo column -- type a title, press Enter, task created with defaults. For rapid capture.

### 4.6 Drag and Drop (existing, enhanced)

Current drag-and-drop moves tasks between columns. Enhance:

- **Within-column reorder**: Drag to reposition within the same column (calls `/reorder`).
- **Drop position indicator**: Show a line/gap where the card will land, not just highlight the entire column.

### 4.7 Assignee Modal Field

Add to the task create/edit modal:

```html
<label class="kb-label">Assigned To</label>
<select class="kb-input" id="kb-task-assigned">
  <option value="both" selected>Both</option>
  <option value="human">Alex (Human)</option>
  <option value="flux2">Flux2 (AI)</option>
</select>
```

### 4.8 Tags Input

Add a tags field to the modal. Simple comma-separated input:

```html
<label class="kb-label">Tags</label>
<input type="text" class="kb-input" id="kb-task-tags"
       placeholder="e.g. frontend, urgent, research (comma-separated)">
```

Display as small pill badges on cards.

### 4.9 Archive Controls

- **Auto-archive**: Tasks in Done for more than 7 days auto-move to archived (configurable).
- **Manual archive**: "Archive" button on done cards (or in card actions).
- **View archived**: Toggle in filter bar to show/hide archived column.

### 4.10 Board Stats Footer

Subtle footer below the board:

```
12 tasks total | 3 by Alex, 9 by Flux2 | Oldest open: 5d
```

Quick health check on the collaboration.

---

## 5. Visual Design

### Color Palette (fits dark portal theme)

| Element | Color | Notes |
|---------|-------|-------|
| Todo header | `rgba(100,100,120,0.25)` | Existing neutral gray |
| In Progress header | `rgba(42,147,193,0.15)` | Existing teal |
| Done header | `rgba(34,197,94,0.1)` | Existing green |
| Urgent priority | `#ef4444` red | Existing |
| High priority | `#f97316` orange | Existing |
| Human assignee | `#60a5fa` blue | Person icon |
| Flux2 assignee | `#a78bfa` purple | Robot icon |
| Both assignee | `var(--gold)` | Handshake icon |
| Tag pills | `rgba(255,255,255,0.08)` bg, `var(--text-dim)` text | Subtle |

### Assignee Icons (Unicode, no images needed)

- Human: `&#x1F464;` (bust silhouette)
- Flux2: `&#x1F916;` (robot face)
- Both: `&#x1F91D;` (handshake)

---

## 6. AI Integration Patterns

### How Flux2 Uses the Kanban

The AI interacts with the kanban via HTTP API. Typical workflows:

**Session start** -- AI reads the board to understand current work:
```
GET /api/custom/kanban/tasks?status=todo&status=inprogress
```

**Task decomposition** -- AI breaks a project into tasks:
```
POST /api/custom/kanban/tasks/bulk
{ "tasks": [
    { "title": "Design API schema", "project": "RankAnything", "assigned_to": "flux2", "created_by": "flux2" },
    { "title": "Review landing page copy", "project": "RankAnything", "assigned_to": "human", "created_by": "flux2" },
    ...
]}
```

**Progress updates** -- AI moves its tasks and leaves notes:
```
POST /api/custom/kanban/tasks/kb-123/move
{ "status": "done" }

POST /api/custom/kanban/tasks/kb-123/notes
{ "author": "flux2", "text": "API schema complete. See /docs/api-schema.md" }
```

**Direct file access** -- In cases where HTTP is unavailable (e.g., during agent startup), the AI can read/write `custom/data/kanban.json` directly. The file format is the single source of truth.

---

## 7. Migration from localStorage

### Strategy: One-time import on first server load

When the frontend loads and detects localStorage data exists but the server has no tasks:

1. Read `localStorage.getItem('flux2_kanban_tasks')`
2. POST each task to `/api/custom/kanban/tasks/bulk` with `created_by: "human"`
3. On success, remove `localStorage.getItem('flux2_kanban_tasks')`
4. Show a brief toast: "Tasks migrated to server storage"

This preserves any tasks Alex has already created.

---

## 8. Implementation Checklist

### Backend (routes.py additions)

- [ ] `kanban_storage.py` -- Read/write helpers with file locking
- [ ] `GET /api/custom/kanban/tasks` -- List with filters
- [ ] `POST /api/custom/kanban/tasks` -- Create single
- [ ] `GET /api/custom/kanban/tasks/{id}` -- Get single
- [ ] `PATCH /api/custom/kanban/tasks/{id}` -- Update partial
- [ ] `DELETE /api/custom/kanban/tasks/{id}` -- Delete
- [ ] `POST /api/custom/kanban/tasks/{id}/move` -- Status + position change
- [ ] `POST /api/custom/kanban/reorder` -- Column reorder
- [ ] `POST /api/custom/kanban/tasks/{id}/notes` -- Add note
- [ ] `POST /api/custom/kanban/tasks/bulk` -- Bulk create
- [ ] `GET /api/custom/kanban/projects` -- Project list
- [ ] Create `custom/data/` directory with empty `kanban.json` (`[]`)

### Frontend (kanban.html changes)

- [ ] Replace localStorage calls with fetch() to API endpoints
- [ ] Add filter bar UI
- [ ] Add assignee field to modal
- [ ] Add tags field to modal
- [ ] Add assignee/tag/note-count badges to cards
- [ ] Add notes thread to detail view
- [ ] Add within-column drag reorder
- [ ] Add localStorage migration logic
- [ ] Add quick-add input to Todo column header
- [ ] Add keyboard shortcut (`n` for new task)
- [ ] Add board stats footer

### File Structure

```
custom/
  routes.py              # Add kanban routes to existing routes list
  data/
    kanban.json           # Task data (auto-created, gitignored)
    kanban.json.bak       # Daily backup
  panels/
    kanban.html           # Updated panel
```

---

## 9. Future Considerations (v2)

These are explicitly out of scope for v1 but worth noting:

- **Subtasks**: Nested task trees with completion rollup
- **Due dates**: Calendar integration, overdue highlighting
- **Recurring tasks**: Templates for daily/weekly standup items
- **Board views**: List view, timeline view, calendar view
- **Notifications**: Toast when the AI creates/completes a task while the portal is open (WebSocket or polling)
- **Task templates**: Pre-filled task structures for common work types
- **Export**: CSV/markdown export of tasks

---

**End of specification. Ready for implementation.**
