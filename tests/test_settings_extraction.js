#!/usr/bin/env node
/**
 * Settings Panel Extraction -- Pre/Post Verification Tests
 *
 * Tests that the Settings code (theme switching, toggle cards, digest cycling,
 * load/save settings) is correctly extracted from IIFE 2d in portal-pb-styled.html
 * into static/js/features/settings.js.
 *
 * These tests should FAIL before extraction and PASS after.
 *
 * Usage: node tests/test_settings_extraction.js
 */

const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const HTML_FILE = path.join(ROOT, 'portal-pb-styled.html');
const SETTINGS_JS = path.join(ROOT, 'static', 'js', 'features', 'settings.js');

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

assert(fs.existsSync(SETTINGS_JS), 'static/js/features/settings.js exists');

if (!fs.existsSync(SETTINGS_JS)) {
  console.error('\nFATAL: settings.js does not exist yet. Remaining tests will reference empty content.');
}

const settingsCode = fs.existsSync(SETTINGS_JS) ? fs.readFileSync(SETTINGS_JS, 'utf8') : '';

// =============================================================================
// SECTION 2: IIFE Structure
// =============================================================================
console.log('\n=== Section 2: IIFE Structure ===');

const trimmed = settingsCode.trim();
assert(trimmed.startsWith('(function(){') || trimmed.startsWith('(function () {'),
  'File starts with IIFE opening: (function(){');
assert(trimmed.endsWith('})();'),
  'File ends with IIFE closing: })();');
assert(settingsCode.includes("'use strict'"),
  "File contains 'use strict' directive");

// =============================================================================
// SECTION 3: Global Exposure
// =============================================================================
console.log('\n=== Section 3: Global Exposure ===');

assert(settingsCode.includes('window._portalSettings'),
  'Extracted file sets window._portalSettings');

// =============================================================================
// SECTION 4: Key Functions Present
// =============================================================================
console.log('\n=== Section 4: Key Functions Present ===');

assert(settingsCode.includes('function _loadSettings'),
  'Function _loadSettings is defined in extracted file');
assert(settingsCode.includes('function _saveSettingPartial'),
  'Function _saveSettingPartial is defined in extracted file');

// =============================================================================
// SECTION 5: Window Overrides
// =============================================================================
console.log('\n=== Section 5: Window Overrides ===');

assert(settingsCode.includes('window.setThemeFromSettings'),
  'File overrides window.setThemeFromSettings');
assert(settingsCode.includes('window.toggleSettingCard'),
  'File overrides window.toggleSettingCard');
assert(settingsCode.includes('window.cycleDigestFrequency'),
  'File overrides window.cycleDigestFrequency');

// =============================================================================
// SECTION 6: Auth Pattern
// =============================================================================
console.log('\n=== Section 6: Auth Pattern ===');

assert(settingsCode.includes("localStorage.getItem('portal_token')"),
  "Uses localStorage.getItem('portal_token') for auth");

// =============================================================================
// SECTION 7: API Endpoints
// =============================================================================
console.log('\n=== Section 7: API Endpoints ===');

assert(settingsCode.includes('/api/settings'),
  'References /api/settings endpoint');

// =============================================================================
// SECTION 8: Settings Removed from HTML
// =============================================================================
console.log('\n=== Section 8: Settings Removed from HTML ===');

const html = fs.readFileSync(HTML_FILE, 'utf8');

// _loadSettings definition should no longer exist inside IIFE 2d in the HTML
const loadSettingsInHtml = (html.match(/function _loadSettings/g) || []).length;
assert(loadSettingsInHtml === 0,
  'function _loadSettings no longer defined inline in HTML');

const savePartialInHtml = (html.match(/function _saveSettingPartial/g) || []).length;
assert(savePartialInHtml === 0,
  'function _saveSettingPartial no longer defined inline in HTML');

// The IIFE window.setThemeFromSettings override should be gone from inline JS
// Note: the original global stub (setThemeFromSettings) at ~line 3160 still remains,
// but the IIFE's override assignment "window.setThemeFromSettings = function" should be gone.
// We count assignments specifically (= function pattern inside script blocks).
const themeOverrideInHtml = (html.match(/window\.setThemeFromSettings\s*=\s*function/g) || []).length;
assert(themeOverrideInHtml === 0,
  'window.setThemeFromSettings override no longer defined inline in HTML');

const digestOverrideInHtml = (html.match(/window\.cycleDigestFrequency\s*=\s*function/g) || []).length;
assert(digestOverrideInHtml === 0,
  'window.cycleDigestFrequency override no longer defined inline in HTML');

// =============================================================================
// SECTION 9: Script Tag in HTML
// =============================================================================
console.log('\n=== Section 9: Script Tag in HTML ===');

assert(html.includes('<script src="/static/js/features/settings.js"></script>'),
  'HTML includes <script src="/static/js/features/settings.js"></script>');

// Verify ordering: settings.js should appear after dock.js
const dockPos = html.indexOf('features/dock.js');
const settingsPos = html.indexOf('features/settings.js');
assert(settingsPos > dockPos,
  'settings.js script tag appears after dock.js');

// =============================================================================
// SECTION 10: No Duplicate Globals
// =============================================================================
console.log('\n=== Section 10: No Duplicate Globals ===');

// window._portalSettings should NOT be set in HTML anymore
const portalSettingsInHtml = (html.match(/window\._portalSettings/g) || []).length;
assert(portalSettingsInHtml === 0,
  'window._portalSettings does not appear in HTML (fully extracted)');

// It should appear exactly once in the extracted file
const portalSettingsInJs = (settingsCode.match(/window\._portalSettings/g) || []).length;
assert(portalSettingsInJs === 1,
  'window._portalSettings appears exactly once in settings.js');

// =============================================================================
// SECTION 11: Sanity Checks
// =============================================================================
console.log('\n=== Section 11: Sanity Checks ===');

// IIFE 2d comment may still exist (tasks/bookmarks/boop not yet extracted)
// We check it still references at least TASKS (since SETTINGS and HUB were extracted)
assert(html.includes('IIFE 2d') || html.includes('IIFE 2d'),
  'IIFE 2d marker still present in HTML (tasks/hub/bookmarks/boop remain)');

// window._portalTasks must still be defined in HTML (not accidentally removed)
assert(html.includes('window._portalTasks'),
  'window._portalTasks still defined in HTML (not accidentally removed)');

// window._portalHub must still be defined somewhere (HTML or hub.js)
const hubJsPath = path.join(ROOT, 'static', 'js', 'features', 'hub.js');
const hubInHtml = html.includes('window._portalHub');
const hubInFile = fs.existsSync(hubJsPath) && fs.readFileSync(hubJsPath, 'utf8').includes('window._portalHub');
assert(hubInHtml || hubInFile,
  'window._portalHub still defined in HTML or in hub.js (not accidentally removed)');

// Global stub functions should still exist in HTML (they are outside IIFE 2d)
// These are the original simple stubs that settings.js overrides at runtime
assert(html.includes('function setThemeFromSettings'),
  'Global stub setThemeFromSettings still defined in HTML');
assert(html.includes('function toggleSettingCard'),
  'Global stub toggleSettingCard still defined in HTML');
assert(html.includes('function cycleDigestFrequency'),
  'Global stub cycleDigestFrequency still defined in HTML');

// --- Summary ---
console.log(`\n${'='.repeat(50)}`);
console.log(`Results: ${passed} passed, ${failed} failed`);
if (failed > 0) {
  process.exit(1);
} else {
  console.log('All tests passed!');
}
