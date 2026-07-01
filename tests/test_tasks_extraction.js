#!/usr/bin/env node
/**
 * Tasks + Todo Panel Extraction -- Pre/Post Verification Tests
 *
 * Tests that the Tasks and Todo functions (from IIFE 2d: TASKS, TODO, HUB, BOOP)
 * are correctly extracted from portal-pb-styled.html into static/js/features/tasks.js.
 *
 * The extraction covers:
 *   - Scheduled tasks: _loadTasks, _renderTasks, _updateStats, _updateTasksBadge,
 *     _scheduleTask, _deleteTask, _patchStatus
 *   - Hub todo: _loadHubTasks, _renderTodoPanel, _buildTodoCard, _filterTodoCards,
 *     _updateTodoStats
 *   - Global exposure: window._portalTasks
 *
 * These tests should FAIL before extraction and PASS after.
 *
 * Usage: node tests/test_tasks_extraction.js
 */

const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const HTML_FILE = path.join(ROOT, 'portal-pb-styled.html');
const TASKS_JS = path.join(ROOT, 'static', 'js', 'features', 'tasks.js');

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
console.log('\n=== Section 1: File Exists ===');

assert(fs.existsSync(TASKS_JS), 'static/js/features/tasks.js exists');

if (!fs.existsSync(TASKS_JS)) {
  console.error('\nFATAL: tasks.js does not exist yet. Remaining tests will reference empty content.');
}

const tasksCode = fs.existsSync(TASKS_JS) ? fs.readFileSync(TASKS_JS, 'utf8') : '';

// =============================================================================
// SECTION 2: IIFE Structure
// =============================================================================
console.log('\n=== Section 2: IIFE Structure ===');

const trimmed = tasksCode.trim();
assert(trimmed.startsWith('(function(){') || trimmed.startsWith('(function () {'),
  'File starts with IIFE opening: (function(){');
assert(trimmed.endsWith('})();'),
  'File ends with IIFE closing: })();');
assert(tasksCode.includes("'use strict'"),
  "File contains 'use strict' directive");

// =============================================================================
// SECTION 3: Global Exposure
// =============================================================================
console.log('\n=== Section 3: Global Exposure ===');

assert(tasksCode.includes('window._portalTasks'),
  'Extracted file sets window._portalTasks');

// Verify the exported API shape
assert(tasksCode.includes('loadTasks') && tasksCode.includes('loadHubTasks'),
  'window._portalTasks exports loadTasks and loadHubTasks');
assert(tasksCode.includes('scheduleTask') && tasksCode.includes('deleteTask'),
  'window._portalTasks exports scheduleTask and deleteTask');
assert(tasksCode.includes('patchStatus'),
  'window._portalTasks exports patchStatus');

// =============================================================================
// SECTION 4: Key Task Functions
// =============================================================================
console.log('\n=== Section 4: Key Task Functions ===');

const taskFunctions = [
  '_loadTasks',
  '_renderTasks',
  '_updateStats',
  '_updateTasksBadge',
  '_scheduleTask',
  '_deleteTask',
  '_patchStatus',
];

for (const fn of taskFunctions) {
  assert(tasksCode.includes('function ' + fn),
    `Function ${fn} is defined in extracted file`);
}

// =============================================================================
// SECTION 5: Key Todo Functions
// =============================================================================
console.log('\n=== Section 5: Key Todo Functions ===');

const todoFunctions = [
  '_loadHubTasks',
  '_renderTodoPanel',
  '_buildTodoCard',
  '_filterTodoCards',
  '_updateTodoStats',
];

for (const fn of todoFunctions) {
  assert(tasksCode.includes('function ' + fn),
    `Function ${fn} is defined in extracted file`);
}

// =============================================================================
// SECTION 6: Auth Pattern
// =============================================================================
console.log('\n=== Section 6: Auth Pattern ===');

assert(tasksCode.includes("localStorage.getItem('portal_token')"),
  'Uses localStorage.getItem(portal_token) for auth');

// =============================================================================
// SECTION 7: API Endpoints
// =============================================================================
console.log('\n=== Section 7: API Endpoints ===');

assert(tasksCode.includes('/api/scheduled-tasks'),
  'References /api/scheduled-tasks endpoint');
assert(tasksCode.includes('/api/schedule-task'),
  'References /api/schedule-task endpoint (POST new task)');
assert(tasksCode.includes('/api/hub/tasks'),
  'References /api/hub/tasks endpoint');

// =============================================================================
// SECTION 8: DOM Element References
// =============================================================================
console.log('\n=== Section 8: DOM Element References ===');

const requiredDomIds = [
  'tasksTableBody',
  'todo-sections-wrap',
  'tasks-sidebar-badge',
  'todo-sidebar-badge',
];

for (const domId of requiredDomIds) {
  assert(tasksCode.includes(domId),
    `References DOM element: ${domId}`);
}

// =============================================================================
// SECTION 9: Tasks Removed from HTML
// =============================================================================
console.log('\n=== Section 9: Tasks Removed from HTML ===');

const html = fs.readFileSync(HTML_FILE, 'utf8');

// Key task function definitions should no longer be inline in HTML
const loadTasksInHtml = (html.match(/function _loadTasks\b/g) || []).length;
assert(loadTasksInHtml === 0,
  'function _loadTasks no longer defined inline in HTML');

const renderTasksInHtml = (html.match(/function _renderTasks\b/g) || []).length;
assert(renderTasksInHtml === 0,
  'function _renderTasks no longer defined inline in HTML');

const loadHubTasksInHtml = (html.match(/function _loadHubTasks\b/g) || []).length;
assert(loadHubTasksInHtml === 0,
  'function _loadHubTasks no longer defined inline in HTML');

const buildTodoCardInHtml = (html.match(/function _buildTodoCard\b/g) || []).length;
assert(buildTodoCardInHtml === 0,
  'function _buildTodoCard no longer defined inline in HTML');

const renderTodoPanelInHtml = (html.match(/function _renderTodoPanel\b/g) || []).length;
assert(renderTodoPanelInHtml === 0,
  'function _renderTodoPanel no longer defined inline in HTML');

// =============================================================================
// SECTION 10: Script Tag in HTML
// =============================================================================
console.log('\n=== Section 10: Script Tag in HTML ===');

assert(html.includes('<script src="/static/js/features/tasks.js"></script>'),
  'HTML includes <script src="/static/js/features/tasks.js"></script>');

// Verify ordering: tasks.js should appear after agents.js (since agents was extracted earlier)
const agentsPos = html.indexOf('features/agents.js');
const tasksPos = html.indexOf('features/tasks.js');
assert(tasksPos > agentsPos,
  'tasks.js script tag appears after agents.js');

// =============================================================================
// SECTION 11: No Duplicate Globals
// =============================================================================
console.log('\n=== Section 11: No Duplicate Globals ===');

// window._portalTasks should NOT be assigned in the HTML anymore
const portalTasksInHtml = (html.match(/window\._portalTasks\s*=/g) || []).length;
assert(portalTasksInHtml === 0,
  'window._portalTasks assignment does not appear in HTML (fully extracted)');

// It should appear exactly once in the extracted file (the assignment)
const portalTasksInJs = (tasksCode.match(/window\._portalTasks\s*=/g) || []).length;
assert(portalTasksInJs === 1,
  'window._portalTasks is assigned exactly once in tasks.js');

// =============================================================================
// SECTION 12: Sanity Checks
// =============================================================================
console.log('\n=== Section 12: Sanity Checks ===');

// Global stub functions should STILL exist in HTML (they delegate to window._portalTasks)
assert(html.includes('function scheduleTask'),
  'Global stub scheduleTask() still defined in HTML (delegates to _portalTasks)');
assert(html.includes('function deleteScheduledTask'),
  'Global stub deleteScheduledTask() still defined in HTML (delegates to _portalTasks)');
assert(html.includes('function updateTasksBadge'),
  'Global stub updateTasksBadge() still defined in HTML (delegates to _portalTasks)');
assert(html.includes('function renderScheduledInTasks'),
  'Global stub renderScheduledInTasks() still defined in HTML (delegates to _portalTasks)');

// The Hub/Boop section and boot function may still be inline in HTML
// (they belong to a different extraction scope)
// OR they may have been extracted together -- either way, the boot should call _loadTasks
assert(tasksCode.includes('portal-auth') || html.includes('portal-auth'),
  'portal-auth event listener exists (in tasks.js or remaining in HTML)');

// File size sanity: the tasks + todo section is roughly lines 2430-2692 (~260 lines)
// plus helpers and boot glue, so expect 200-450 lines
if (tasksCode.length > 0) {
  const lineCount = tasksCode.split('\n').length;
  assert(lineCount >= 150, `Extracted file has >= 150 lines (got ${lineCount}; not a stub)`);
  assert(lineCount <= 500, `Extracted file has <= 500 lines (got ${lineCount}; not bloated)`);
  assert(tasksCode.length >= 4000, `File size >= 4KB (got ${tasksCode.length}; not a stub)`);
}

// --- Summary ---
console.log(`\n${'='.repeat(50)}`);
console.log(`Results: ${passed} passed, ${failed} failed out of ${passed + failed} assertions`);
if (failed > 0) {
  process.exit(1);
} else {
  console.log('All tests passed!');
}
