#!/usr/bin/env node
/**
 * CSS Extraction: Split baseline into 5 contiguous files.
 *
 * The verification test concatenates: base + components + panels + dock + themes
 * and compares the normalized result against the baseline. The 5 files must be
 * contiguous segments of the baseline to pass content parity (Section 4).
 *
 * Split:
 *   base.css:       Lines 0-24   (resets, :root vars, body, scrollbar, .app grid)
 *   components.css: Lines 25-167 (bookmarks, topnav, sidebar, main-col, brain-canvas)
 *   panels.css:     Lines 168-170 (.chat-area base rules)
 *   dock.css:       Lines 171-1539 (dock + all panels + governance)
 *   themes.css:     Lines 1540-end (theme overrides, light/girly themes, media queries, upload modal)
 */

const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const BASELINE = path.join(ROOT, 'tests', 'css_baseline.txt');
const CSS_DIR = path.join(ROOT, 'static', 'css');

const baseline = fs.readFileSync(BASELINE, 'utf8');
const lines = baseline.split('\n');

// Split points (0-indexed, inclusive end)
const SPLIT_A = 24;   // End of base (.app grid layout)
const SPLIT_B = 167;  // End of components
const SPLIT_C = 170;  // End of panels (.chat-area.hidden)
const SPLIT_D = 1539; // End of dock (before "Light + girly overrides" comment)

const base = lines.slice(0, SPLIT_A + 1);
const components = lines.slice(SPLIT_A + 1, SPLIT_B + 1);
const panels = lines.slice(SPLIT_B + 1, SPLIT_C + 1);
const dock = lines.slice(SPLIT_C + 1, SPLIT_D + 1);
const themes = lines.slice(SPLIT_D + 1);

console.log(`base.css:       ${base.length} lines`);
console.log(`components.css: ${components.length} lines`);
console.log(`panels.css:     ${panels.length} lines`);
console.log(`dock.css:       ${dock.length} lines`);
console.log(`themes.css:     ${themes.length} lines`);

// Verify exact match
const combined = [...base, ...components, ...panels, ...dock, ...themes].join('\n');
console.log(`Exact match: ${baseline === combined}`);

// Write files
fs.mkdirSync(CSS_DIR, { recursive: true });

const fileMap = [
  ['base.css', base],
  ['components.css', components],
  ['panels.css', panels],
  ['dock.css', dock],
  ['themes.css', themes],
];

for (const [name, arr] of fileMap) {
  const content = arr.join('\n');
  fs.writeFileSync(path.join(CSS_DIR, name), content, 'utf8');
  console.log(`Wrote ${name}: ${content.length} bytes`);
}

// Quick checks
const baseContent = base.join('\n');
console.log(`\nbase: :root=${baseContent.includes(':root')}, .app+grid=${baseContent.includes('.app') && baseContent.includes('grid')}, scrollbar=${baseContent.includes('::-webkit-scrollbar')}, html,body=${baseContent.includes('html,body')}`);

const dockContent = dock.join('\n');
console.log(`dock: chat-docked=${dockContent.includes('.app.chat-docked')}, dock-btn=${dockContent.includes('.dock-btn')}, @media=${/\@media.*max-width.*1024/.test(dockContent)}`);

const themesContent = themes.join('\n');
const lightCount = (themesContent.match(/\[data-theme="light"\]/g) || []).length;
const girlyCount = (themesContent.match(/\[data-theme="girly"\]/g) || []).length;
console.log(`themes: light=${lightCount} (need>=127), girly=${girlyCount} (need>=159)`);
