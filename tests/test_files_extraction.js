#!/usr/bin/env node
/**
 * Files Panel Extraction -- Pre/Post Verification Tests
 *
 * Tests that the Files IIFE (IIFE 2e: FILES PANEL -- Real Backend Wiring)
 * is correctly extracted from portal-pb-styled.html into
 * static/js/features/files.js.
 *
 * The extraction covers:
 *   - File listing:  _loadFiles, _renderView, _renderGrid, _renderList
 *   - Navigation:    _navigate, _goUp, _updateBreadcrumb
 *   - Upload:        _handleUpload, _onUploadsDone
 *   - Helpers:       _esc, _tok, _auth, _fmtSize, _getExt, _getCat,
 *                    _getIconClass, _getTypeLabel, _buildCardHtml
 *   - UI:            _filterCategory, _getFilteredItems, _initSearch,
 *                    _updateSidebarCounts, _updateStorageBar, _initRefs
 *   - Boot:          _boot
 *   - Global exposure: window._portalFiles, window._portalDownload
 *
 * These tests should FAIL before extraction and PASS after.
 *
 * Usage: node tests/test_files_extraction.js
 */

const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const HTML_FILE = path.join(ROOT, 'portal-pb-styled.html');
const FILES_JS = path.join(ROOT, 'static', 'js', 'features', 'files.js');

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

assert(fs.existsSync(FILES_JS), 'static/js/features/files.js exists');

if (!fs.existsSync(FILES_JS)) {
  console.error('\nFATAL: files.js does not exist yet. Remaining tests will reference empty content.');
}

const filesCode = fs.existsSync(FILES_JS) ? fs.readFileSync(FILES_JS, 'utf8') : '';

// =============================================================================
// SECTION 2: IIFE Structure
// =============================================================================
console.log('\n=== Section 2: IIFE Structure ===');

const trimmed = filesCode.trim();
assert(trimmed.startsWith('(function(){') || trimmed.startsWith('(function () {'),
  'File starts with IIFE opening: (function(){');
assert(trimmed.endsWith('})();'),
  'File ends with IIFE closing: })();');

assert(filesCode.includes("'use strict'"), "File contains 'use strict' directive");

// =============================================================================
// SECTION 3: Global Exposure (window._portalFiles)
// =============================================================================
console.log('\n=== Section 3: Global Exposure ===');

assert(filesCode.includes('window._portalFiles'),
  'Extracted file exposes window._portalFiles');

// Verify the public API surface on _portalFiles
assert(filesCode.includes('navigate') && filesCode.includes('_navigate'),
  'window._portalFiles exposes navigate method');
assert(filesCode.includes('goUp') && filesCode.includes('_goUp'),
  'window._portalFiles exposes goUp method');
assert(filesCode.includes('filterCategory') && filesCode.includes('_filterCategory'),
  'window._portalFiles exposes filterCategory method');
assert(filesCode.includes('handleUpload') && filesCode.includes('_handleUpload'),
  'window._portalFiles exposes handleUpload method');

// Also exposes window._portalDownload (secure blob download helper)
assert(filesCode.includes('window._portalDownload'),
  'Extracted file exposes window._portalDownload');

// =============================================================================
// SECTION 4: Key Functions Present (at least 8 core functions)
// =============================================================================
console.log('\n=== Section 4: Key Functions Present ===');

const requiredFunctions = [
  '_loadFiles',
  '_renderView',
  '_renderGrid',
  '_renderList',
  '_buildCardHtml',
  '_navigate',
  '_goUp',
  '_handleUpload',
  '_onUploadsDone',
  '_updateBreadcrumb',
  '_filterCategory',
  '_getFilteredItems',
  '_initSearch',
  '_updateSidebarCounts',
  '_updateStorageBar',
  '_initRefs',
  '_boot',
];

for (const fn of requiredFunctions) {
  assert(filesCode.includes('function ' + fn),
    `Function ${fn} is defined in extracted file`);
}

// =============================================================================
// SECTION 5: Auth Pattern
// =============================================================================
console.log('\n=== Section 5: Auth Pattern ===');

assert(filesCode.includes("localStorage.getItem('portal_token')"),
  'Uses localStorage.getItem(portal_token) for auth');

assert(filesCode.includes("'Authorization'") && filesCode.includes("'Bearer '"),
  'Constructs Authorization: Bearer header');

assert(filesCode.includes("'portal-auth'"),
  'Listens for portal-auth custom event to re-boot on login');

// =============================================================================
// SECTION 6: API Endpoints (all fetch targets present)
// =============================================================================
console.log('\n=== Section 6: API Endpoints ===');

assert(filesCode.includes('/api/download/list'),
  'References /api/download/list endpoint (directory listing)');

assert(filesCode.includes('/api/chat/upload'),
  'References /api/chat/upload endpoint (file upload)');

assert(filesCode.includes('/api/download?path='),
  'References /api/download?path= endpoint (secure blob download)');

// =============================================================================
// SECTION 7: Files IIFE Removed from HTML
// =============================================================================
console.log('\n=== Section 7: Files IIFE Removed from HTML ===');

const html = fs.readFileSync(HTML_FILE, 'utf8');

assert(!html.includes('// ===== IIFE 2e: FILES PANEL'),
  'IIFE 2e comment marker removed from HTML');

// Verify key function definitions are gone from inline HTML
const loadFilesInHtml = (html.match(/function _loadFiles/g) || []).length;
assert(loadFilesInHtml === 0,
  'function _loadFiles no longer defined inline in HTML');

const renderGridInHtml = (html.match(/function _renderGrid/g) || []).length;
assert(renderGridInHtml === 0,
  'function _renderGrid no longer defined inline in HTML');

const handleUploadInHtml = (html.match(/function _handleUpload/g) || []).length;
assert(handleUploadInHtml === 0,
  'function _handleUpload no longer defined inline in HTML');

// =============================================================================
// SECTION 8: Script Tag Present in HTML
// =============================================================================
console.log('\n=== Section 8: Script Tag in HTML ===');

assert(html.includes('<script src="/static/js/features/files.js"></script>'),
  'HTML includes <script src="/static/js/features/files.js"></script>');

// Verify ordering: files.js should appear after panel-manager.js and dock.js
const panelManagerPos = html.indexOf('panel-manager.js');
const dockPos = html.indexOf('features/dock.js');
const filesPos = html.indexOf('features/files.js');

assert(filesPos > panelManagerPos,
  'files.js script tag appears after panel-manager.js');
assert(filesPos > dockPos,
  'files.js script tag appears after dock.js');

// =============================================================================
// SECTION 9: No Duplicate Globals
// =============================================================================
console.log('\n=== Section 9: No Duplicate Globals ===');

// window._portalFiles should NOT appear in the HTML (only in extracted file)
const portalFilesInHtml = (html.match(/window\._portalFiles\s*=/g) || []).length;
assert(portalFilesInHtml === 0,
  'window._portalFiles assignment does not appear in HTML (fully extracted)');

// It should appear exactly once (the assignment) in the extracted file
const portalFilesAssignInJs = (filesCode.match(/window\._portalFiles\s*=/g) || []).length;
assert(portalFilesAssignInJs === 1,
  'window._portalFiles is assigned exactly once in files.js');

// window._portalDownload should NOT be defined in the HTML anymore
const portalDownloadInHtml = (html.match(/window\._portalDownload\s*=/g) || []).length;
assert(portalDownloadInHtml === 0,
  'window._portalDownload assignment does not appear in HTML (fully extracted)');

const portalDownloadInJs = (filesCode.match(/window\._portalDownload\s*=/g) || []).length;
assert(portalDownloadInJs === 1,
  'window._portalDownload is assigned exactly once in files.js');

// =============================================================================
// SECTION 10: Sanity Checks
// =============================================================================
console.log('\n=== Section 10: Sanity Checks ===');

if (filesCode.length > 0) {
  // The IIFE spans lines 6042-6499 (~457 lines). Extracted file should be similar.
  const lineCount = filesCode.split('\n').length;
  assert(lineCount >= 350, `Extracted file has >= 350 lines (got ${lineCount}; expect ~460)`);
  assert(lineCount <= 700, `Extracted file has <= 700 lines (got ${lineCount}; not bloated)`);

  // File should be non-trivial size
  assert(filesCode.length >= 10000, `File size >= 10KB (got ${filesCode.length}; not a stub)`);
}

// The Google Drive integration IIFE that follows should still exist in HTML
assert(html.includes('GOOGLE DRIVE INTEGRATION'),
  'Google Drive integration block still present in HTML (not accidentally removed)');

// The IIFE 2d (BOOP) that precedes should still exist in HTML
assert(html.includes('IIFE 2d') || html.includes('IIFE 2d: BOOP'),
  'IIFE 2d (BOOP) still present in HTML (not accidentally removed)');

// DOM elements referenced by files panel should still be in HTML (they are in the markup, not the IIFE)
assert(html.includes('id="filesArea"'),
  'filesArea container still present in HTML markup');
assert(html.includes('id="files-grid-content"'),
  'files-grid-content container still present in HTML markup');
assert(html.includes('id="files-breadcrumb"'),
  'files-breadcrumb container still present in HTML markup');

// HTML references to window._portalFiles in onclick handlers should remain
// (these are in the HTML markup, not inside the IIFE)
const portalFilesOnclick = (html.match(/window\._portalFiles\./g) || []).length;
assert(portalFilesOnclick >= 1,
  'HTML markup still has onclick references to window._portalFiles (e.g., goUp, navigate, handleUpload)');

// --- Summary ---
console.log(`\n${'='.repeat(50)}`);
console.log(`Results: ${passed} passed, ${failed} failed`);
if (failed > 0) {
  process.exit(1);
} else {
  console.log('All tests passed!');
}
