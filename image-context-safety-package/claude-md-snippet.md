### Image Context Safety (Prevents Dimension Limit Errors)

**Requirement**: NEVER use the Read tool on image files (.png, .jpg, .jpeg, .gif, .webp) during multi-step workflows.

**Why**: When multiple images accumulate in conversation context, Claude hits "image exceeds dimension limit for many-image requests (2000px)". This crashes the session.

**Rules**:
- Report image **file paths** only - let the human view images in portal or browser
- If image analysis is truly needed, do it in a **fresh sub-agent** (isolated context) via the Agent tool
- After any screenshot operation, **delete the /tmp copy** immediately
- After screenshot-heavy workflows, run: `bash tools/cleanup-context-images.sh`
- NEVER read base64 image data into context
- NEVER accumulate more than 2 images in a single conversation context

**Cleanup**: `tools/cleanup-context-images.sh` runs every 30 min via cron and can be called manually.
