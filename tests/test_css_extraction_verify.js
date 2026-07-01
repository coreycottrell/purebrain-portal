#!/usr/bin/env node
/**
 * CSS Extraction -- Post-Extraction Verification
 *
 * Run AFTER extracting CSS from portal-pb-styled.html into separate files.
 * Verifies visual parity, completeness, and correctness.
 *
 * Prerequisites:
 *   1. Run test_css_extraction_baseline.js BEFORE extraction
 *   2. Extract CSS into static/css/{base,components,panels,dock,themes}.css
 *   3. Update portal-pb-styled.html: remove <style>, add <link> tags
 *   4. Run this script
 *
 * Usage: node tests/test_css_extraction_verify.js
 */

const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const HTML_FILE = path.join(ROOT, 'portal-pb-styled.html');
const CSS_DIR = path.join(ROOT, 'static', 'css');
const BASELINE_FILE = path.join(__dirname, 'css_baseline.txt');
const META_FILE = path.join(__dirname, 'css_baseline_meta.json');

// Expected CSS files in cascade order (order matters for specificity)
const CSS_FILES_ORDERED = [
  'base.css',
  'components.css',
  'panels.css',
  'dock.css',
  'themes.css',
];

let passed = 0;
let failed = 0;
let warnings = 0;

function assert(condition, msg) {
  if (condition) {
    passed++;
    console.log(`  PASS: ${msg}`);
  } else {
    failed++;
    console.error(`  FAIL: ${msg}`);
  }
}

function warn(msg) {
  warnings++;
  console.log(`  WARN: ${msg}`);
}

// =============================================================================
// SECTION 1: Prerequisite Checks
// =============================================================================
console.log('\n=== Section 1: Prerequisite Checks ===');

assert(fs.existsSync(BASELINE_FILE), 'Baseline CSS file exists (run baseline script first)');
assert(fs.existsSync(META_FILE), 'Baseline metadata file exists');
assert(fs.existsSync(HTML_FILE), 'portal-pb-styled.html exists');

if (!fs.existsSync(BASELINE_FILE) || !fs.existsSync(META_FILE)) {
  console.error('\nFATAL: Baseline files missing. Run test_css_extraction_baseline.js first.');
  process.exit(1);
}

const baseline = fs.readFileSync(BASELINE_FILE, 'utf8');
const meta = JSON.parse(fs.readFileSync(META_FILE, 'utf8'));

// Check all CSS files exist
for (const file of CSS_FILES_ORDERED) {
  const fullPath = path.join(CSS_DIR, file);
  assert(fs.existsSync(fullPath), `${file} exists in static/css/`);
}

// =============================================================================
// SECTION 2: Style Block Removal
// =============================================================================
console.log('\n=== Section 2: Style Block Removal ===');

const html = fs.readFileSync(HTML_FILE, 'utf8');
const styleBlockMatch = html.match(/<style>([\s\S]*?)<\/style>/);

// The HTML may still have a <style> block if there are inline JS-generated styles,
// but the ORIGINAL large CSS block should be gone.
if (styleBlockMatch) {
  const remainingCSS = styleBlockMatch[1].trim();
  // If there is a remaining style block, it should be tiny (maybe empty or just a few lines)
  const remainingLines = remainingCSS.split('\n').length;
  if (remainingLines > 10) {
    assert(false, `<style> block should be removed or near-empty (found ${remainingLines} lines)`);
  } else if (remainingLines > 0 && remainingCSS.length > 0) {
    warn(`Small <style> block remains (${remainingLines} lines, ${remainingCSS.length} chars) -- verify this is intentional`);
    assert(true, '<style> block is acceptably small (likely dynamic/runtime styles)');
  } else {
    assert(true, '<style> block is empty or removed');
  }
} else {
  assert(true, '<style> block fully removed from HTML');
}

// =============================================================================
// SECTION 3: Link Tag Verification
// =============================================================================
console.log('\n=== Section 3: Link Tag Order ===');

const linkRe = /<link[^>]+href="([^"]*\.css)"[^>]*>/g;
const foundLinks = [];
let m;
while ((m = linkRe.exec(html)) !== null) {
  const href = m[1];
  // Only track our CSS files (not Google Fonts, etc.)
  if (href.includes('static/css/') || href.startsWith('static/css/')) {
    foundLinks.push(path.basename(href));
  }
}

assert(foundLinks.length === CSS_FILES_ORDERED.length,
  `Found ${foundLinks.length} CSS <link> tags (expected ${CSS_FILES_ORDERED.length})`);

// Verify order matches expected cascade
let orderCorrect = true;
for (let i = 0; i < CSS_FILES_ORDERED.length; i++) {
  if (foundLinks[i] !== CSS_FILES_ORDERED[i]) {
    orderCorrect = false;
    assert(false, `Link tag #${i + 1}: found "${foundLinks[i]}", expected "${CSS_FILES_ORDERED[i]}"`);
  }
}
if (orderCorrect && foundLinks.length === CSS_FILES_ORDERED.length) {
  assert(true, 'CSS <link> tags are in correct cascade order');
}

// Verify link tags appear before </head>
const headEnd = html.indexOf('</head>');
const lastLinkIdx = html.lastIndexOf('static/css/');
if (headEnd > 0 && lastLinkIdx > 0) {
  assert(lastLinkIdx < headEnd, 'All CSS <link> tags are inside <head>');
}

// =============================================================================
// SECTION 4: CSS Completeness (Content Parity)
// =============================================================================
console.log('\n=== Section 4: CSS Content Completeness ===');

// Concatenate all CSS files in link order
let concatenated = '';
for (const file of CSS_FILES_ORDERED) {
  const fullPath = path.join(CSS_DIR, file);
  if (fs.existsSync(fullPath)) {
    concatenated += fs.readFileSync(fullPath, 'utf8') + '\n';
  }
}

// Normalize for comparison: strip comments, collapse whitespace
function normalize(cssText) {
  return cssText
    .replace(/\/\*[\s\S]*?\*\//g, '')   // Remove comments
    .replace(/\s+/g, ' ')                // Collapse whitespace
    .replace(/\s*([{}:;,>~+])\s*/g, '$1') // Remove space around punctuation
    .trim();
}

const normalizedBaseline = normalize(baseline);
const normalizedCombined = normalize(concatenated);

// Content parity check: first try byte-for-byte, then fall back to rule-set comparison.
// Rule-set comparison is needed because extracting dock rules into dock.css changes
// concatenation order (dock appears after panels in link order but before panels in baseline).
// Both approaches verify that no CSS rules are lost, duplicated, or modified.
if (normalizedBaseline === normalizedCombined) {
  assert(true, 'Normalized concatenated CSS matches baseline (byte-for-byte parity)');
} else {
  // Same length but different order = reordering from extraction (expected for dock)
  const bLen = normalizedBaseline.length;
  const cLen = normalizedCombined.length;
  console.log(`    Byte-for-byte: SKIP (dock extraction reorders content)`);
  console.log(`    Baseline: ${bLen} chars, Combined: ${cLen} chars, Diff: ${Math.abs(bLen - cLen)}`);

  // Rule-set parity: extract all rule blocks and compare as sorted sets.
  // This catches missing/extra/modified rules but allows reordering.
  function extractRuleSet(normed) {
    const rules = [];
    let i = 0;
    while (i < normed.length) {
      const brace = normed.indexOf('{', i);
      if (brace === -1) break;
      let depth = 1;
      let j = brace + 1;
      while (j < normed.length && depth > 0) {
        if (normed[j] === '{') depth++;
        else if (normed[j] === '}') depth--;
        j++;
      }
      rules.push(normed.substring(i, j));
      i = j;
    }
    return rules.sort();
  }

  const baselineRules = extractRuleSet(normalizedBaseline);
  const combinedRules = extractRuleSet(normalizedCombined);

  // Compare sorted rule arrays
  let rulesMatch = baselineRules.length === combinedRules.length;
  if (rulesMatch) {
    for (let i = 0; i < baselineRules.length; i++) {
      if (baselineRules[i] !== combinedRules[i]) {
        rulesMatch = false;
        break;
      }
    }
  }

  assert(
    rulesMatch,
    `CSS rule-set parity: all ${baselineRules.length} rules preserved (order may differ due to dock extraction)`
  );

  if (!rulesMatch) {
    console.log(`    Baseline rules: ${baselineRules.length}, Combined rules: ${combinedRules.length}`);
    // Find differences
    const bSet = new Set(baselineRules);
    const cSet = new Set(combinedRules);
    let onlyBaseline = 0, onlyCombined = 0;
    for (const r of baselineRules) {
      if (!cSet.has(r)) {
        onlyBaseline++;
        if (onlyBaseline <= 3) console.log(`    Only in baseline: ${r.substring(0, 100)}...`);
      }
    }
    for (const r of combinedRules) {
      if (!bSet.has(r)) {
        onlyCombined++;
        if (onlyCombined <= 3) console.log(`    Only in combined: ${r.substring(0, 100)}...`);
      }
    }
  }

  // Also verify normalized lengths match (same content, just reordered)
  assert(
    bLen === cLen,
    `Normalized content lengths match (${bLen} baseline vs ${cLen} combined)`
  );
}

// =============================================================================
// SECTION 5: No Duplicate Rules Across Files
// =============================================================================
console.log('\n=== Section 5: No Duplicate Rules ===');

function extractRuleBlocks(cssText) {
  // Remove comments, extract selector+body pairs
  const clean = cssText.replace(/\/\*[\s\S]*?\*\//g, '');
  const blocks = [];
  // Handle both top-level rules and rules inside @media
  const topLevel = clean.replace(/@media[^{]*\{([\s\S]*?\})\s*\}/g, '');
  const re = /([^{}@]+)\{([^{}]*)\}/g;
  let m;
  while ((m = re.exec(topLevel)) !== null) {
    const sel = m[1].trim();
    const body = m[2].trim();
    if (sel && body && !sel.startsWith('@keyframes')) {
      blocks.push({ selector: sel, body: body });
    }
  }
  return blocks;
}

// Check for exact duplicate selector+body across files
const rulesPerFile = {};
const allRulesSet = new Map(); // key: normalized(selector+body) -> file
let duplicatesFound = 0;

for (const file of CSS_FILES_ORDERED) {
  const fullPath = path.join(CSS_DIR, file);
  if (!fs.existsSync(fullPath)) continue;
  const content = fs.readFileSync(fullPath, 'utf8');
  const rules = extractRuleBlocks(content);
  rulesPerFile[file] = rules.length;

  for (const rule of rules) {
    const key = normalize(rule.selector + '{' + rule.body + '}');
    if (allRulesSet.has(key)) {
      const origFile = allRulesSet.get(key);
      if (origFile !== file) {
        duplicatesFound++;
        if (duplicatesFound <= 5) {
          console.log(`    DUPLICATE: "${rule.selector}" in both ${origFile} and ${file}`);
        }
      }
    } else {
      allRulesSet.set(key, file);
    }
  }
}

assert(duplicatesFound === 0, `No duplicate rules across CSS files (found ${duplicatesFound})`);
if (duplicatesFound > 5) {
  console.log(`    ... and ${duplicatesFound - 5} more duplicates`);
}

// Report rule counts per file
for (const [file, count] of Object.entries(rulesPerFile)) {
  console.log(`    ${file}: ~${count} top-level rule blocks`);
}

// =============================================================================
// SECTION 6: Theme Rules in themes.css
// =============================================================================
console.log('\n=== Section 6: Theme Coverage ===');

const themesPath = path.join(CSS_DIR, 'themes.css');
if (fs.existsSync(themesPath)) {
  const themesCSS = fs.readFileSync(themesPath, 'utf8');

  // Check light theme root variables
  const lightRootMatch = themesCSS.match(/\[data-theme="light"\]\s*\{/);
  assert(!!lightRootMatch, 'themes.css contains [data-theme="light"] root variable block');

  // Check girly theme root variables
  const girlyRootMatch = themesCSS.match(/\[data-theme="girly"\]\s*\{/);
  assert(!!girlyRootMatch, 'themes.css contains [data-theme="girly"] root variable block');

  // Count theme rules
  const lightCount = (themesCSS.match(/\[data-theme="light"\]/g) || []).length;
  const girlyCount = (themesCSS.match(/\[data-theme="girly"\]/g) || []).length;

  // Theme coverage threshold: 75% (not 90%) because scattered theme rules inside
  // @media blocks (e.g., mobile tabbar theme overrides at lines 1659-1663 inside the
  // big @media(max-width:1024px) block) legitimately stay in their parent file
  // (panels.css). Extracting them would break the @media block structure.
  // The contiguous theme blocks (light theme, girly theme, upload modal themes)
  // are all in themes.css. The remaining ~20% are @media-embedded overrides.
  assert(lightCount >= meta.lightThemeRuleCount * 0.75,
    `themes.css has >= 75% of baseline light theme rules (${lightCount} vs baseline ${meta.lightThemeRuleCount})`);
  assert(girlyCount >= meta.girlyThemeRuleCount * 0.75,
    `themes.css has >= 75% of baseline girly theme rules (${girlyCount} vs baseline ${meta.girlyThemeRuleCount})`);

  // Verify NO theme rules in other files (they should ALL be in themes.css)
  let themeLeaks = 0;
  for (const file of CSS_FILES_ORDERED) {
    if (file === 'themes.css') continue;
    const fullPath = path.join(CSS_DIR, file);
    if (!fs.existsSync(fullPath)) continue;
    const content = fs.readFileSync(fullPath, 'utf8');
    const lightLeaks = (content.match(/\[data-theme="light"\]/g) || []).length;
    const girlyLeaks = (content.match(/\[data-theme="girly"\]/g) || []).length;
    if (lightLeaks > 0 || girlyLeaks > 0) {
      themeLeaks += lightLeaks + girlyLeaks;
      warn(`${file} contains ${lightLeaks} light + ${girlyLeaks} girly theme rules (should be in themes.css)`);
    }
  }
  // NOTE: Some theme rules inside @media blocks may legitimately remain in
  // the file that owns the @media block (e.g., mobile tabbar theme overrides
  // inside the max-width:1024px media query in panels.css). This is a WARN,
  // not a FAIL, because the coder may choose either approach. The content
  // parity check in Section 4 is the definitive correctness test.
  if (themeLeaks === 0) {
    assert(true, 'No theme rules leaked into non-themes.css files');
  }
} else {
  assert(false, 'themes.css does not exist');
}

// =============================================================================
// SECTION 7: Dock Rules in dock.css
// =============================================================================
console.log('\n=== Section 7: Dock Rules ===');

const dockPath = path.join(CSS_DIR, 'dock.css');
if (fs.existsSync(dockPath)) {
  const dockCSS = fs.readFileSync(dockPath, 'utf8');

  // Required dock selectors (core dock functionality)
  const requiredDockSelectors = [
    '.app.chat-docked',
    '.dock-btn',
    '.dock-resize',
    '.topnav-dock',
    '.app.chat-docked.dock-expanded',
  ];

  for (const sel of requiredDockSelectors) {
    const found = dockCSS.includes(sel);
    assert(found, `dock.css contains "${sel}" rules`);
  }

  // Check dock mobile override (@media max-width: 1024px)
  const dockMediaMatch = dockCSS.match(/@media\s*\(\s*max-width\s*:\s*1024px\s*\)/);
  assert(!!dockMediaMatch, 'dock.css contains mobile override @media(max-width:1024px)');

  // Verify no dock rules in other files
  let dockLeaks = 0;
  const dockPatterns = ['.dock-btn', '.dock-resize', '.topnav-dock', 'chat-docked'];
  for (const file of CSS_FILES_ORDERED) {
    if (file === 'dock.css') continue;
    // themes.css is allowed to have dock-related theme overrides
    if (file === 'themes.css') continue;
    const fullPath = path.join(CSS_DIR, file);
    if (!fs.existsSync(fullPath)) continue;
    const content = fs.readFileSync(fullPath, 'utf8');
    for (const pat of dockPatterns) {
      if (content.includes(pat)) {
        dockLeaks++;
        warn(`${file} contains dock selector "${pat}" (should be in dock.css or themes.css)`);
      }
    }
  }
  if (dockLeaks === 0) {
    assert(true, 'No dock rules leaked into non-dock/non-themes files');
  }
} else {
  assert(false, 'dock.css does not exist');
}

// =============================================================================
// SECTION 8: base.css Content Validation
// =============================================================================
console.log('\n=== Section 8: base.css Validation ===');

const basePath = path.join(CSS_DIR, 'base.css');
if (fs.existsSync(basePath)) {
  const baseCSS = fs.readFileSync(basePath, 'utf8');

  // Must have :root variables
  assert(baseCSS.includes(':root'), 'base.css contains :root variable declarations');

  // Check all baseline :root vars are present
  let missingVars = 0;
  for (const v of meta.rootVars) {
    if (!baseCSS.includes(v)) {
      missingVars++;
      if (missingVars <= 5) {
        console.log(`    Missing var: ${v}`);
      }
    }
  }
  assert(missingVars === 0, `All ${meta.rootVars.length} :root variables present in base.css (missing: ${missingVars})`);

  // Must have reset rules
  assert(baseCSS.includes('box-sizing'), 'base.css contains box-sizing reset');
  assert(baseCSS.includes('html,body') || baseCSS.includes('html, body'), 'base.css contains html,body rules');

  // Must have .app grid layout
  assert(baseCSS.includes('.app') && baseCSS.includes('grid'), 'base.css contains .app grid layout');

  // Must have scrollbar styling
  assert(baseCSS.includes('::-webkit-scrollbar'), 'base.css contains scrollbar styling');
} else {
  assert(false, 'base.css does not exist');
}

// =============================================================================
// SECTION 9: Media Queries Preserved
// =============================================================================
console.log('\n=== Section 9: Media Queries ===');

// All baseline media queries must appear somewhere in the extracted files
for (const mq of meta.mediaQueries) {
  // Normalize the media query for comparison
  const mqNorm = mq.replace(/\s+/g, '').toLowerCase();
  let found = false;
  for (const file of CSS_FILES_ORDERED) {
    const fullPath = path.join(CSS_DIR, file);
    if (!fs.existsSync(fullPath)) continue;
    const content = fs.readFileSync(fullPath, 'utf8').replace(/\s+/g, '').toLowerCase();
    if (content.includes(mqNorm)) {
      found = true;
      break;
    }
  }
  assert(found, `Media query preserved: ${mq}`);
}

// =============================================================================
// SECTION 10: @keyframes Preserved
// =============================================================================
console.log('\n=== Section 10: @keyframes ===');

for (const kf of meta.keyframes) {
  let found = false;
  for (const file of CSS_FILES_ORDERED) {
    const fullPath = path.join(CSS_DIR, file);
    if (!fs.existsSync(fullPath)) continue;
    const content = fs.readFileSync(fullPath, 'utf8');
    if (content.includes(`@keyframes ${kf}`)) {
      found = true;
      break;
    }
  }
  assert(found, `@keyframes "${kf}" preserved in extracted CSS`);
}

// =============================================================================
// SECTION 11: No Accidental Selector Modifications
// =============================================================================
console.log('\n=== Section 11: Selector Integrity ===');

// Spot-check critical selectors that are known to be fragile
const criticalSelectors = [
  '.app.chat-docked #chatArea',
  '.app.chat-docked.dock-expanded #chatArea',
  '.mobile-tabbar',
  '.sidebar.mobile-open',
  '.sidebar-overlay.visible',
  ':root',
  '[data-theme="light"]',
  '[data-theme="girly"]',
  '.chat-area.hidden',
  '.composer-textarea',
  '.msg-bubble',
  '.f-card',
  '#brain-canvas',
  '.ar-card',
  '.hub-card',
];

for (const sel of criticalSelectors) {
  let found = false;
  for (const file of CSS_FILES_ORDERED) {
    const fullPath = path.join(CSS_DIR, file);
    if (!fs.existsSync(fullPath)) continue;
    const content = fs.readFileSync(fullPath, 'utf8');
    if (content.includes(sel)) {
      found = true;
      break;
    }
  }
  assert(found, `Critical selector preserved: ${sel}`);
}

// =============================================================================
// SECTION 12: File Size Sanity
// =============================================================================
console.log('\n=== Section 12: File Size Sanity ===');

let totalSize = 0;
for (const file of CSS_FILES_ORDERED) {
  const fullPath = path.join(CSS_DIR, file);
  if (!fs.existsSync(fullPath)) continue;
  const size = fs.statSync(fullPath).size;
  totalSize += size;
  assert(size > 100, `${file} is not empty (${size} bytes)`);
  console.log(`    ${file}: ${size} bytes`);
}

// Total should be close to baseline (within 5% tolerance for added comments/whitespace)
const baselineSize = meta.cssLengthBytes;
const tolerance = 0.05;
const lowerBound = baselineSize * (1 - tolerance);
const upperBound = baselineSize * (1 + tolerance);
assert(
  totalSize >= lowerBound && totalSize <= upperBound,
  `Total CSS size (${totalSize}) within 5% of baseline (${baselineSize})`
);

// =============================================================================
// SECTION 13: No CSS Left in HTML Body
// =============================================================================
console.log('\n=== Section 13: No Inline CSS Pollution ===');

// After </head>, there should be no <style> blocks (except possibly script-generated)
const bodyStart = html.indexOf('<body');
if (bodyStart > 0) {
  const bodyHTML = html.substring(bodyStart);
  const bodyStyleMatch = bodyHTML.match(/<style[\s>]/);
  assert(!bodyStyleMatch, 'No <style> blocks in <body>');
}

// =============================================================================
// Summary
// =============================================================================
console.log(`\n${'='.repeat(60)}`);
console.log(`CSS Extraction Verification Results:`);
console.log(`  PASSED:   ${passed}`);
console.log(`  FAILED:   ${failed}`);
console.log(`  WARNINGS: ${warnings}`);
console.log(`${'='.repeat(60)}`);

if (failed > 0) {
  console.log('\nExtraction has issues that must be fixed before merging.');
  process.exit(1);
} else if (warnings > 0) {
  console.log('\nExtraction passed with warnings. Review warnings before merging.');
  process.exit(0);
} else {
  console.log('\nExtraction verified -- full CSS parity confirmed.');
  process.exit(0);
}
