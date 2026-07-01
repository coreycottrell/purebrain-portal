#!/usr/bin/env node
/**
 * CSS Extraction — Pre-Extraction Baseline Capture
 *
 * Run BEFORE extracting CSS from portal-pb-styled.html.
 * Captures the full <style> block content as the canonical baseline.
 *
 * Usage: node tests/test_css_extraction_baseline.js
 * Output: tests/css_baseline.txt (raw CSS), tests/css_baseline_meta.json (stats)
 */

const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const HTML_FILE = path.join(ROOT, 'portal-pb-styled.html');
const BASELINE_FILE = path.join(__dirname, 'css_baseline.txt');
const META_FILE = path.join(__dirname, 'css_baseline_meta.json');

// --- Extract CSS from <style> block ---
const html = fs.readFileSync(HTML_FILE, 'utf8');
const styleMatch = html.match(/<style>([\s\S]*?)<\/style>/);
if (!styleMatch) {
  console.error('FATAL: No <style> block found in portal-pb-styled.html');
  process.exit(1);
}

const css = styleMatch[1];

// --- Parse CSS into rule-level metadata ---
function extractSelectors(cssText) {
  // Remove comments
  const noComments = cssText.replace(/\/\*[\s\S]*?\*\//g, '');
  // Remove @keyframes blocks (they contain { } that confuse selector parsing)
  const noKeyframes = noComments.replace(/@keyframes\s+[\w-]+\s*\{[^}]*(?:\{[^}]*\}[^}]*)*\}/g, '');

  const selectors = [];
  // Match selectors followed by { ... }
  const re = /([^{}]+)\{([^{}]*)\}/g;
  let m;
  while ((m = re.exec(noKeyframes)) !== null) {
    const rawSelector = m[1].trim();
    const body = m[2].trim();
    if (rawSelector && body) {
      // Split comma-separated selectors
      rawSelector.split(',').forEach(s => {
        selectors.push(s.trim());
      });
    }
  }
  return selectors;
}

function extractMediaQueries(cssText) {
  const queries = [];
  const re = /@media\s*\([^)]+\)/g;
  let m;
  while ((m = re.exec(cssText)) !== null) {
    queries.push(m[0]);
  }
  return [...new Set(queries)];
}

function extractKeyframes(cssText) {
  const kf = [];
  const re = /@keyframes\s+([\w-]+)/g;
  let m;
  while ((m = re.exec(cssText)) !== null) {
    kf.push(m[1]);
  }
  return kf;
}

function extractThemeRules(cssText, theme) {
  const re = new RegExp(`\\[data-theme="${theme}"\\][^{]*\\{[^}]*\\}`, 'g');
  const rules = [];
  let m;
  while ((m = re.exec(cssText)) !== null) {
    rules.push(m[0].split('{')[0].trim());
  }
  return rules;
}

function extractDockRules(cssText) {
  const dockPatterns = [
    /\.dock-btn[^{]*/g,
    /\.dock-resize[^{]*/g,
    /\.topnav-dock[^{]*/g,
    /\.app\.chat-docked[^{]*/g,
    /\.app\.chat-docked\.dock-expanded[^{]*/g,
  ];
  const rules = [];
  for (const pat of dockPatterns) {
    let m;
    while ((m = pat.exec(cssText)) !== null) {
      rules.push(m[0].trim());
    }
  }
  return rules;
}

function extractRootVars(cssText) {
  const rootMatch = cssText.match(/:root\s*\{([^}]+)\}/);
  if (!rootMatch) return [];
  return rootMatch[1].match(/--[\w-]+/g) || [];
}

const selectors = extractSelectors(css);
const mediaQueries = extractMediaQueries(css);
const keyframes = extractKeyframes(css);
const lightRules = extractThemeRules(css, 'light');
const girlyRules = extractThemeRules(css, 'girly');
const dockRules = extractDockRules(css);
const rootVars = extractRootVars(css);

const meta = {
  capturedAt: new Date().toISOString(),
  sourceFile: 'portal-pb-styled.html',
  cssLengthBytes: Buffer.byteLength(css, 'utf8'),
  cssLengthChars: css.length,
  cssLineCount: css.split('\n').length,
  totalSelectorsApprox: selectors.length,
  uniqueSelectors: [...new Set(selectors)].length,
  mediaQueries: mediaQueries,
  mediaQueryCount: mediaQueries.length,
  keyframes: keyframes,
  keyframeCount: keyframes.length,
  rootVarCount: rootVars.length,
  rootVars: rootVars,
  lightThemeRuleCount: lightRules.length,
  girlyThemeRuleCount: girlyRules.length,
  dockRuleCount: dockRules.length,
  lightThemeSelectors: lightRules,
  girlyThemeSelectors: girlyRules,
  dockSelectors: dockRules,
};

// --- Write baseline files ---
fs.writeFileSync(BASELINE_FILE, css, 'utf8');
fs.writeFileSync(META_FILE, JSON.stringify(meta, null, 2), 'utf8');

console.log('=== CSS Extraction Baseline Captured ===');
console.log(`  CSS size:        ${meta.cssLengthBytes} bytes / ${meta.cssLineCount} lines`);
console.log(`  Selectors:       ~${meta.totalSelectorsApprox} total, ${meta.uniqueSelectors} unique`);
console.log(`  Media queries:   ${meta.mediaQueryCount}`);
console.log(`  @keyframes:      ${meta.keyframeCount} (${meta.keyframes.join(', ')})`);
console.log(`  :root vars:      ${meta.rootVarCount}`);
console.log(`  Light theme:     ${meta.lightThemeRuleCount} rules`);
console.log(`  Girly theme:     ${meta.girlyThemeRuleCount} rules`);
console.log(`  Dock rules:      ${meta.dockRuleCount} selectors`);
console.log('');
console.log(`  Baseline:  ${BASELINE_FILE}`);
console.log(`  Metadata:  ${META_FILE}`);
console.log('');
console.log('Run test_css_extraction_verify.js AFTER extraction to verify parity.');
