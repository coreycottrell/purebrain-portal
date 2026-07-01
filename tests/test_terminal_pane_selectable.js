#!/usr/bin/env node
/**
 * Terminal Pane Selectable -- CSS Regression Test
 *
 * THE REAL ROOT CAUSE (third copy/paste attempt):
 * The served portal sets `user-select:none` on the <body> (base.css) and then
 * re-enables `user-select:text` for a specific allowlist of elements. The
 * terminal pane (#teamsPaneContent / .teams-pane-content) was NOT on that
 * allowlist, so the user could not select its text AT ALL. No amount of
 * JS selection-preservation logic could help, because there was never a
 * selection to preserve.
 *
 * Two prior fixes touched terminal.js (the live renderer) and made the
 * re-render selection-safe -- correct and necessary, but invisible to the
 * user because the CSS forbade selection in the first place.
 *
 * This test asserts, against the ACTUAL served CSS file, that:
 *   1. body still has user-select:none (so we know the override matters), and
 *   2. .teams-pane-content is granted user-select:text.
 *
 * It FAILS before the CSS fix and PASSES after.
 *
 * Usage: node tests/test_terminal_pane_selectable.js
 */

const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const BASE_CSS = path.join(ROOT, 'static', 'css', 'base.css');

let passed = 0;
let failed = 0;
function assert(cond, msg) {
  if (cond) { passed++; console.log(`  PASS: ${msg}`); }
  else { failed++; console.error(`  FAIL: ${msg}`); }
}

const css = fs.readFileSync(BASE_CSS, 'utf8');

console.log('\n=== Terminal pane must be user-selectable ===');

// Sanity: body still disables selection globally (so the allowlist matters).
assert(
  /body\s*\{[^}]*user-select:\s*none/.test(css),
  'body sets user-select:none (the global default that requires an override)'
);

// Find every rule that grants user-select:text and collect their selectors.
const selectableSelectors = [];
const ruleRe = /([^{}]+)\{([^}]*)\}/g;
let m;
while ((m = ruleRe.exec(css)) !== null) {
  const selectorText = m[1];
  const body = m[2];
  if (/user-select:\s*text/.test(body)) {
    selectableSelectors.push(selectorText);
  }
}

const grantsTerminalPane = selectableSelectors.some(function (sel) {
  return /\.teams-pane-content/.test(sel);
});

assert(
  grantsTerminalPane,
  '.teams-pane-content is granted user-select:text (terminal text is selectable)'
);

console.log(`\n=== RESULTS: ${passed} passed, ${failed} failed ===\n`);
process.exit(failed === 0 ? 0 : 1);
