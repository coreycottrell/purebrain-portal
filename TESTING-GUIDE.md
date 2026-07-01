# PureBrain Mission Control — Testing Guide

## Access

Open the portal in your browser:
- **URL**: `https://portal.purebrain.ai/pb`
- Works on desktop and mobile browsers

## Authentication

1. When you first open the portal, a login overlay will appear
2. Paste the **access token** provided to you by the admin
3. Click **Connect** — the portal will verify and store your token locally
4. You stay logged in until you explicitly log out (Settings or browser data clear)

If your token expires or becomes invalid, the portal will prompt you to re-enter it. You can also update your token anytime in **Settings > Connection**.

## Tabs Overview

| Tab | What It Does |
|-----|-------------|
| **Chat** | Send messages to the AI agent and receive responses in real time |
| **Hub** | View agent status, context usage, BOOP cycle info, and quick actions |
| **Tasks** | Track and manage tasks assigned to the AI |
| **Files** | Browse, upload, and download files from the agent's workspace |
| **Agents** | View active sub-agents and their current status |
| **Terminal** | Direct terminal access to the agent environment |
| **Inbox** | Email and notification inbox |
| **Settings** | Theme, token management, notification preferences |
| **Constitution** | View and manage the AI's operating rules and governance |

## Known Limitations

- **CC-Chat tab**: Only visible when the Command Center bridge is connected. If the CC bridge is down, this tab will be hidden automatically.
- **Terminal**: Requires an active tmux session on the backend. May show "no session" if the agent isn't running.
- **Some features are internal-only**: Email sending, BOOP controls, and agent restart require specific backend configuration that external testers may not have.
- **AI Chat**: Requires the AI agent to be running. If the agent is offline, messages will queue but won't get responses until it's back.

## Reporting Bugs

When reporting a bug, please include:

1. **What you did** (steps to reproduce)
2. **What you expected** to happen
3. **What actually happened** (include screenshots if possible)
4. **Browser and device** (e.g., Chrome on Windows, Safari on iPhone)
5. **Any error messages** shown in the portal or browser console (F12 > Console)

Send bug reports to the admin who gave you access.

## Tips

- Use the **theme switcher** in the top-right corner (Dark, Light, Rose modes)
- The **CTX pill** in the top bar shows the AI's current context usage
- **Dock Chat** button pins the chat to the bottom of the screen while you browse other tabs
- All settings sync across devices when using the same token
