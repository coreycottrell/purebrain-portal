#!/usr/bin/env node
/**
 * Terminal/Context/Status Extraction -- Pre/Post Verification Tests
 *
 * Tests that the terminal IIFE (IIFE 2b: Terminal, Context, Status & Compact)
 * is correctly extracted from portal-pb-styled.html into
 * static/js/features/terminal.js.
 *
 * These tests should FAIL before extraction and PASS after.
 *
 * Usage: node tests/test_terminal_extraction.js
 */

const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const HTML_FILE = path.join(ROOT, 'portal-pb-styled.html');
const TERMINAL_JS = path.join(ROOT, 'static', 'js', 'features', 'terminal.js');

let passed = 0;
let failed = 0;

function assert(condition, msg) {
  if (condition) {
    passed++;
    console.log(`  PASS: ${msg}`);
  } else {
    failed++;
    console.error(`  FAIL: ${msg}`);
  }
}

// =============================================================================
// SECTION 1: Extracted File Exists and Has Correct Structure
// =============================================================================
console.log('\n=== Section 1: Extracted File Structure ===');

assert(fs.existsSync(TERMINAL_JS), 'static/js/features/terminal.js exists');

if (!fs.existsSync(TERMINAL_JS)) {
  console.error('\nFATAL: terminal.js does not exist yet. Remaining tests will reference empty content.');
}

const termCode = fs.existsSync(TERMINAL_JS) ? fs.readFileSync(TERMINAL_JS, 'utf8') : '';

// IIFE wrapper
const trimmed = termCode.trim();
assert(trimmed.startsWith('(function(){') || trimmed.startsWith('(function () {'),
  'File starts with IIFE opening: (function(){');
assert(trimmed.endsWith('})();'),
  'File ends with IIFE closing: })();');

// 'use strict' directive
assert(termCode.includes("'use strict'"), "File contains 'use strict' directive");

// =============================================================================
// SECTION 2: Global Exposure (window._portalMonitor)
// =============================================================================
console.log('\n=== Section 2: Global Exposure ===');

assert(termCode.includes('window._portalMonitor'),
  'Extracted file exposes window._portalMonitor');

assert(termCode.includes('window.refreshTeamsPanes'),
  'Extracted file exposes window.refreshTeamsPanes');

assert(termCode.includes('window._stopMonitoring'),
  'Extracted file exposes window._stopMonitoring');

// Verify the _portalMonitor shape: refreshTerminal, updateContext, updateStatus, isClaudeAlive
assert(termCode.includes('refreshTerminal'),
  '_portalMonitor exposes refreshTerminal');
assert(termCode.includes('updateContext'),
  '_portalMonitor exposes updateContext');
assert(termCode.includes('updateStatus'),
  '_portalMonitor exposes updateStatus');
assert(termCode.includes('isClaudeAlive'),
  '_portalMonitor exposes isClaudeAlive');

// =============================================================================
// SECTION 3: Key Functions Present
// =============================================================================
console.log('\n=== Section 3: Key Functions Present ===');

const requiredFunctions = [
  '_getToken',
  '_safeJson',
  'stripAnsi',
  'loadPaneTabs',
  'pollSelectedPane',
  'connectTerminalWS',
  'updateCtxPill',
  'updateOnlineStatus',
  'pollCompactStatus',
  'bootMonitoring',
];

for (const fn of requiredFunctions) {
  assert(termCode.includes('function ' + fn),
    `Function ${fn} is defined in extracted file`);
}

// =============================================================================
// SECTION 4: Dependencies and Auth Pattern
// =============================================================================
console.log('\n=== Section 4: Dependencies and Auth Pattern ===');

assert(termCode.includes("localStorage.getItem('portal_token')"),
  'Uses localStorage.getItem(portal_token) for auth');

assert(termCode.includes('window._portalChat'),
  'References window._portalChat for token fallback');

assert(termCode.includes('portalFetch'),
  'Uses portalFetch helper for authenticated API calls');

assert(termCode.includes('escHtml'),
  'References escHtml helper for safe HTML rendering');

// =============================================================================
// SECTION 5: API Endpoint References
// =============================================================================
console.log('\n=== Section 5: API Endpoint References ===');

const requiredEndpoints = [
  '/api/panes',
  '/api/context',
  '/api/status',
  '/api/compact/status',
  '/api/restart',
];

for (const endpoint of requiredEndpoints) {
  assert(termCode.includes("'" + endpoint + "'") || termCode.includes('"' + endpoint + '"'),
    `References API endpoint: ${endpoint}`);
}

// WebSocket endpoint
assert(termCode.includes('/ws/terminal'),
  'References WebSocket endpoint: /ws/terminal');

// =============================================================================
// SECTION 6: DOM Element References
// =============================================================================
console.log('\n=== Section 6: DOM Element References ===');

const requiredDomRefs = [
  'teamsPaneContent',
  'teamsTabBar',
  'ctx-pill',
  'ctx-fill',
  'online-dot',
  'online-pill',
  'compact-banner',
  'restartBtn',
];

for (const ref of requiredDomRefs) {
  assert(termCode.includes(ref),
    `References DOM element: ${ref}`);
}

// =============================================================================
// SECTION 7: Polling Intervals Present
// =============================================================================
console.log('\n=== Section 7: Polling Intervals ===');

assert(termCode.includes('setInterval'),
  'Contains setInterval for polling');

assert(termCode.includes('30000') || termCode.includes('30e3'),
  'Has 30s polling interval (context pill)');

assert(termCode.includes('60000') || termCode.includes('60e3'),
  'Has 60s polling interval (online status)');

assert(termCode.includes('10000') || termCode.includes('10e3'),
  'Has 10s polling interval (compact status)');

assert(termCode.includes('3000') || termCode.includes('3e3'),
  'Has 3s polling interval (pane tabs)');

// =============================================================================
// SECTION 8: IIFE Removed from HTML
// =============================================================================
console.log('\n=== Section 8: IIFE Removed from HTML ===');

const html = fs.readFileSync(HTML_FILE, 'utf8');

assert(!html.includes('// 2b. Terminal, Context, Status & Compact'),
  'IIFE 2b comment marker removed from HTML');

// Verify key function definitions are gone from HTML
const bootInHtml = (html.match(/function bootMonitoring/g) || []).length;
assert(bootInHtml === 0,
  'function bootMonitoring no longer defined inline in HTML');

const connectTermInHtml = (html.match(/function connectTerminalWS/g) || []).length;
assert(connectTermInHtml === 0,
  'function connectTerminalWS no longer defined inline in HTML');

const updateCtxInHtml = (html.match(/function updateCtxPill/g) || []).length;
assert(updateCtxInHtml === 0,
  'function updateCtxPill no longer defined inline in HTML');

const updateStatusInHtml = (html.match(/function updateOnlineStatus/g) || []).length;
assert(updateStatusInHtml === 0,
  'function updateOnlineStatus no longer defined inline in HTML');

// =============================================================================
// SECTION 9: Script Tag Added to HTML
// =============================================================================
console.log('\n=== Section 9: Script Tag in HTML ===');

assert(html.includes('<script src="/static/js/features/terminal.js"></script>'),
  'HTML includes <script src="/static/js/features/terminal.js"></script>');

// Verify ordering: terminal.js should appear after panel-manager.js
const panelManagerPos = html.indexOf('panel-manager.js');
const terminalPos = html.indexOf('features/terminal.js');

assert(terminalPos > panelManagerPos,
  'terminal.js script tag appears after panel-manager.js');

// =============================================================================
// SECTION 10: No Duplicate Globals
// =============================================================================
console.log('\n=== Section 10: No Duplicate Globals ===');

// window._portalMonitor assignment should not appear in the HTML anymore
const portalMonitorInHtml = (html.match(/window\._portalMonitor\s*=/g) || []).length;
assert(portalMonitorInHtml === 0,
  'window._portalMonitor assignment does not appear in HTML (fully extracted)');

// It should appear exactly once in the extracted file
const portalMonitorInJs = (termCode.match(/window\._portalMonitor\s*=/g) || []).length;
assert(portalMonitorInJs === 1,
  'window._portalMonitor assigned exactly once in terminal.js');

// =============================================================================
// SECTION 11: Neighboring IIFEs Intact
// =============================================================================
console.log('\n=== Section 11: Neighboring IIFEs Intact ===');

// IIFE 2 (Poke AI) should still exist in HTML -- we did NOT remove it
assert(html.includes('// 2. Poke AI'),
  'IIFE 2 (Poke AI) still present in HTML (not accidentally removed)');

// IIFE 3 (Chat Search) should still exist in HTML
assert(html.includes('// 3. Chat Search'),
  'IIFE 3 (Chat Search) still present in HTML (not accidentally removed)');

// The stub for refreshTeamsPanes should still be in HTML (pre-auth fallback)
assert(html.includes('refreshTeamsPanes') && html.includes('Connecting'),
  'refreshTeamsPanes stub still present in HTML (pre-auth fallback)');

// The _portalMonitor consumer reference should remain (line ~2163)
assert(html.includes('_portalMonitor') && html.includes('isClaudeAlive'),
  '_portalMonitor consumer reference still in HTML (read-only usage, not definition)');

// =============================================================================
// SECTION 12: File Size Sanity Check
// =============================================================================
console.log('\n=== Section 12: Sanity Checks ===');

if (termCode.length > 0) {
  // The IIFE spans lines 5031-5418 (~387 lines). Extracted file should be similar.
  const lineCount = termCode.split('\n').length;
  assert(lineCount >= 300, `Extracted file has >= 300 lines (got ${lineCount}; expect ~387)`);
  assert(lineCount <= 550, `Extracted file has <= 550 lines (got ${lineCount}; not bloated)`);

  // File should be non-trivial size
  assert(termCode.length >= 8000, `File size >= 8KB (got ${termCode.length}; not a stub)`);
}

// --- Summary ---
console.log(`\n${'='.repeat(50)}`);
console.log(`Results: ${passed} passed, ${failed} failed`);
if (failed > 0) {
  process.exit(1);
} else {
  console.log('All tests passed!');
}
