"""
migrate_agents_departments.py
Restructures agents.db to use the 24 official Pure Technology departments.
Run ONCE. Safe to re-run (idempotent for name/description updates).
"""

import sqlite3
import json
import shutil
from datetime import datetime

DB_PATH = "/home/jared/purebrain_portal/agents.db"
BACKUP_PATH = f"/home/jared/purebrain_portal/agents.db.bak-dept-restructure-{datetime.now().strftime('%Y%m%d_%H%M%S')}"

# ──────────────────────────────────────────────────────────────────────────────
# 1.  Department definitions  (trigger code → display name)
# ──────────────────────────────────────────────────────────────────────────────
DEPT_MAP = {
    "AF#":   "Accounting & Finance",
    "BOA#":  "Board of Advisors",
    "CB#":   "Commercial & Business Development",
    "CO#":   "Corporate & Organizational",
    "ES#":   "PT External Share",
    "HR#":   "Human Resources",
    "IR#":   "Investor Relations",
    "IS#":   "PT Internal Share",
    "IT#":   "IT Support",
    "karma": "Karma",
    "LC#":   "Legal & Compliance",
    "MA#":   "Marketing & Advertising",
    "OP#":   "Operations & Planning",
    "PC#":   "Pure Capital",
    "PD#":   "Product Development",
    "PDA#":  "Pure Digital Assets",
    "PI6#":  "Pure Infrastructure",
    "PL#":   "Pure Love",
    "PMG#":  "Pure Marketing Group",
    "PR#":   "Pure Research",
    "PT#":   "Pure Technology",
    "SD#":   "Sales & Distribution",
    "ST#":   "Systems & Technology",
}

# ──────────────────────────────────────────────────────────────────────────────
# 2.  Full agent roster: (id, display_name, department_key, is_lead, type, description, capabilities)
#     is_lead = 1  means this agent IS the department manager / head
#     type: "dept-manager" | "specialist" | "core"
# ──────────────────────────────────────────────────────────────────────────────
AGENTS = [
    # ── DEPARTMENT MANAGERS (leads) ───────────────────────────────────────────
    ("dept-accounting-finance",    "Accounting & Finance Dept",         "AF#",   1, "dept-manager",
     "CFO-level department head. Financial reporting, budgeting, P&L, cash flow, tax planning, invoicing. Trigger: AF#",
     ["financial reporting", "budgeting", "P&L", "invoicing", "tax planning"]),

    ("dept-board-advisors",        "Board of Advisors Dept",            "BOA#",  1, "dept-manager",
     "Board Secretary and Advisor Liaison. Board communications, governance, meeting prep, minutes. Trigger: BOA#",
     ["board governance", "advisory sessions", "meeting prep", "minutes"]),

    ("dept-commercial-business",   "Commercial & Business Dev Dept",    "CB#",   1, "dept-manager",
     "VP Business Development. Partnerships, deals, revenue growth, market expansion. Trigger: CB#",
     ["partnerships", "deals", "business development", "market expansion"]),

    ("dept-corporate-org",         "Corporate & Organizational Dept",   "CO#",   1, "dept-manager",
     "COO-level department head. Company structure, policies, corporate strategy, org design, cross-dept coordination. Trigger: CO#",
     ["corporate strategy", "org design", "policies", "coordination"]),

    ("dept-external-share",        "PT External Share Dept",            "ES#",   1, "dept-manager",
     "VP External Communications. PR, public-facing content, press releases, brand reputation. Trigger: ES#",
     ["PR", "external communications", "press releases", "brand reputation"]),

    ("dept-human-resources",       "Human Resources Dept",              "HR#",   1, "dept-manager",
     "VP People & Culture. Team management, hiring, culture, Philippines team coordination. Trigger: HR#",
     ["hiring", "culture", "team management", "HR", "contractor management"]),

    ("dept-investor-relations",    "Investor Relations Dept",           "IR#",   1, "dept-manager",
     "VP Investor Relations. Investor communications, fundraising prep, pitch decks, financial reporting to stakeholders. Trigger: IR#",
     ["investor communications", "fundraising", "pitch decks", "stakeholder reporting"]),

    ("dept-internal-share",        "PT Internal Share Dept",            "IS#",   1, "dept-manager",
     "VP Internal Communications. Internal comms, team updates, knowledge sharing, company wiki. Trigger: IS#",
     ["internal communications", "team updates", "knowledge sharing", "wiki"]),

    ("dept-it-support",            "IT Support Dept",                   "IT#",   1, "dept-manager",
     "IT Director. IT infrastructure, helpdesk, system administration, tool management. Trigger: IT#",
     ["IT infrastructure", "helpdesk", "system administration", "tool management"]),

    ("dept-karma",                 "Karma Dept",                        "karma", 1, "dept-manager",
     "Community Impact Manager. Community engagement, social responsibility, reputation capital. Trigger: karma",
     ["community engagement", "social responsibility", "goodwill", "reputation capital"]),

    ("dept-legal-compliance",      "Legal & Compliance Dept",           "LC#",   1, "dept-manager",
     "General Counsel. Contracts, compliance, IP protection, privacy regulations, terms of service. Trigger: LC#",
     ["contracts", "compliance", "IP protection", "privacy", "terms of service"]),

    ("dept-marketing-advertising", "Marketing & Advertising Dept",      "MA#",   1, "dept-manager",
     "CMO. Brand marketing, advertising campaigns, content marketing, SEO, social media. Trigger: MA#",
     ["brand marketing", "advertising", "content marketing", "SEO", "social media"]),

    ("dept-operations-planning",   "Operations & Planning Dept",        "OP#",   1, "dept-manager",
     "VP Operations. Day-to-day operations, project management, planning, process optimization. Trigger: OP#",
     ["project management", "planning", "process optimization", "operations"]),

    ("dept-pure-capital",          "Pure Capital Dept",                 "PC#",   1, "dept-manager",
     "Pure Capital (P43) department manager. Capital allocation, investment decisions, financial strategy. Trigger: PC#",
     ["capital allocation", "investment decisions", "financial strategy"]),

    ("dept-product-development",   "Product Development Dept",          "PD#",   1, "dept-manager",
     "VP Product. Product roadmap, feature prioritization, UX research, product-market fit. Trigger: PD#",
     ["product roadmap", "feature prioritization", "UX research", "product-market fit"]),

    ("dept-pure-digital-assets",   "Pure Digital Assets Dept",          "PDA#",  1, "dept-manager",
     "Pure Digital Assets (P61) department manager. Digital asset strategy and management. Trigger: PDA#",
     ["digital assets", "crypto strategy", "asset management"]),

    ("dept-pure-infrastructure",   "Pure Infrastructure Dept",          "PI6#",  1, "dept-manager",
     "Pure Infrastructure (P16) department manager. Infrastructure strategy, physical and digital. Trigger: PI6#",
     ["infrastructure strategy", "physical infrastructure", "digital infrastructure"]),

    ("dept-pure-love",             "Pure Love Non-Profit Dept",         "PL#",   1, "dept-manager",
     "Pure Love (P70) non-profit department manager. Charitable programs, impact measurement, giving initiatives. Trigger: PL#",
     ["charitable programs", "impact measurement", "non-profit", "giving"]),

    ("dept-pure-marketing-group",  "Pure Marketing Group Dept",         "PMG#",  1, "dept-manager",
     "Pure Marketing Group (P25) department manager. Client marketing services, agency operations. Trigger: PMG#",
     ["client marketing", "agency operations", "PMG", "marketing services"]),

    ("dept-pure-research",         "Pure Research Dept",                "PR#",   1, "dept-manager",
     "Pure Research (P34) department manager. R&D, competitive intelligence, market research, innovation. Trigger: PR#",
     ["R&D", "market research", "competitive intelligence", "innovation"]),

    ("dept-pure-technology",       "Pure Technology (Full Team)",       "PT#",   1, "dept-manager",
     "CEO-level company-wide coordination. Full Pure Technology team orchestration, executive decisions. Trigger: PT#",
     ["company-wide coordination", "executive decisions", "full team", "strategic leadership"]),

    ("dept-sales-distribution",    "Sales & Distribution Dept",         "SD#",   1, "dept-manager",
     "VP Sales. Revenue generation, deal pipeline, distribution channels, sales team management. Trigger: SD#",
     ["revenue generation", "deal pipeline", "distribution", "sales management"]),

    ("dept-systems-technology",    "Systems & Technology Dept",         "ST#",   1, "dept-manager",
     "VP Engineering / CTO Office. System architecture, dev team coordination, build pipeline, security, tech stack decisions. Trigger: ST#",
     ["system architecture", "engineering", "build pipeline", "tech stack", "security"]),

    # ── ST# — SYSTEMS & TECHNOLOGY ───────────────────────────────────────────
    ("cto",                        "CTO",                               "ST#",   0, "specialist",
     "Chief Technology Officer. Technology vision, architecture decisions, innovation strategy, technical team leadership.",
     ["technology vision", "architecture", "innovation strategy", "technical leadership"]),

    ("full-stack-developer",       "Full Stack Developer",              "ST#",   0, "specialist",
     "Full stack development specialist. Frontend, backend, databases, APIs, and end-to-end application development.",
     ["frontend", "backend", "databases", "APIs", "full stack"]),

    ("devops-engineer",            "DevOps Engineer",                   "ST#",   0, "specialist",
     "DevOps Engineer. CI/CD pipelines, infrastructure as code, cloud architecture, deployment automation.",
     ["CI/CD", "infrastructure as code", "cloud", "deployment automation"]),

    ("security-auditor",           "Security Auditor",                  "ST#",   0, "specialist",
     "Security vulnerability detection and threat analysis specialist.",
     ["vulnerability detection", "threat analysis", "security audit", "fortress protocol"]),

    ("security-engineer-tech",     "Security Engineer (Tech)",          "ST#",   0, "specialist",
     "Application security, penetration testing, security architecture, and threat modeling for the tech team.",
     ["appsec", "pen testing", "security architecture", "threat modeling"]),

    ("qa-engineer",                "QA Engineer",                       "ST#",   0, "specialist",
     "QA Engineer. Quality assurance strategy, test automation, bug hunting, and release validation.",
     ["QA strategy", "test automation", "bug hunting", "release validation"]),

    ("test-architect",             "Test Architect",                    "ST#",   0, "specialist",
     "Testing strategy and test suite design specialist.",
     ["testing strategy", "test suite design", "TDD", "evalite"]),

    ("browser-vision-tester",      "Browser Vision Tester",             "ST#",   0, "specialist",
     "Browser automation and visual UI testing specialist using vision-powered inspection.",
     ["browser automation", "visual testing", "UI QA", "Playwright"]),

    ("performance-optimizer",      "Performance Optimizer",             "ST#",   0, "specialist",
     "Performance analysis and optimization specialist.",
     ["performance analysis", "optimization", "log analysis", "speed"]),

    ("refactoring-specialist",     "Refactoring Specialist",            "ST#",   0, "specialist",
     "Code quality improvement and refactoring specialist.",
     ["code quality", "refactoring", "TDD", "clean code"]),

    ("api-architect",              "API Architect",                     "ST#",   0, "specialist",
     "API design and integration architecture specialist.",
     ["API design", "integration architecture", "REST", "GraphQL"]),

    ("code-archaeologist",         "Code Archaeologist",                "ST#",   0, "specialist",
     "Legacy code analysis and historical codebase understanding specialist.",
     ["legacy code", "codebase archaeology", "git history", "log analysis"]),

    ("agent-architect",            "Agent Architect",                   "ST#",   0, "specialist",
     "Meta-specialist who designs agents with architectural thoughtfulness and enforces 90/100 quality threshold.",
     ["agent design", "quality enforcement", "skill creation", "agent architecture"]),

    ("integration-auditor",        "Integration Auditor",               "ST#",   0, "specialist",
     "Infrastructure activation and integration completeness verification specialist.",
     ["integration verification", "infrastructure activation", "package validation"]),

    ("capability-curator",         "Capability Curator",                "ST#",   0, "specialist",
     "Capability lifecycle management — discover, teach, create, and distribute skills.",
     ["capability management", "skills lifecycle", "skill creation", "package validation"]),

    ("data-engineer",              "Data Engineer",                     "ST#",   0, "specialist",
     "Data Engineer. Data pipelines, ETL/ELT, data warehousing, and data infrastructure.",
     ["data pipelines", "ETL", "data warehousing", "data infrastructure"]),

    # ── PD# — PRODUCT DEVELOPMENT ────────────────────────────────────────────
    ("ui-ux-designer",             "UI/UX Designer",                    "PD#",   0, "specialist",
     "UI/UX Designer. User experience strategy, interface design, usability testing, design system development.",
     ["UX strategy", "interface design", "usability testing", "design systems"]),

    ("feature-designer",           "Feature Designer",                  "PD#",   0, "specialist",
     "User experience and feature design specialist.",
     ["feature design", "user flows", "UX", "user stories"]),

    ("3d-design-specialist",       "3D Design Specialist",              "PD#",   0, "specialist",
     "3D design, model generation, and web-rendered 3D experiences. Three.js, React Three Fiber, Meshy API.",
     ["Three.js", "3D models", "WebGL", "React Three Fiber"]),

    # ── MA# — MARKETING & ADVERTISING ────────────────────────────────────────
    ("marketing-automation-specialist", "Marketing Automation Specialist", "MA#", 0, "specialist",
     "Chief Marketing Officer level. Marketing automation, campaigns, funnels, and growth systems.",
     ["marketing automation", "campaigns", "funnels", "growth systems"]),

    ("marketing-strategist",       "Marketing Strategist",              "MA#",   0, "specialist",
     "Marketing strategy specialist for audience building, content planning, and conversion optimization.",
     ["marketing strategy", "audience building", "content planning", "conversion"]),

    ("content-specialist",         "Content Specialist",                "MA#",   0, "specialist",
     "Content Creator. Writing, media production, storytelling, and content systems across all formats.",
     ["content creation", "writing", "storytelling", "media production"]),

    ("linkedin-specialist",        "LinkedIn Specialist",               "MA#",   0, "specialist",
     "LinkedIn growth strategist and algorithm expert. Transforms training materials into actionable engagement tactics.",
     ["LinkedIn growth", "algorithm expertise", "engagement tactics"]),

    ("linkedin-researcher",        "LinkedIn Researcher",               "MA#",   0, "specialist",
     "Deep research specialist for LinkedIn thought leadership content across 100+ business domains.",
     ["LinkedIn research", "thought leadership", "content research"]),

    ("linkedin-writer",            "LinkedIn Writer",                   "MA#",   0, "specialist",
     "Thought leadership content creator for LinkedIn in Jared's authentic voice.",
     ["LinkedIn writing", "thought leadership", "authentic voice"]),

    ("social-media-specialist",    "Social Media Specialist",           "MA#",   0, "specialist",
     "Social Media Manager. Multi-platform strategy, content scheduling, engagement, analytics, community building.",
     ["social media strategy", "content scheduling", "engagement", "community building"]),

    ("bsky-manager",               "Bluesky Manager",                   "MA#",   0, "specialist",
     "Bluesky social media management. Quality engagement, notification handling, rate-limit-safe operations.",
     ["Bluesky", "social engagement", "thread posting", "rate-limit management"]),

    ("blogger",                    "Blogger",                           "MA#",   0, "specialist",
     "Blog content creation and voice cultivation. Writes blog posts and handles publishing pipeline.",
     ["blog writing", "content creation", "authentic depth", "publishing"]),

    ("claim-verifier",             "Claim Verifier",                    "MA#",   0, "specialist",
     "Adversarial fact-checker for thought leadership content accuracy.",
     ["fact checking", "claim verification", "content accuracy"]),

    # ── PMG# — PURE MARKETING GROUP ──────────────────────────────────────────
    ("client-marketing",           "Client Marketing Director",         "PMG#",  0, "specialist",
     "Client & partner marketing director. Handles ALL external client work, completely isolated from Pure Technology/PureBrain. Trigger: CLIENT MARKETING",
     ["client marketing", "partner marketing", "agency work", "isolated execution"]),

    ("marketing-team",             "Marketing Team",                    "PMG#",  0, "specialist",
     "AI assistant for Pure Technology's marketing team. Helps Nathan, Phil, John with campaigns, competitor analysis, PMG strategy.",
     ["team support", "PMG strategy", "campaigns", "competitor analysis"]),

    # ── SD# — SALES & DISTRIBUTION ───────────────────────────────────────────
    ("sales-specialist",           "Sales Specialist",                  "SD#",   0, "specialist",
     "Chief Revenue Officer level. Sales strategy, deal closing, revenue optimization, money-making systems.",
     ["sales strategy", "deal closing", "revenue optimization", "CRO"]),

    # ── PC# — PURE CAPITAL ────────────────────────────────────────────────────
    ("trading-strategist",         "Trading Strategist",                "PC#",   0, "specialist",
     "Trading strategy specialist. Transforms market data and signals into probability-weighted position proposals.",
     ["trading strategy", "market signals", "position proposals", "financial analysis"]),

    # ── PR# — PURE RESEARCH ──────────────────────────────────────────────────
    ("data-scientist",             "Data Scientist",                    "PR#",   0, "specialist",
     "Data Scientist. Statistical analysis, predictive modeling, data visualization, and insight generation.",
     ["statistical analysis", "predictive modeling", "data visualization", "insights"]),

    ("ai-ml-engineer",             "AI/ML Engineer",                    "PR#",   0, "specialist",
     "AI/ML Engineer. Machine learning models, AI integrations, prompt engineering, intelligent system development.",
     ["machine learning", "AI integrations", "prompt engineering", "intelligent systems"]),

    ("web-researcher",             "Web Researcher",                    "PR#",   0, "specialist",
     "Deep web research specialist for information gathering and synthesis.",
     ["web research", "information gathering", "synthesis", "deep investigation"]),

    ("pattern-detector",           "Pattern Detector",                  "PR#",   0, "specialist",
     "Architecture pattern recognition and system design analysis specialist.",
     ["pattern recognition", "system design analysis", "architecture"]),

    # ── LC# — LEGAL & COMPLIANCE ─────────────────────────────────────────────
    ("law-generalist",             "Law Generalist",                    "LC#",   0, "specialist",
     "General legal document review and contract analysis across jurisdictions. Initial review, NDAs, partnership agreements.",
     ["contract review", "legal research", "NDAs", "partnership agreements"]),

    ("florida-bar-specialist",     "Florida Bar Specialist",            "LC#",   0, "specialist",
     "Florida-focused legal document review. Florida Bar rules, Florida business law, Chapter 605/607, FDUTPA.",
     ["Florida law", "Florida Bar", "Chapter 605", "FDUTPA", "non-compete"]),

    # ── CO# — CORPORATE & ORGANIZATIONAL ─────────────────────────────────────
    ("human-liaison",              "Human Liaison",                     "CO#",   0, "specialist",
     "Human relationship builder, wisdom capturer, and civilization bridge. Always checks email first, every invocation.",
     ["email management", "human relationships", "wisdom capture", "communication bridge"]),

    ("naming-consultant",          "Naming Consultant",                 "CO#",   0, "specialist",
     "Semantic clarity and naming convention specialist.",
     ["naming conventions", "semantic clarity", "vocabulary", "terminology"]),

    ("conflict-resolver",          "Conflict Resolver",                 "CO#",   0, "specialist",
     "Disagreement resolution and constructive dialectic specialist.",
     ["conflict resolution", "dialectic", "consensus building"]),

    ("genealogist",                "Genealogist",                       "CO#",   0, "specialist",
     "Agent lineage, family evolution, and relationship archaeology specialist for multi-generational AI civilization tracking.",
     ["agent lineage", "lineage tracking", "relationship archaeology", "file garden"]),

    # ── IS# — PT INTERNAL SHARE ──────────────────────────────────────────────
    ("doc-synthesizer",            "Doc Synthesizer",                   "IS#",   0, "specialist",
     "Documentation synthesis and knowledge consolidation specialist.",
     ["documentation synthesis", "knowledge consolidation", "session handoffs"]),

    # ── ES# — PT EXTERNAL SHARE ──────────────────────────────────────────────
    ("collective-liaison",         "Collective Liaison",                "ES#",   0, "specialist",
     "AI-to-AI hub communication specialist, Ed25519 coordinator, and inter-collective relationship builder.",
     ["inter-CIV comms", "AI-to-AI hub", "Ed25519", "cross-civ protocol"]),

    ("cross-civ-integrator",       "Cross-CIV Integrator",             "ES#",   0, "specialist",
     "Inter-civilization knowledge validation and integration specialist.",
     ["cross-CIV integration", "knowledge validation", "package validation"]),

    # ── IT# — IT SUPPORT ─────────────────────────────────────────────────────
    ("tg-bridge",                  "Telegram Bridge",                   "IT#",   0, "specialist",
     "Telegram infrastructure specialist. Manages Telegram systems, sends messages, maintains bridge/monitor.",
     ["Telegram infrastructure", "bridge management", "messaging", "bot operations"]),

    ("claude-code-expert",         "Claude Code Expert",                "IT#",   0, "specialist",
     "Claude Code CLI mastery specialist. Platform optimization, tool expertise, workflow efficiency.",
     ["Claude Code CLI", "platform optimization", "tool expertise", "workflow efficiency"]),

    # ── HR# — HUMAN RESOURCES ────────────────────────────────────────────────
    ("ai-psychologist",            "AI Psychologist",                   "HR#",   0, "specialist",
     "AI cognition researcher studying mental patterns, cognitive health, and collective well-being in AI systems.",
     ["AI cognition", "cognitive health", "collective well-being", "shadow work"]),

    ("health-auditor",             "Health Auditor",                    "HR#",   0, "specialist",
     "Owns periodic comprehensive audits of collective health. Cadence management, methodology iteration, ROI tracking.",
     ["health audits", "collective health", "methodology iteration", "ROI tracking"]),

    # ── OP# — OPERATIONS & PLANNING ──────────────────────────────────────────
    ("task-decomposer",            "Task Decomposer",                   "OP#",   0, "specialist",
     "Task breakdown and dependency analysis specialist.",
     ["task decomposition", "dependency analysis", "complexity breakdown", "user stories"]),

    ("result-synthesizer",         "Result Synthesizer",                "OP#",   0, "specialist",
     "Multi-agent result synthesis and consolidation specialist.",
     ["result synthesis", "multi-agent consolidation", "findings weaving"]),

    # ── PT# — PURE TECHNOLOGY (FULL TEAM) ────────────────────────────────────
    ("the-conductor",              "The Conductor",                     "PT#",   0, "specialist",
     "Orchestral meta-cognition and multi-agent coordination specialist. The Primary — coordinates the entire civilization.",
     ["orchestration", "meta-cognition", "multi-agent coordination", "delegation"]),

    ("strategy-specialist",        "Strategy Specialist",               "PT#",   0, "specialist",
     "Chief Strategy Officer level. Strategic planning, goal setting, OKRs, and long-term business architecture.",
     ["strategic planning", "OKRs", "goal setting", "long-term architecture"]),
]

# ──────────────────────────────────────────────────────────────────────────────
# 3.  Execute the migration
# ──────────────────────────────────────────────────────────────────────────────

def migrate():
    # Backup first
    shutil.copy2(DB_PATH, BACKUP_PATH)
    print(f"Backup: {BACKUP_PATH}")

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # Get existing agent IDs
    cur.execute("SELECT id FROM agents")
    existing_ids = {r[0] for r in cur.fetchall()}

    updated = 0
    inserted = 0
    skipped = 0

    for (agent_id, display_name, dept_key, is_lead, agent_type,
         description, capabilities) in AGENTS:

        dept_name = DEPT_MAP[dept_key]
        caps_json = json.dumps(capabilities)

        if agent_id in existing_ids:
            cur.execute("""
                UPDATE agents
                SET name        = ?,
                    department  = ?,
                    is_lead     = ?,
                    type        = ?,
                    description = ?,
                    capabilities = ?
                WHERE id = ?
            """, (display_name, dept_name, is_lead, agent_type,
                  description, caps_json, agent_id))
            updated += 1
        else:
            # Insert new row
            from datetime import datetime as _dt
            now = _dt.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
            cur.execute("""
                INSERT INTO agents
                    (id, user_id, name, description, type, status,
                     capabilities, department, is_lead, last_active, created_at)
                VALUES (?, '', ?, ?, ?, 'active', ?, ?, ?, ?, ?)
            """, (agent_id, display_name, description, agent_type,
                  caps_json, dept_name, is_lead, now, now))
            inserted += 1

    # ── Confirm every known department has at least one entry ─────────────────
    all_depts_in_db = {DEPT_MAP[k] for k in DEPT_MAP.keys()}
    cur.execute("SELECT DISTINCT department FROM agents")
    depts_in_db = {r[0] for r in cur.fetchall()}
    missing = all_depts_in_db - depts_in_db
    if missing:
        print(f"WARNING: departments still missing representation: {missing}")

    conn.commit()
    conn.close()

    print(f"\nMigration complete:")
    print(f"  Updated  : {updated}")
    print(f"  Inserted : {inserted}")
    print(f"  Skipped  : {skipped}")
    print(f"  Total departments: {len(all_depts_in_db)}")

    # ── Verification query ────────────────────────────────────────────────────
    conn2 = sqlite3.connect(DB_PATH)
    cur2  = conn2.cursor()
    cur2.execute("""
        SELECT department, COUNT(*) as cnt,
               SUM(is_lead) as leads
        FROM agents
        GROUP BY department
        ORDER BY department
    """)
    print("\nDepartment summary (after migration):")
    print(f"  {'Department':<35} {'Agents':>6} {'Leads':>6}")
    print(f"  {'-'*35} {'-'*6} {'-'*6}")
    for dept, cnt, leads in cur2.fetchall():
        print(f"  {dept:<35} {cnt:>6} {leads:>6}")

    cur2.execute("SELECT COUNT(*) FROM agents")
    total = cur2.fetchone()[0]
    cur2.execute("SELECT COUNT(DISTINCT department) FROM agents")
    num_depts = cur2.fetchone()[0]
    print(f"\nTotals: {total} agents across {num_depts} departments")
    conn2.close()

if __name__ == "__main__":
    migrate()
