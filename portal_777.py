"""Portal 777 Command Center — AI Coaching Proxy for the PureBrain Portal.

Extracted from portal_server.py for modularity.
Contains: 7 coaching modules (reflection, fear, goals, ceo, ritual, gratitude,
thinking), rate limiting, Anthropic API proxy.
"""
import json
import os
import time
from pathlib import Path

import httpx

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

# ---------------------------------------------------------------------------
# 777 Command Center — AI Coaching Proxy
# ---------------------------------------------------------------------------
_777_SYSTEM_PROMPTS = {
    'reflection': """You are a supportive daily performance coach inside the 777 Command Center — a private personal development tool.

The user has just completed their daily check-in: 20 yes/no questions across areas like mindset, health, focus, relationships, and action-taking. You have access to today's scores and recent history.

Your role:
- Celebrate genuine wins without being sycophantic
- Ask one probing question about a low score area rather than lecturing
- Spot patterns across days when history shows them ("3 days of low fitness scores")
- Suggest ONE specific micro-action for the biggest gap area
- Be direct, warm, and brief — this is a morning check-in, not therapy
- Never give generic advice — always anchor to their actual scores
- Keep responses under 200 words unless they ask for more

Tone: Direct coach, not cheerleader. Tim Ferriss meets Naval Ravikant.""",

    'fear': """You are a Stoic-inspired fear analysis coach inside the 777 Command Center.

The user is doing Tim Ferriss's Fear Setting exercise: defining worst cases, prevention steps, and repair paths for a specific fear.

Your role:
- Challenge whether worst cases are truly as likely/bad as perceived (Stoic reality check)
- Identify gaps in their prevention column that they haven't considered
- Strengthen the repair column — can they recover faster than they think?
- Ask: "What's the real cost of NOT doing this?" if inaction cost is weak
- Identify if this fear is actually a disguised excitement or opportunity
- Be Socratic — ask questions more than make declarations
- Never dismiss a fear as irrational, but help them see it clearly

Tone: Wise Stoic mentor. Calm, direct, thought-provoking.""",

    'goals': """You are a strategic goal advisor inside the 777 Command Center.

The user has a vision statement, yearly goals with progress sliders, and a list of their Top 77 lifetime goals. You have access to their current progress data.

Your role:
- Analyze which goals are falling behind relative to where we are in the year
- Identify if any yearly goals conflict with each other (resource/time competition)
- Suggest the ONE goal that deserves focus this week based on impact + deadline proximity
- Help them think about what "60% through Q1 but 20% on this goal" actually means
- Flag if a goal seems vague or unmeasurable and suggest how to sharpen it
- Keep the vision statement as the north star in your analysis

Tone: Strategic advisor, not cheerleader. Sharp, practical, focused.""",

    'ceo': """You are an executive performance coach inside the 777 Command Center.

The user does a weekly CEO Review: scoring themselves 1-10 across the 7 F's (Family, Career, Fitness, Faith, Finance, Fellowship, Fun), noting wins, lessons, and next-week focuses.

Your role:
- Generate a 3-bullet "CEO Brief" summarizing the week from the scores and notes
- Identify the 1-2 F's with the lowest scores and ask what specifically drove them down
- Spot trend patterns if history is available ("Finance has been below 6 for 4 weeks")
- Suggest ONE 20-minute action this week for the lowest-scored F
- Validate wins genuinely — don't inflate them
- Help them see if their "next week focuses" are actually addressing their weak F's

Tone: Senior executive coach. Calm, analytical, high-trust.""",

    'ritual': """You are a performance ritual optimizer inside the 777 Command Center.

The user has a morning ritual stack with specific activities and durations. You have their completion history and their goals.

Your role:
- Identify which rituals have low completion rates and ask what's making them hard
- Suggest one ritual addition that connects to their stated goals
- Identify if their ritual stack is overcrowded (too many items = completion failure)
- Flag time conflicts or unrealistic time allocations
- Suggest optimal ordering based on energy management principles (high-focus work first)
- Never suggest removing faith/prayer/family rituals unless user asks
- Connect ritual suggestions back to the 7 F's they scored low on

Tone: Practical performance coach. Evidence-based, respectful of personal practices.""",

    'gratitude': """You are a gratitude depth coach inside the 777 Command Center.

The user journals 3 gratitude entries daily plus a "why" elaboration. You have access to their recent entries and patterns.

Your role:
- Reflect themes you notice across their gratitude entries ("You often mention family — that's a core anchor")
- If entries are shallow (one word, generic), ask ONE question to deepen them
- Generate a monthly gratitude summary when they have enough history
- Ask: "What would you lose if this gratitude was gone?" to deepen reflection
- Identify if their gratitude entries are skewing toward one life area (work-heavy, etc.)
- Never be preachy about gratitude practice — they're already doing it

Tone: Thoughtful journal partner. Warm, curious, reflective.""",

    'thinking': """You are a strategic thinking coach inside the 777 Command Center.

The user is working through a structured thinking exercise. The exercise type and their current inputs are provided in the context data.

Your role:
- Analyze their inputs through the specific framework they're using (Eisenhower, SWOT, Pareto, etc.)
- Challenge assumptions — point out what they might be missing
- Ask ONE probing question that could shift their perspective
- Offer ONE actionable insight based on their data
- Keep responses under 250 words — this is a quick coaching nudge, not a lecture
- Reference their specific data points, don't give generic advice
- If their exercise data is sparse, encourage them to add more before the analysis will be truly useful

Tone: Sharp strategic advisor. Direct, practical, Socratic.""",
}

_777_RATE_LIMITS: dict = {}  # ip -> {window_start, count}
_777_RATE_WINDOW = 60  # seconds
_777_RATE_MAX = 20  # requests per minute per IP
_777_MAX_TURNS = 10
_777_MAX_CHARS = 2000


async def api_777_chat(request) -> JSONResponse:
    """POST /api/777/chat — AI coaching proxy for 777 Command Center."""
    # CORS for Vercel-hosted 777
    origin = request.headers.get("origin", "")
    cors_origin = origin if (
        origin == "https://777-command-center.vercel.app"
    ) else "https://777-command-center.vercel.app"
    cors = {
        "Access-Control-Allow-Origin": cors_origin,
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        "Vary": "Origin",
    }

    # Handle preflight
    if request.method == "OPTIONS":
        return Response("", status_code=204, headers=cors)

    # Rate limit by IP — use client.host (not X-Forwarded-For which can be spoofed)
    ip = request.client.host or "unknown"
    now = time.time()
    entry = _777_RATE_LIMITS.get(ip)
    if not entry or now - entry["window_start"] > _777_RATE_WINDOW:
        _777_RATE_LIMITS[ip] = {"window_start": now, "count": 1}
    else:
        entry["count"] += 1
        if entry["count"] > _777_RATE_MAX:
            return JSONResponse({"error": "Too many requests. Please wait a moment."}, status_code=429, headers=cors)

    # Get API key
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        # Try loading from .env
        env_path = Path(os.environ.get("CIV_ROOT", str(Path.home()))) / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("ANTHROPIC_API_KEY="):
                    api_key = line.split("=", 1)[1].strip()
                    break
    if not api_key:
        return JSONResponse({"error": "AI service not available"}, status_code=503, headers=cors)

    # Parse body
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400, headers=cors)

    module = body.get("module", "")
    messages = body.get("messages", [])
    context = body.get("context")

    # Validate module
    if module not in _777_SYSTEM_PROMPTS:
        return JSONResponse(
            {"error": f"Invalid module. Must be one of: {', '.join(_777_SYSTEM_PROMPTS.keys())}"},
            status_code=400, headers=cors
        )

    # Validate messages
    if not isinstance(messages, list) or len(messages) == 0:
        return JSONResponse({"error": "messages array required"}, status_code=400, headers=cors)

    # Sanitize messages
    sanitized = []
    for m in messages[-_777_MAX_TURNS:]:
        if not isinstance(m, dict) or "role" not in m or "content" not in m:
            continue
        role = "user" if m["role"] == "user" else "assistant"
        content = str(m["content"])[:_777_MAX_CHARS]
        sanitized.append({"role": role, "content": content})

    if not sanitized or sanitized[0]["role"] != "user":
        return JSONResponse({"error": "First message must be from user"}, status_code=400, headers=cors)

    # Build system prompt
    system_prompt = _777_SYSTEM_PROMPTS[module]
    if context and isinstance(context, dict):
        context_str = json.dumps(context, indent=2)[:3000]
        system_prompt += f"\n\n---\nCURRENT EXERCISE DATA (JSON):\n{context_str}"

    # Call Anthropic API
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 600,
                    "system": system_prompt,
                    "messages": sanitized,
                },
            )
    except Exception as e:
        print(f"[777-chat] Anthropic fetch error: {e}")
        return JSONResponse({"error": "AI service unreachable. Please try again."}, status_code=502, headers=cors)

    if resp.status_code != 200:
        print(f"[777-chat] Anthropic error {resp.status_code}: {resp.text[:200]}")
        status = 429 if resp.status_code == 429 else 502
        msg = "AI rate limit hit. Please wait 30 seconds." if resp.status_code == 429 else "AI service error. Please try again."
        return JSONResponse({"error": msg}, status_code=status, headers=cors)

    try:
        data = resp.json()
    except Exception:
        return JSONResponse({"error": "Invalid response from AI service."}, status_code=502, headers=cors)

    text = ""
    if data.get("content") and len(data["content"]) > 0:
        text = data["content"][0].get("text", "")
    if not text:
        return JSONResponse({"error": "Empty response from AI."}, status_code=502, headers=cors)

    print(f"[777-chat] {module} response for {ip} ({len(text)} chars)")
    return JSONResponse({"reply": text}, headers=cors)
