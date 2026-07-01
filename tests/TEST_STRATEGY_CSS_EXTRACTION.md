# test-architect: CSS Extraction from Monolithic HTML -- Test Strategy

**Agent**: test-architect
**Step**: 3 (Test Strategy)
**Date**: 2026-05-13

---

## Memory Search Results
- Searched: existing test files in tests/, test configs, decision records in docs/
- Found: 35 existing test files (mix of Python/pytest and Node.js), existing JS test pattern in test_panel_dock.js using Node assert pattern (no framework dependencies), no jest/vitest/playwright configs
- Applying: Matching the existing Node.js test pattern (pure node, no dependencies, assert/pass/fail counters, exit code 1 on failure) as established by test_panel_dock.js

## ADR Reference
No formal ADR exists for this CSS extraction. The task is a pure structural refactor: extract the ~2218-line `<style>` block (lines 15-2232 of portal-pb-styled.html) into 5 separate CSS files under `static/css/`.

## Coverage Targets
- Unit tests: N/A (this is a structural extraction, not code logic)
- Verification tests: 100% of CSS rules accounted for
- Automated checks: 13 verification sections covering completeness, ordering, themes, dock, media queries, keyframes, selectors, and file sizes

## Test Architecture

This extraction is unique: there is no "logic" to unit test. The risk is **visual regression through lost, duplicated, or misordered CSS rules**. The strategy uses a two-phase approach:

### Phase 1: Pre-Extraction Baseline (run ONCE, before any changes)
### Phase 2: Post-Extraction Verification (run after extraction, on every iteration)

---

## Test Scripts Created

### 1. `tests/test_css_extraction_baseline.js` -- Baseline Capture

**Run**: `node tests/test_css_extraction_baseline.js`
**When**: ONCE, before the coder touches portal-pb-styled.html
**Output**:
  - `tests/css_baseline.txt` -- Raw CSS content from the `<style>` block
  - `tests/css_baseline_meta.json` -- Structured metadata (counts, selectors, vars)

**What it captures**:
- Full CSS text (189,631 bytes, 2,218 lines)
- ~2,027 total selectors (~1,830 unique)
- 22 `:root` CSS custom properties
- 141 `[data-theme="light"]` rules
- 177 `[data-theme="girly"]` rules
- 43 dock-related selectors
- 12 `@keyframes` animations
- 4 `@media` query breakpoints

### 2. `tests/test_css_extraction_verify.js` -- Post-Extraction Verification

**Run**: `node tests/test_css_extraction_verify.js`
**When**: After each extraction iteration, before opening PR
**Exit code**: 0 = pass, 1 = fail

**13 Verification Sections**:

| # | Section | What It Checks |
|---|---------|---------------|
| 1 | Prerequisites | Baseline files exist, HTML exists, all 5 CSS files exist |
| 2 | Style Block Removal | `<style>` block removed (or acceptably small if runtime styles remain) |
| 3 | Link Tag Order | 5 `<link>` tags in correct cascade order inside `<head>` |
| 4 | CSS Completeness | Concatenated CSS (in link order) matches baseline after normalization |
| 5 | No Duplicates | No rule block appears in more than one CSS file |
| 6 | Theme Coverage | themes.css has light+girly root vars, >= 90% of theme rules |
| 7 | Dock Rules | dock.css has all required dock selectors + mobile override |
| 8 | base.css Validation | Has `:root` vars, reset, grid layout, scrollbar styles |
| 9 | Media Queries | All 4 baseline `@media` queries preserved |
| 10 | @keyframes | All 12 animation keyframes preserved |
| 11 | Selector Integrity | 15 critical selectors spot-checked across files |
| 12 | File Size Sanity | No empty files, total within 5% of baseline |
| 13 | No Inline Pollution | No `<style>` blocks in `<body>` |

---

## CSS File Mapping (Guidance for Coder)

Based on analysis of the 2,218-line style block, here is the recommended content mapping:

### `static/css/base.css` (lines 16-37 approx)
- `*` reset, `box-sizing`, cursor rules
- `:root` CSS custom properties (22 variables)
- `html,body` base styles
- `body` font/background
- Scrollbar styles (`::-webkit-scrollbar*`)
- `.app` grid layout
- `.main-col` base

### `static/css/components.css` (lines 38-166, 575-645, 697-740, 746-855, etc.)
- Bookmarks bar (`.bm-*`)
- Top nav (`.topnav*`, `.ctx-*`, `.tn-*`, `.online-*`)
- Sidebar (`.sidebar*`, `.agent-hub-*`)
- Buttons, pills, badges
- Modal overlay (`.modal-*`)
- Notification bell (`.notif-*`)
- Update notification (`.update-*`)
- Theme toggle (`.theme-toggle-*`, `.settings-theme-*`)
- Upload mode modal (`#upload-mode-*`)

### `static/css/panels.css` (lines 183-575, 647-end of panels)
- Chat area (`.chat-*`, `.msg-*`, `.composer-*`, `.brain-banner`)
- CC view (`.cc-*`)
- Email detail (`.email-*`, `.inbox-*`)
- Files panel (`.files-*`, `.f-card`, `.f-*`, Google Drive `.gdrive-*`)
- Tasks panel (`.tasks-*`, `.tl-*`)
- Teams panel (`.teams-*`, `.hk`)
- Dashboard (`.dash-*`)
- Payments (`.pay-*`)
- Refer panel (`.refer-*`)
- Todo panel (`.todo-*`)
- Agent roster (`.ar-*`, `.aa-*`, `.fleet-*`)
- Agent hub (`.hub-*`)
- Constitution/governance (`.const-*`, `.gov-*`, `.deploy-*`, `.rule-*`)
- Settings area (`.settings-*`)
- Responsive media queries for panels:
  - `@media(max-width:1024px)` -- tablet/mobile layouts
  - `@media(max-width:400px)` -- very small screens
  - `@media(min-width:1025px)` -- desktop-only overrides
- Mobile tabbar (`.mobile-tabbar`, `.mob-tab*`)
- `@keyframes` used by panels (all 12 animations)

### `static/css/dock.css` (lines 186-208 + scattered dock selectors)
- Dock state rules (`.app.chat-docked`, `.app.chat-docked.dock-expanded`)
- Dock buttons (`.dock-btn*`)
- Resize handle (`.dock-resize*`)
- Topnav dock toggle (`.topnav-dock*`)
- Dock mobile disable (the `@media(max-width:1024px)` block that sets `display:none` on dock elements)
- NOTE: Dock-related `@media` rules inside the main 1024px block (lines 1758-1762) should also go here

### `static/css/themes.css` (lines 61-67, 281-331, 1555-1605, 1672-1677, 1817-2068, 2200-2231)
- `[data-theme="light"]` root variable block + all light overrides
- `[data-theme="girly"]` root variable block + all girly overrides
- Theme-specific canvas styles (`.binary-rain-canvas`, `.rose-bloom-canvas`)
- Theme dropdown styles (`.theme-dropdown-*`)
- All scattered theme rules from component sections (lines 61-67, 281-331, etc.)
- Theme-specific mobile overrides from inside @media blocks (lines 1672-1677)
- Upload mode modal theme overrides (lines 2200-2231)

---

## Critical Design Decisions for Coder

1. **Scattered theme rules**: Theme overrides appear both inline with components (e.g., bookmarks theme at line 61-67) AND in the main theme blocks (1817-2068). The verification script checks that themes.css contains >= 90% of all theme rules. The coder should consolidate ALL `[data-theme=...]` rules into themes.css.

2. **Theme rules inside @media blocks**: Lines 1672-1677 have theme overrides for mobile tabbar inside the `@media(max-width:1024px)` block. The coder must decide: (a) move them into themes.css with their own `@media` wrapper, or (b) keep them in the responsive section. Either way, the Section 4 content parity check will catch any loss.

3. **Dock mobile overrides appear twice**: Lines 208 (standalone `@media`) and 1758-1762 (inside the big 1024px block). Both need to end up in dock.css. The Section 5 duplicate check will catch if they appear in multiple files.

4. **Cascade order is critical**: `themes.css` MUST be last because theme selectors use identical specificity to base rules and rely on source order to win. `dock.css` before `themes.css` ensures theme overrides for dock elements apply correctly.

5. **The `.binary-rain-canvas` and `.rose-bloom-canvas` rules**: These are defined as base rules (display:none) and then overridden by theme selectors. The base definitions go in panels.css (or components.css); the theme-activated versions go in themes.css.

6. **`@keyframes` placement**: Each `@keyframes` should live in the same file as the selector that uses it. E.g., `gdspin` with Google Drive styles in panels.css, `uploadModalIn` with upload modal in components.css.

---

## Quality Gates for qa-engineer

- [ ] Baseline captured before extraction began (css_baseline.txt + css_baseline_meta.json exist)
- [ ] `node tests/test_css_extraction_verify.js` exits 0
- [ ] All 13 sections pass (0 FAIL)
- [ ] Warnings reviewed and justified
- [ ] `static/css/` directory contains exactly: base.css, components.css, panels.css, dock.css, themes.css
- [ ] No regressions in existing test suite: `node tests/test_panel_dock.js` still passes
- [ ] Manual spot-check: load portal on 8097, toggle between dark/light/girly themes, verify no visual diff

## Notes for full-stack-developer

1. **Run baseline FIRST**: `node tests/test_css_extraction_baseline.js` -- this is a one-time capture. Do not skip it.
2. **Iterate against verify**: After each extraction pass, run `node tests/test_css_extraction_verify.js`. Fix failures. Repeat.
3. **The Section 4 parity check is the master test**: If concatenating all 5 CSS files in link order produces the same normalized CSS as the baseline, the extraction is correct by definition. Other sections provide diagnostics.
4. **Do not modify selectors or values**: This is pure extraction. Copy-paste, do not rewrite. The normalization strips comments and whitespace, but selector and value changes will be caught.
5. **The CSS directory does not exist yet**: Create `static/css/` before starting.
6. **The `<link>` tags should use relative paths**: `static/css/base.css` etc., matching the server's static file serving.
7. **The server reads HTML from disk on every request** (no caching), so the changes will be immediately visible on refresh.
