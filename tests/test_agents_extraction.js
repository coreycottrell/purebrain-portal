#!/usr/bin/env node
/**
 * Agents Panel Extraction -- Pre/Post Verification Tests
 *
 * Tests that the agents IIFE (IIFE 2c: AGENTS + ROSTER) is correctly
 * extracted from portal-pb-styled.html into static/js/features/agents.js.
 *
 * These tests should FAIL before extraction and PASS after.
 *
 * Usage: node tests/test_agents_extraction.js
 */

const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const HTML_FILE = path.join(ROOT, 'portal-pb-styled.html');
const AGENTS_JS = path.join(ROOT, 'static', 'js', 'features', 'agents.js');

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

assert(fs.existsSync(AGENTS_JS), 'static/js/features/agents.js exists');

if (!fs.existsSync(AGENTS_JS)) {
  console.error('\nFATAL: agents.js does not exist yet. Remaining tests will reference empty content.');
}

const agentsCode = fs.existsSync(AGENTS_JS) ? fs.readFileSync(AGENTS_JS, 'utf8') : '';

// IIFE wrapper
const trimmed = agentsCode.trim();
assert(trimmed.startsWith('(function(){') || trimmed.startsWith('(function () {'),
  'File starts with IIFE opening: (function(){');
assert(trimmed.endsWith('})();'),
  'File ends with IIFE closing: })();');

// 'use strict' directive
assert(agentsCode.includes("'use strict'"), "File contains 'use strict' directive");

// =============================================================================
// SECTION 2: Global Exposure
// =============================================================================
console.log('\n=== Section 2: Global Exposure ===');

assert(agentsCode.includes('window._loadAgentsPanel'),
  'Extracted file exposes window._loadAgentsPanel');

// =============================================================================
// SECTION 3: Key Functions Present
// =============================================================================
console.log('\n=== Section 3: Key Functions Present ===');

const requiredFunctions = [
  '_buildAgentCard',
  '_loadAgents',
  '_loadLiveAgents',
  '_renderActiveAgentsGrid',
  '_renderRosterGrid',
  '_wireFilter',
  '_wireViewToggle',
];

for (const fn of requiredFunctions) {
  assert(agentsCode.includes('function ' + fn),
    `Function ${fn} is defined in extracted file`);
}

// =============================================================================
// SECTION 4: Dependencies and Auth Pattern
// =============================================================================
console.log('\n=== Section 4: Dependencies and Auth Pattern ===');

assert(agentsCode.includes("localStorage.getItem('portal_token')"),
  'Uses localStorage.getItem(portal_token) for auth');

assert(agentsCode.includes('escHtml') || agentsCode.includes('_escH'),
  'References escHtml helper (directly or via _escH alias)');

assert(agentsCode.includes("'portal-auth'"),
  'Listens for portal-auth custom event');

// =============================================================================
// SECTION 5: Auto-refresh Timers Present
// =============================================================================
console.log('\n=== Section 5: Auto-refresh Timers ===');

assert(agentsCode.includes('setInterval'),
  'Contains setInterval for auto-refresh');

assert(agentsCode.includes('30000') || agentsCode.includes('30e3'),
  'Has 30s refresh interval for agent list');

assert(agentsCode.includes('20000') || agentsCode.includes('20e3'),
  'Has 20s polling interval for live agents');

// =============================================================================
// SECTION 6: DOM Element References
// =============================================================================
console.log('\n=== Section 6: DOM Element References ===');

const requiredDomIds = [
  'active-agents-grid',
  'ar-grid',
  'ar-grid-2',
  'agentsArea',
  'agent-rosterArea',
];

for (const domId of requiredDomIds) {
  assert(agentsCode.includes(domId),
    `References DOM element: ${domId}`);
}

// =============================================================================
// SECTION 7: Global onclick References Intact
// =============================================================================
console.log('\n=== Section 7: Global onclick References ===');

const requiredOnclickGlobals = [
  'openAssignTask',
  'wakeAgent',
  'chatWithAgent',
  'updateBatchBar',
];

for (const fn of requiredOnclickGlobals) {
  assert(agentsCode.includes(fn),
    `References global onclick function: ${fn}`);
}

// =============================================================================
// SECTION 8: IIFE Removed from HTML
// =============================================================================
console.log('\n=== Section 8: IIFE Removed from HTML ===');

const html = fs.readFileSync(HTML_FILE, 'utf8');

assert(!html.includes('// ===== IIFE 2c: AGENTS + ROSTER'),
  'IIFE 2c comment marker removed from HTML');

// Verify the inline _buildAgentCard function is gone from HTML
// (it should only exist in the extracted file now)
const buildCardInHtml = (html.match(/function _buildAgentCard/g) || []).length;
assert(buildCardInHtml === 0,
  'function _buildAgentCard no longer defined inline in HTML');

// Verify _loadLiveAgents function definition is gone from HTML
const loadLiveInHtml = (html.match(/function _loadLiveAgents/g) || []).length;
assert(loadLiveInHtml === 0,
  'function _loadLiveAgents no longer defined inline in HTML');

// =============================================================================
// SECTION 9: Script Tag Added to HTML
// =============================================================================
console.log('\n=== Section 9: Script Tag in HTML ===');

assert(html.includes('<script src="/static/js/features/agents.js"></script>'),
  'HTML includes <script src="/static/js/features/agents.js"></script>');

// Verify ordering: agents.js should appear after panel-manager.js and dock.js
const panelManagerPos = html.indexOf('panel-manager.js');
const dockPos = html.indexOf('features/dock.js');
const agentsPos = html.indexOf('features/agents.js');

assert(agentsPos > panelManagerPos,
  'agents.js script tag appears after panel-manager.js');
assert(agentsPos > dockPos,
  'agents.js script tag appears after dock.js');

// =============================================================================
// SECTION 10: No Duplicate Globals
// =============================================================================
console.log('\n=== Section 10: No Duplicate Globals ===');

// _loadAgentsPanel should appear zero times in the HTML
// (it was moved entirely to agents.js)
const loadAgentsPanelInHtml = (html.match(/window\._loadAgentsPanel/g) || []).length;
assert(loadAgentsPanelInHtml === 0,
  'window._loadAgentsPanel does not appear in HTML (fully extracted)');

// It should appear exactly once in the extracted file
const loadAgentsPanelInJs = (agentsCode.match(/window\._loadAgentsPanel/g) || []).length;
assert(loadAgentsPanelInJs === 1,
  'window._loadAgentsPanel appears exactly once in agents.js');

// =============================================================================
// SECTION 11: File Size Sanity Check
// =============================================================================
console.log('\n=== Section 11: Sanity Checks ===');

if (agentsCode.length > 0) {
  // The IIFE was ~390 lines (2396-2786). Extracted file should be similar size.
  const lineCount = agentsCode.split('\n').length;
  assert(lineCount >= 300, `Extracted file has >= 300 lines (got ${lineCount}; expect ~390)`);
  assert(lineCount <= 600, `Extracted file has <= 600 lines (got ${lineCount}; not bloated)`);

  // File should be non-trivial size
  assert(agentsCode.length >= 8000, `File size >= 8KB (got ${agentsCode.length}; not a stub)`);
}

// The IIFE 2d (TASKS, TODO, HUB...) should still exist in HTML -- we did NOT remove it
assert(html.includes('IIFE 2d') && html.includes('TASKS'),
  'IIFE 2d (TASKS) still present in HTML (not accidentally removed)');

// The global helper functions referenced by agents (openAssignTask, wakeAgent, etc.)
// should still be defined somewhere in the HTML (they are outside the agents IIFE)
assert(html.includes('function openAssignTask'),
  'openAssignTask still defined in HTML (global helper, not part of agents IIFE)');
assert(html.includes('function wakeAgent'),
  'wakeAgent still defined in HTML (global helper, not part of agents IIFE)');
assert(html.includes('function chatWithAgent'),
  'chatWithAgent still defined in HTML (global helper, not part of agents IIFE)');
assert(html.includes('function updateBatchBar'),
  'updateBatchBar still defined in HTML (global helper, not part of agents IIFE)');

// --- Summary ---
console.log(`\n${'='.repeat(50)}`);
console.log(`Results: ${passed} passed, ${failed} failed`);
if (failed > 0) {
  process.exit(1);
} else {
  console.log('All tests passed!');
}
