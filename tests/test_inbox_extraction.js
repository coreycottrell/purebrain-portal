#!/usr/bin/env node
/**
 * Inbox IIFE Extraction -- Pre/Post Verification Tests
 *
 * Tests that the AgentMail Inbox code (email wizard, chat/inbox/CC view
 * switching, and all inbox functions) is correctly extracted from
 * portal-pb-styled.html into static/js/features/inbox.js.
 *
 * The inbox module covers:
 * - Email config state (emailConfigured, emailAddress)
 * - Email wizard (openEmailWizard, closeEmailWizard, ewGoTo, ewNext, etc.)
 * - Chat/Inbox/CC view switching (switchChatView)
 * - AgentMail inbox (syncInbox, openThread, filterInbox, searchInbox, etc.)
 * - Compose email (handleComposeEmail, _openComposeModal, _sendComposedEmail)
 *
 * These tests should FAIL before extraction and PASS after.
 *
 * Usage: node tests/test_inbox_extraction.js
 */

const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const HTML_FILE = path.join(ROOT, 'portal-pb-styled.html');
const INBOX_JS = path.join(ROOT, 'static', 'js', 'features', 'inbox.js');

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
// SECTION 1: Extracted File Structure
// =============================================================================
console.log('\n=== Section 1: File Structure ===');

assert(fs.existsSync(INBOX_JS), 'static/js/features/inbox.js exists');

const code = fs.existsSync(INBOX_JS) ? fs.readFileSync(INBOX_JS, 'utf8') : '';
const trimmed = code.trim();

assert(trimmed.startsWith('(function(){') || trimmed.startsWith('(function () {'),
  'File starts with IIFE opening');
assert(trimmed.endsWith('})();'), 'File ends with IIFE closing');
assert(code.includes("'use strict'"), "File contains 'use strict'");

// =============================================================================
// SECTION 2: Global Exposure (window.* for onclick handlers)
// =============================================================================
console.log('\n=== Section 2: Global Exposure ===');

const globals = [
  'openEmailWizard', 'closeEmailWizard', 'openConfirmModal', 'closeConfirmModal',
  'ewGoTo', 'ewNext', 'switchChatView', 'syncInbox', 'openThread',
  'closeEmailDetail', 'toggleEmailActions', 'mailoDraft',
  'filterInbox', 'searchInbox', 'handleComposeEmail', '_initInbox'
];

for (const g of globals) {
  assert(code.includes('window.' + g),
    `Exposes window.${g}`);
}

// =============================================================================
// SECTION 3: Key Functions Present
// =============================================================================
console.log('\n=== Section 3: Key Functions ===');

const fns = [
  'openEmailWizard', 'closeEmailWizard', 'openConfirmModal', 'closeConfirmModal',
  'ewGoTo', 'ewNext',
  'switchChatView', '_inboxTimeAgo', '_renderThreadList', 'syncInbox',
  'openThread', 'closeEmailDetail', 'toggleEmailActions', 'mailoDraft',
  '_openReplyComposer', '_sendReply', 'filterInbox', 'searchInbox',
  'handleComposeEmail', '_openComposeModal', '_sendComposedEmail', '_initInbox'
];

for (const fn of fns) {
  assert(code.includes('function ' + fn),
    `Function ${fn} defined in extracted file`);
}

// =============================================================================
// SECTION 4: API Endpoints
// =============================================================================
console.log('\n=== Section 4: API Endpoints ===');

assert(code.includes('/api/inbox/status'), 'References /api/inbox/status');
assert(code.includes('/api/inbox/threads'), 'References /api/inbox/threads');
assert(code.includes('/api/inbox/send'), 'References /api/inbox/send');
assert(code.includes('/api/inbox/reply/'), 'References /api/inbox/reply/');
assert(code.includes('/api/inbox/threads/'), 'References /api/inbox/threads/:id (openThread)');
assert(code.includes('/api/settings'), 'References /api/settings (email wizard)');

// =============================================================================
// SECTION 5: Dependencies
// =============================================================================
console.log('\n=== Section 5: Dependencies ===');

assert(code.includes('escHtml'), 'Uses escHtml helper');
assert(code.includes('showToast'), 'Uses showToast helper');
assert(code.includes("localStorage.getItem('portal_token')"),
  'Reads portal_token from localStorage');
assert(code.includes('inboxView'),
  'References DOM element: inboxView');
assert(code.includes('emailWizardOverlay'),
  'References DOM element: emailWizardOverlay');
assert(code.includes('emailDetail'),
  'References DOM element: emailDetail');

// =============================================================================
// SECTION 6: Functions Removed from HTML
// =============================================================================
console.log('\n=== Section 6: Functions Removed from HTML ===');

const html = fs.readFileSync(HTML_FILE, 'utf8');

assert(!html.includes('// \u2500\u2500 AgentMail Inbox'),
  'AgentMail Inbox comment marker removed from HTML');

const syncInboxInHtml = (html.match(/function syncInbox/g) || []).length;
assert(syncInboxInHtml === 0,
  'function syncInbox no longer defined in HTML');

const initInboxInHtml = (html.match(/function _initInbox/g) || []).length;
assert(initInboxInHtml === 0,
  'function _initInbox no longer defined in HTML');

const openThreadInHtml = (html.match(/function openThread/g) || []).length;
assert(openThreadInHtml === 0,
  'function openThread no longer defined in HTML');

const switchChatViewInHtml = (html.match(/function switchChatView/g) || []).length;
assert(switchChatViewInHtml === 0,
  'function switchChatView no longer defined in HTML');

const openEmailWizardInHtml = (html.match(/function openEmailWizard/g) || []).length;
assert(openEmailWizardInHtml === 0,
  'function openEmailWizard no longer defined in HTML');

// =============================================================================
// SECTION 7: Script Tag in HTML
// =============================================================================
console.log('\n=== Section 7: Script Tag ===');

assert(html.includes('<script src="/static/js/features/inbox.js"></script>'),
  'HTML includes inbox.js script tag');

// Should appear after dock.js
const dockPos = html.indexOf('features/dock.js');
const inboxPos = html.indexOf('features/inbox.js');
assert(inboxPos > dockPos, 'inbox.js appears after dock.js');

// =============================================================================
// SECTION 8: No Duplicate Definitions
// =============================================================================
console.log('\n=== Section 8: No Duplicates ===');

const syncInboxInJs = (code.match(/function syncInbox/g) || []).length;
assert(syncInboxInJs === 1, 'syncInbox defined exactly once in inbox.js');

const initInboxInJs = (code.match(/function _initInbox/g) || []).length;
assert(initInboxInJs === 1, '_initInbox defined exactly once in inbox.js');

// =============================================================================
// SECTION 9: Sanity Checks
// =============================================================================
console.log('\n=== Section 9: Sanity Checks ===');

if (code.length > 0) {
  const lineCount = code.split('\n').length;
  assert(lineCount >= 200, `Line count >= 200 (got ${lineCount})`);
  assert(lineCount <= 600, `Line count <= 600 (got ${lineCount})`);
  assert(code.length >= 5000, `File size >= 5KB (got ${code.length})`);
}

// Adjacent code should still be in HTML
assert(html.includes('function setTheme'),
  'setTheme still in HTML (not accidentally removed)');
assert(html.includes('function filterTaskPills'),
  'filterTaskPills still in HTML (not accidentally removed)');

// IIFE 2d should still be intact
assert(html.includes('IIFE 2d'),
  'IIFE 2d still present in HTML (not accidentally removed)');

// Global helpers should still be defined in HTML (used by inbox.js as external deps)
assert(html.includes('function escHtml'),
  'escHtml still defined in HTML (global helper, not part of inbox module)');
assert(html.includes('showToast'),
  'showToast still referenced in HTML (global helper)');

// --- Summary ---
console.log(`\n${'='.repeat(50)}`);
console.log(`Results: ${passed} passed, ${failed} failed, ${passed + failed} total`);
if (failed > 0) {
  process.exit(1);
} else {
  console.log('All tests passed!');
}
