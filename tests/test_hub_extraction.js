#!/usr/bin/env node
/**
 * Hub Panel Extraction -- Pre/Post Verification Tests
 *
 * Tests that the hub section (within IIFE 2d) is correctly extracted from
 * portal-pb-styled.html into static/js/features/hub.js.
 *
 * These tests should FAIL before extraction and PASS after.
 *
 * Usage: node tests/test_hub_extraction.js
 */

const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const HTML_FILE = path.join(ROOT, 'portal-pb-styled.html');
const HUB_JS = path.join(ROOT, 'static', 'js', 'features', 'hub.js');

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
// SECTION 1: Extracted File Exists
// =============================================================================
console.log('\n=== Section 1: Extracted File Exists ===');

assert(fs.existsSync(HUB_JS), 'static/js/features/hub.js exists');

if (!fs.existsSync(HUB_JS)) {
  console.error('\nFATAL: hub.js does not exist yet. Remaining tests will reference empty content.');
}

const hubCode = fs.existsSync(HUB_JS) ? fs.readFileSync(HUB_JS, 'utf8') : '';

// =============================================================================
// SECTION 2: IIFE Structure
// =============================================================================
console.log('\n=== Section 2: IIFE Structure ===');

const trimmed = hubCode.trim();
assert(trimmed.startsWith('(function(){') || trimmed.startsWith('(function () {'),
  'File starts with IIFE opening: (function(){');
assert(trimmed.endsWith('})();'),
  'File ends with IIFE closing: })();');
assert(hubCode.includes("'use strict'"),
  "File contains 'use strict' directive");

// =============================================================================
// SECTION 3: Global Exposure
// =============================================================================
console.log('\n=== Section 3: Global Exposure ===');

assert(hubCode.includes('window._portalHub'),
  'Extracted file exposes window._portalHub');

// =============================================================================
// SECTION 4: Key Functions Present
// =============================================================================
console.log('\n=== Section 4: Key Functions Present ===');

const requiredFunctions = [
  '_refreshHub',
  '_loadHubLiveAgents',
  '_loadBoopStatus',
  '_loadBoopConfig',
  '_loadContextForHub',
  '_wireHubActions',
];

for (const fn of requiredFunctions) {
  assert(hubCode.includes('function ' + fn),
    `Function ${fn} is defined in extracted file`);
}

// =============================================================================
// SECTION 5: Auth Pattern
// =============================================================================
console.log('\n=== Section 5: Auth Pattern ===');

assert(hubCode.includes("localStorage.getItem('portal_token')"),
  'Uses localStorage.getItem(portal_token) for auth');

// =============================================================================
// SECTION 6: API Endpoints
// =============================================================================
console.log('\n=== Section 6: API Endpoints ===');

const requiredEndpoints = [
  '/api/hub/live-agents',
  '/api/boop/status',
  '/api/boop/config',
  '/api/context',
  '/api/status',
  '/api/restart',
  '/api/continue',
  '/api/resume',
  '/api/chat/send',
  '/api/hub/debug-report',
];

for (const ep of requiredEndpoints) {
  assert(hubCode.includes(ep),
    `References API endpoint: ${ep}`);
}

// =============================================================================
// SECTION 7: DOM Element References
// =============================================================================
console.log('\n=== Section 7: DOM Element References ===');

const requiredDomIds = [
  'hub-live-count',
  'hub-live-agents-list',
  'hub-svc-boop-led',
  'hub-svc-claude-led',
  'hub-btn-restart',
  'hub-btn-compact',
];

for (const domId of requiredDomIds) {
  assert(hubCode.includes(domId),
    `References DOM element: ${domId}`);
}

// =============================================================================
// SECTION 8: Hub Removed from HTML
// =============================================================================
console.log('\n=== Section 8: Hub Removed from HTML ===');

const html = fs.readFileSync(HTML_FILE, 'utf8');

const hubFunctionsToCheck = [
  '_refreshHub',
  '_loadHubLiveAgents',
  '_loadBoopStatus',
  '_wireHubActions',
];

for (const fn of hubFunctionsToCheck) {
  const matches = (html.match(new RegExp('function ' + fn, 'g')) || []).length;
  assert(matches === 0,
    `function ${fn} no longer defined inline in HTML`);
}

// =============================================================================
// SECTION 9: Script Tag in HTML
// =============================================================================
console.log('\n=== Section 9: Script Tag in HTML ===');

assert(html.includes('<script src="/static/js/features/hub.js"></script>'),
  'HTML includes <script src="/static/js/features/hub.js"></script>');

// hub.js should appear after agents.js (hub depends on later load order)
const agentsPos = html.indexOf('features/agents.js');
const hubPos = html.indexOf('features/hub.js');
assert(hubPos > agentsPos,
  'hub.js script tag appears after agents.js');

// =============================================================================
// SECTION 10: No Duplicate Globals
// =============================================================================
console.log('\n=== Section 10: No Duplicate Globals ===');

const hubGlobalInHtml = (html.match(/window\._portalHub/g) || []).length;
assert(hubGlobalInHtml === 0,
  'window._portalHub does not appear in HTML (fully extracted)');

const hubGlobalInJs = (hubCode.match(/window\._portalHub/g) || []).length;
assert(hubGlobalInJs === 1,
  'window._portalHub appears exactly once in hub.js');

// =============================================================================
// SECTION 11: Sanity Checks
// =============================================================================
console.log('\n=== Section 11: Sanity Checks ===');

// _portalTasks must still be defined somewhere in HTML (not accidentally removed)
assert(html.includes('window._portalTasks'),
  'window._portalTasks still defined in HTML (not accidentally removed)');

// _portalSettings must still be defined somewhere in HTML or in settings.js
const settingsJs = path.join(ROOT, 'static', 'js', 'features', 'settings.js');
const settingsInHtml = html.includes('window._portalSettings');
const settingsInFile = fs.existsSync(settingsJs) && fs.readFileSync(settingsJs, 'utf8').includes('window._portalSettings');
assert(settingsInHtml || settingsInFile,
  'window._portalSettings still defined in HTML or in settings.js (not accidentally removed)');

// --- Summary ---
console.log(`\n${'='.repeat(50)}`);
console.log(`Results: ${passed} passed, ${failed} failed`);
if (failed > 0) {
  process.exit(1);
} else {
  console.log('All tests passed!');
}
