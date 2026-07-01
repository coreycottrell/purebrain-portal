#!/usr/bin/env node
/**
 * PORTAL_FILE Card Renderer -- Parser + Render Verification Tests
 *
 * Restores the dropped [PORTAL_FILE:stored_name:display_name] card renderer in
 * static/js/features/chat.js. The backend emits this tag (portal_server.py:2123)
 * and serves the file at GET /api/chat/uploads/{stored_name}?token=... (token-authed,
 * 401 without). The frontend parser/renderer was dropped in a rebuild, leaving the
 * tag to render as dead raw text to the human.
 *
 * SPEC (Aether/Jared, 2026-06-18):
 *  1. stored_name is colon-free ([A-Za-z0-9._-]). display_name MAY contain spaces/colons.
 *     Split on the FIRST colon AFTER the "PORTAL_FILE:" prefix.
 *  2. Build token-authed URL /api/chat/uploads/{stored_name}?token={portal_token}
 *     for BOTH the inline preview AND the download link. Token from
 *     localStorage.getItem('portal_token').
 *  3. Render a styled card: inline preview for md/text/img/pdf (by stored_name ext)
 *     + a download button. Match existing chat card/message styling.
 *
 * These tests FAIL before the renderer is restored and PASS after.
 *
 * Strategy: the parser is exposed as a pure function `parsePortalFile` so we can
 * exercise the REAL parsing logic (incl. the first-colon-split edge case) in a
 * sandbox, without a DOM. Render presence is verified via static source analysis.
 *
 * Usage: node tests/test_portal_file_card.js
 */

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.resolve(__dirname, '..');
const CHAT_JS = path.join(ROOT, 'static', 'js', 'features', 'chat.js');

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

const chatCode = fs.existsSync(CHAT_JS) ? fs.readFileSync(CHAT_JS, 'utf8') : '';

// =============================================================================
// Extract the pure parsePortalFile function and evaluate it in a sandbox.
// We pull the function source by name so we test the SHIPPED implementation,
// not a re-implementation in the test.
// =============================================================================
function loadParser() {
  // Match: function parsePortalFile(<args>) { ... } up to its matching closing brace.
  const startIdx = chatCode.indexOf('function parsePortalFile');
  if (startIdx === -1) return null;
  // Brace-match from the first '{' after the signature.
  let i = chatCode.indexOf('{', startIdx);
  if (i === -1) return null;
  let depth = 0;
  let end = -1;
  for (; i < chatCode.length; i++) {
    const ch = chatCode[i];
    if (ch === '{') depth++;
    else if (ch === '}') {
      depth--;
      if (depth === 0) { end = i + 1; break; }
    }
  }
  if (end === -1) return null;
  const fnSrc = chatCode.slice(startIdx, end);
  const sandbox = {};
  vm.createContext(sandbox);
  // Expose the function in the sandbox, then return it.
  vm.runInContext(fnSrc + '\n;this.__parse = parsePortalFile;', sandbox);
  return sandbox.__parse;
}

// =============================================================================
// SECTION 1: Parser exists and is a pure function
// =============================================================================
console.log('\n=== Section 1: Parser Exists ===');
assert(fs.existsSync(CHAT_JS), 'static/js/features/chat.js exists');
assert(chatCode.includes('function parsePortalFile'),
  'Pure parser function parsePortalFile is defined');

const parse = loadParser();
assert(typeof parse === 'function',
  'parsePortalFile is extractable and evaluates as a function');

// =============================================================================
// SECTION 2: Basic parse (simple display_name)
// =============================================================================
console.log('\n=== Section 2: Basic Parse ===');
if (typeof parse === 'function') {
  const r = parse('[PORTAL_FILE:1750000000000_report.md:report.md]');
  assert(r !== null, 'Parses a simple PORTAL_FILE tag');
  assert(r && r.storedName === '1750000000000_report.md',
    'stored_name = "1750000000000_report.md"');
  assert(r && r.displayName === 'report.md',
    'display_name = "report.md"');
}

// =============================================================================
// SECTION 3: CRITICAL EDGE CASE -- first-colon split with display_name
//            containing BOTH spaces AND a colon.
//            "report:final version.md" must stay intact as the display_name.
// =============================================================================
console.log('\n=== Section 3: First-Colon-Split Edge Case ===');
if (typeof parse === 'function') {
  const edge = parse('[PORTAL_FILE:1750000000000_report.md:report:final version.md]');
  assert(edge !== null, 'Parses tag whose display_name has spaces AND a colon');
  assert(edge && edge.storedName === '1750000000000_report.md',
    'stored_name stops at FIRST colon: "1750000000000_report.md"');
  assert(edge && edge.displayName === 'report:final version.md',
    'display_name keeps everything after first colon: "report:final version.md"');

  // Another: multiple colons + spaces in display name
  const edge2 = parse('[PORTAL_FILE:abc-123_data.pdf:Q3 2026: Final: Results.pdf]');
  assert(edge2 && edge2.storedName === 'abc-123_data.pdf',
    'multi-colon: stored_name = "abc-123_data.pdf"');
  assert(edge2 && edge2.displayName === 'Q3 2026: Final: Results.pdf',
    'multi-colon: display_name = "Q3 2026: Final: Results.pdf"');
}

// =============================================================================
// SECTION 4: Non-matching / malformed input returns null
// =============================================================================
console.log('\n=== Section 4: Non-Matching Input ===');
if (typeof parse === 'function') {
  assert(parse('just a normal message') === null,
    'Plain text returns null');
  assert(parse('[PORTAL_FILE:no-colon-here]') === null,
    'Tag with no display_name separator returns null');
  assert(parse('') === null, 'Empty string returns null');
}

// =============================================================================
// SECTION 5: Extension detection helper (preview type by stored_name ext)
// =============================================================================
console.log('\n=== Section 5: Preview Type Detection ===');
if (typeof parse === 'function') {
  const img = parse('[PORTAL_FILE:123_pic.PNG:pic.png]');
  assert(img && img.kind === 'image', 'PNG (case-insensitive) -> kind "image"');
  const pdf = parse('[PORTAL_FILE:123_doc.pdf:doc.pdf]');
  assert(pdf && pdf.kind === 'pdf', 'pdf -> kind "pdf"');
  const md = parse('[PORTAL_FILE:123_notes.md:notes.md]');
  assert(md && md.kind === 'text', 'md -> kind "text"');
  const txt = parse('[PORTAL_FILE:123_log.txt:log.txt]');
  assert(txt && txt.kind === 'text', 'txt -> kind "text"');
  const other = parse('[PORTAL_FILE:123_archive.zip:archive.zip]');
  assert(other && other.kind === 'file', 'zip -> kind "file" (download only)');
}

// =============================================================================
// SECTION 6: Renderer present and CIV-agnostic / token-authed
// =============================================================================
console.log('\n=== Section 6: Renderer Integration (static analysis) ===');
assert(chatCode.includes('PORTAL_FILE'),
  'chat.js references PORTAL_FILE (renderer restored)');
assert(/function\s+renderPortalFileCard/.test(chatCode) ||
       chatCode.includes('portal-file-card'),
  'A render path for the file card exists (renderPortalFileCard / portal-file-card)');

// Token-authed URL must be built with localStorage token (CIV-agnostic).
assert(chatCode.includes("/api/chat/uploads/"),
  'Builds /api/chat/uploads/ URL for the file');
assert(/uploads\/'\s*\+\s*encodeURIComponent[\s\S]{0,80}\?token=/.test(chatCode) ||
       /\?token='\s*\+\s*encodeURIComponent/.test(chatCode),
  'URL includes ?token= (token-authed for preview AND download)');

// No hardcoded CIV names/paths in the new card code (CIV-agnostic).
assert(!/PORTAL_FILE[\s\S]{0,2000}\/home\//.test(chatCode),
  'No hardcoded /home/ filesystem paths near PORTAL_FILE rendering');

// Download affordance present.
assert(/download/i.test(chatCode),
  'Card provides a download affordance (download link/button)');

// =============================================================================
// SECTION 7: Wired into the message build path
// =============================================================================
console.log('\n=== Section 7: Wired Into buildMessageEl ===');
// The parser should be consulted inside the assistant render branch.
assert(chatCode.includes('parsePortalFile('),
  'parsePortalFile is actually CALLED (wired into render path)');

// --- Summary ---
console.log(`\n${'='.repeat(50)}`);
console.log(`Results: ${passed} passed, ${failed} failed, ${passed + failed} total`);
if (failed > 0) {
  process.exit(1);
} else {
  console.log('All tests passed!');
}
