#!/usr/bin/env node
/**
 * CC (Command Center) Chat Extraction -- Pre/Post Verification Tests
 *
 * Tests that the CC Chat IIFE (section 16: CC (Command Center) Chat Integration)
 * is correctly extracted from portal-pb-styled.html into static/js/features/cc-chat.js.
 *
 * These tests should FAIL before extraction and PASS after.
 *
 * Usage: node tests/test_cc_extraction.js
 */

const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const HTML_FILE = path.join(ROOT, 'portal-pb-styled.html');
const CC_JS = path.join(ROOT, 'static', 'js', 'features', 'cc-chat.js');

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

assert(fs.existsSync(CC_JS), 'static/js/features/cc-chat.js exists');

if (!fs.existsSync(CC_JS)) {
  console.error('\nFATAL: cc-chat.js does not exist yet. Remaining tests will reference empty content.');
}

const ccCode = fs.existsSync(CC_JS) ? fs.readFileSync(CC_JS, 'utf8') : '';

// IIFE wrapper
const trimmed = ccCode.trim();
assert(trimmed.startsWith('(function(){') || trimmed.startsWith('(function () {'),
  'File starts with IIFE opening: (function(){');
assert(trimmed.endsWith('})();'),
  'File ends with IIFE closing: })();');

// 'use strict' directive
assert(ccCode.includes("'use strict'"), "File contains 'use strict' directive");

// =============================================================================
// SECTION 2: Window-Exposed Globals
// =============================================================================
console.log('\n=== Section 2: Window-Exposed Globals ===');

const requiredWindowGlobals = [
  '_ccReply',
  '_ccSendReply',
  '_ccFilterChannel',
  '_ccStartPolling',
  '_ccStopPolling',
  '_ccFullStop',
  '_ccToggle',
];

for (const fn of requiredWindowGlobals) {
  assert(ccCode.includes('window.' + fn),
    `Extracted file exposes window.${fn}`);
}

// =============================================================================
// SECTION 3: Key Internal Functions Present
// =============================================================================
console.log('\n=== Section 3: Key Internal Functions ===');

const requiredFunctions = [
  '_loadCCSettings',
  '_saveCCSettings',
  'ccHeaders',
  'formatCCTime',
  'highlightMentions',
  'getChannelName',
  'renderCCMessage',
  'loadChannels',
  'pollMessages',
  'startPolling',
  'stopPolling',
  '_updateToggleUI',
];

for (const fn of requiredFunctions) {
  assert(ccCode.includes('function ' + fn),
    `Function ${fn} is defined in extracted file`);
}

// =============================================================================
// SECTION 4: Constants and Configuration
// =============================================================================
console.log('\n=== Section 4: Constants and Configuration ===');

assert(ccCode.includes("cc.purebrain.ai"),
  'Contains CC_BASE URL (cc.purebrain.ai)');

assert(ccCode.includes('CC_KEY'),
  'Contains CC_KEY variable');

assert(ccCode.includes('CIV_NAME'),
  'Contains CIV_NAME variable');

assert(ccCode.includes('POLL_INTERVAL') || ccCode.includes('10000'),
  'Contains active poll interval (10000ms)');

assert(ccCode.includes('60000'),
  'Contains background poll interval (60000ms)');

// =============================================================================
// SECTION 5: Dependencies and Auth Pattern
// =============================================================================
console.log('\n=== Section 5: Dependencies and Auth Pattern ===');

assert(ccCode.includes("localStorage.getItem('portal_token')"),
  'Uses localStorage.getItem(portal_token) for auth');

assert(ccCode.includes("localStorage.getItem('cc_civ_key')"),
  'Reads cc_civ_key from localStorage');

assert(ccCode.includes('escHtml') || ccCode.includes('ccEsc'),
  'References escHtml helper (directly or via ccEsc alias)');

assert(ccCode.includes("'cc-key-saved'"),
  'Listens for cc-key-saved custom event');

assert(ccCode.includes('showToast'),
  'References showToast for user notifications');

// =============================================================================
// SECTION 6: DOM Element References
// =============================================================================
console.log('\n=== Section 6: DOM Element References ===');

const requiredDomIds = [
  'ccMessages',
  'ccChannelFilter',
  'ccStatus',
  'ccBadge',
  'ccToggleBtn',
  'ccSubtab',
];

for (const domId of requiredDomIds) {
  assert(ccCode.includes(domId),
    `References DOM element: ${domId}`);
}

// =============================================================================
// SECTION 7: IIFE Removed from HTML
// =============================================================================
console.log('\n=== Section 7: IIFE Removed from HTML ===');

const html = fs.readFileSync(HTML_FILE, 'utf8');

assert(!html.includes('// 16. CC (Command Center) Chat Integration'),
  'Section 16 comment marker removed from HTML');

// Verify key function definitions are gone from inline HTML
const loadCCSettingsInHtml = (html.match(/function _loadCCSettings/g) || []).length;
assert(loadCCSettingsInHtml === 0,
  'function _loadCCSettings no longer defined inline in HTML');

const pollMessagesInHtml = (html.match(/function pollMessages/g) || []).length;
assert(pollMessagesInHtml === 0,
  'function pollMessages no longer defined inline in HTML (CC version)');

const renderCCMessageInHtml = (html.match(/function renderCCMessage/g) || []).length;
assert(renderCCMessageInHtml === 0,
  'function renderCCMessage no longer defined inline in HTML');

// =============================================================================
// SECTION 8: Script Tag Added to HTML
// =============================================================================
console.log('\n=== Section 8: Script Tag in HTML ===');

assert(html.includes('<script src="/static/js/features/cc-chat.js"></script>'),
  'HTML includes <script src="/static/js/features/cc-chat.js"></script>');

// Verify ordering: cc-chat.js should appear after settings.js (it depends on settings infra)
const settingsPos = html.indexOf('features/settings.js');
const ccChatPos = html.indexOf('features/cc-chat.js');

assert(ccChatPos > settingsPos,
  'cc-chat.js script tag appears after settings.js');

// =============================================================================
// SECTION 9: No Duplicate Globals
// =============================================================================
console.log('\n=== Section 9: No Duplicate Globals ===');

// window._ccStartPolling should appear zero times in the HTML
const ccStartInHtml = (html.match(/window\._ccStartPolling/g) || []).length;
assert(ccStartInHtml === 0,
  'window._ccStartPolling does not appear in HTML (fully extracted)');

// It should appear exactly once in the extracted file (the assignment)
const ccStartInJs = (ccCode.match(/window\._ccStartPolling/g) || []).length;
assert(ccStartInJs === 1,
  'window._ccStartPolling appears exactly once in cc-chat.js');

// =============================================================================
// SECTION 10: Adjacent Sections Not Damaged
// =============================================================================
console.log('\n=== Section 10: Adjacent Sections Not Damaged ===');

// Section 15 (Payments tab) should still exist -- it comes right before CC
assert(html.includes('// 15. Payments tab'),
  'Section 15 (Payments tab) still present in HTML (not accidentally removed)');

// MOBILE BOTTOM TAB BAR comes right after the CC IIFE closing </script>
assert(html.includes('MOBILE BOTTOM TAB BAR'),
  'MOBILE BOTTOM TAB BAR section still present in HTML (not accidentally removed)');

// IIFE 2e (FILES PANEL) should still exist further down
assert(html.includes('IIFE 2e') || html.includes('FILES PANEL'),
  'IIFE 2e (FILES PANEL) still present in HTML (not accidentally removed)');

// =============================================================================
// SECTION 11: File Size Sanity Check
// =============================================================================
console.log('\n=== Section 11: Sanity Checks ===');

if (ccCode.length > 0) {
  // The IIFE was ~383 lines (5602-5984). Extracted file should be similar size.
  const lineCount = ccCode.split('\n').length;
  assert(lineCount >= 300, `Extracted file has >= 300 lines (got ${lineCount}; expect ~383)`);
  assert(lineCount <= 550, `Extracted file has <= 550 lines (got ${lineCount}; not bloated)`);

  // File should be non-trivial size
  assert(ccCode.length >= 8000, `File size >= 8KB (got ${ccCode.length}; not a stub)`);
}

// --- Summary ---
console.log(`\n${'='.repeat(50)}`);
console.log(`Results: ${passed} passed, ${failed} failed, ${passed + failed} total`);
if (failed > 0) {
  process.exit(1);
} else {
  console.log('All tests passed!');
}
