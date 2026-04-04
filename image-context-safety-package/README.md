# Image Context Safety Package

## What This Is

A 4-layer defense-in-depth solution that prevents Claude Code sessions from crashing due to image context overflow.

## The Problem

When Claude Code agents use the `Read` tool on image files (.png, .jpg, etc.), the image data accumulates in the conversation context. After 2-3 images, Claude hits:

```
"image exceeds dimension limit for many-image requests (2000px)"
```

This error crashes the entire session, losing all work in progress.

## The 4-Layer Solution

| Layer | File | What It Does |
|-------|------|--------------|
| 1. PreToolUse Hook | `warn-image-read.sh` | Automatically blocks Read tool from loading images |
| 2. Cron Cleanup | `cleanup-context-images.sh` | Removes stale /tmp images every 30 min |
| 3. Settings Config | `settings-snippet.json` | Registers the hook in Claude Code |
| 4. CLAUDE.md Rules | `claude-md-snippet.md` | Teaches agents the behavioral rules |

## Quick Install

From within the project directory (e.g., the portal root):

```bash
bash image-context-safety-package/install.sh --civ-root /home/aiciv
```

Or for a portal deployment:

```bash
bash image-context-safety-package/install.sh --civ-root /path/to/portal
```

## Portal Integration

This package is included in the portal repo so every new portal deployment can activate it.
See `PORTAL-SETUP-GUIDE.md` Step 6 for integration instructions.

## Origin

Created by Aether CIV (Pure Technology) after discovering the dimension limit crash during browser-vision-tester workflows. April 2026. Integrated into purebrain-portal by Flux2.
