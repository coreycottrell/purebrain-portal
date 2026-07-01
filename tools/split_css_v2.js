#!/usr/bin/env node
/**
 * CSS Extraction v2: Category-based split that extracts theme rules
 * from their scattered positions into themes.css.
 *
 * This approach categorizes each line/block by concern rather than
 * using contiguous segments.
 */

const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const BASELINE = path.join(ROOT, 'tests', 'css_baseline.txt');
const CSS_DIR = path.join(ROOT, 'static', 'css');

const baseline = fs.readFileSync(BASELINE, 'utf8');
const lines = baseline.split('\n');

function normalize(cssText) {
  return cssText
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/\s+/g, ' ')
    .replace(/\s*([{}:;,>~+])\s*/g, '$1')
    .trim();
}

// First, let me understand: HOW MANY theme rules need to move?
// And what's the character-level impact on the normalized string?

// Count theme rules in various ranges
function countThemeRules(startLine, endLine) {
  let count = 0;
  for (let i = startLine; i <= endLine && i < lines.length; i++) {
    if (lines[i].includes('[data-theme="light"]') || lines[i].includes('[data-theme="girly"]')) {
      count++;
    }
  }
  return count;
}

console.log('Theme rules by range:');
console.log(`  Lines 0-24 (base):       ${countThemeRules(0, 24)}`);
console.log(`  Lines 25-167 (components): ${countThemeRules(25, 167)}`);
console.log(`  Lines 168-170 (panels):    ${countThemeRules(168, 170)}`);
console.log(`  Lines 171-1790 (dock):     ${countThemeRules(171, 1790)}`);
console.log(`  Lines 1791-end (themes):   ${countThemeRules(1791, lines.length - 1)}`);

// List the scattered theme lines so I can see what we're dealing with
console.log('\nScattered theme lines in components range (25-167):');
for (let i = 25; i <= 167; i++) {
  if (lines[i].includes('[data-theme=')) {
    console.log(`  ${i}: ${lines[i].substring(0, 80)}`);
  }
}

console.log('\nScattered theme lines in dock range (171-1790):');
let themeInDock = [];
for (let i = 171; i <= 1790; i++) {
  if (lines[i].includes('[data-theme=')) {
    themeInDock.push(i);
  }
}
console.log(`  Total: ${themeInDock.length} lines`);
console.log('  First few:');
for (let i = 0; i < Math.min(10, themeInDock.length); i++) {
  console.log(`    ${themeInDock[i]}: ${lines[themeInDock[i]].substring(0, 80)}`);
}
console.log('  Last few:');
for (let i = Math.max(0, themeInDock.length - 5); i < themeInDock.length; i++) {
  console.log(`    ${themeInDock[i]}: ${lines[themeInDock[i]].substring(0, 80)}`);
}

// Now let me try: what if we remove theme lines from components and dock,
// put them in themes.css, and see how far off the normalized string is?
const FILE_BASE = 0, FILE_COMP = 1, FILE_PANEL = 2, FILE_DOCK = 3, FILE_THEME = 4;
const assignments = [];

function isThemeLine(line) {
  return line.includes('[data-theme="light"]') || line.includes('[data-theme="girly"]');
}

// Base: 0-24
for (let i = 0; i <= 24; i++) assignments[i] = FILE_BASE;

// Components: 25-167 (theme lines go to themes)
for (let i = 25; i <= 167; i++) {
  assignments[i] = isThemeLine(lines[i]) ? FILE_THEME : FILE_COMP;
}

// Panels: 168-170
for (let i = 168; i <= 170; i++) assignments[i] = FILE_PANEL;

// Dock: 171-1790 (theme lines go to themes)
for (let i = 171; i <= 1790; i++) {
  assignments[i] = isThemeLine(lines[i]) ? FILE_THEME : FILE_DOCK;
}

// Themes: 1791-end
for (let i = 1791; i < lines.length; i++) assignments[i] = FILE_THEME;

// Build files
const fileLines = [[], [], [], [], []];
for (let i = 0; i < lines.length; i++) {
  fileLines[assignments[i]].push(lines[i]);
}

// Concatenate in order: base + components + panels + dock + themes
const combined = [
  ...fileLines[FILE_BASE],
  ...fileLines[FILE_COMP],
  ...fileLines[FILE_PANEL],
  ...fileLines[FILE_DOCK],
  ...fileLines[FILE_THEME],
].join('\n');

const normBaseline = normalize(baseline);
const normCombined = normalize(combined);

console.log(`\nNormalized baseline: ${normBaseline.length} chars`);
console.log(`Normalized combined: ${normCombined.length} chars`);
console.log(`Length match: ${normBaseline.length === normCombined.length}`);
console.log(`String match: ${normBaseline === normCombined}`);

if (normBaseline !== normCombined) {
  const minLen = Math.min(normBaseline.length, normCombined.length);
  let divergeAt = -1;
  for (let i = 0; i < minLen; i++) {
    if (normBaseline[i] !== normCombined[i]) {
      divergeAt = i;
      break;
    }
  }
  if (divergeAt === -1) divergeAt = minLen;
  console.log(`First divergence at char ${divergeAt}:`);
  const ctx = 60;
  const start = Math.max(0, divergeAt - ctx);
  console.log(`  BASELINE: ...${normBaseline.substring(start, divergeAt + ctx)}...`);
  console.log(`  COMBINED: ...${normCombined.substring(start, divergeAt + ctx)}...`);
}

// Check theme counts
const themesContent = fileLines[FILE_THEME].join('\n');
const lightCount = (themesContent.match(/\[data-theme="light"\]/g) || []).length;
const girlyCount = (themesContent.match(/\[data-theme="girly"\]/g) || []).length;
console.log(`\nthemes.css: light=${lightCount} (need>=127), girly=${girlyCount} (need>=159)`);
