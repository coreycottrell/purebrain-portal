#!/usr/bin/env node
/**
 * Task Creation Modal — Date Picker for "Run Once" tasks
 *
 * BUG (Jared via Aether): the Schedule Task modal only had a TIME input, so a
 * one-time ("Run Once") task could only be scheduled for TODAY at a given time,
 * never a future date.
 *
 * FIX: add a date input (id="sched-date", type="date") shown only when schedule
 * type === "once"; build fire_at from sched-date + sched-time for one-time tasks;
 * validate the date is present and not in the past; prefill it on edit.
 *
 * These tests should FAIL before the fix (RED) and PASS after (GREEN).
 *
 * Usage: node tests/test_task_date_picker.js
 */

const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const HTML_FILE = path.join(ROOT, 'portal-pb-styled.html');
const TASKS_JS = path.join(ROOT, 'static', 'js', 'features', 'tasks.js');

let passed = 0;
let failed = 0;

function assert(condition, msg) {
  if (condition) { passed++; console.log(`  PASS: ${msg}`); }
  else { failed++; console.error(`  FAIL: ${msg}`); }
}

const html = fs.readFileSync(HTML_FILE, 'utf8');
const tasksCode = fs.readFileSync(TASKS_JS, 'utf8');

// =============================================================================
// SECTION 1: HTML — date input exists in the schedule modal
// =============================================================================
console.log('\n=== Section 1: Date input in schedule modal ===');

// Isolate the schedule-modal block so we don't match other modals
const modalStart = html.indexOf('id="schedule-modal"');
const modalEnd = html.indexOf('<!-- Add Rule Modal -->');
const modalBlock = (modalStart !== -1 && modalEnd !== -1)
  ? html.substring(modalStart, modalEnd) : '';

assert(modalBlock.length > 0, 'schedule-modal block found in HTML');
assert(/id=["']sched-date["']/.test(modalBlock),
  'schedule-modal contains a date input with id="sched-date"');
assert(/<input[^>]*type=["']date["'][^>]*id=["']sched-date["']|<input[^>]*id=["']sched-date["'][^>]*type=["']date["']/.test(modalBlock),
  'sched-date input is type="date"');
// The existing time input must remain
assert(/id=["']sched-time["']/.test(modalBlock),
  'schedule-modal still contains the time input (sched-time)');

// =============================================================================
// SECTION 2: toggleSchedCustom shows/hides the date row by type
// =============================================================================
console.log('\n=== Section 2: toggle wiring for date row ===');

// toggleSchedCustom lives inline in the HTML. It must reference sched-date so the
// date field shows only for "once".
const toggleMatch = html.match(/function toggleSchedCustom\([^)]*\)\{[^}]*\}/);
assert(toggleMatch !== null, 'toggleSchedCustom function present in HTML');
assert(toggleMatch && /sched-date/.test(toggleMatch[0]),
  'toggleSchedCustom references sched-date (shows/hides date field by type)');

// =============================================================================
// SECTION 3: tasks.js — _scheduleTask uses sched-date for one-time tasks
// =============================================================================
console.log('\n=== Section 3: _scheduleTask uses the date input ===');

assert(tasksCode.includes("getElementById('sched-date')") ||
       tasksCode.includes('getElementById("sched-date")'),
  '_scheduleTask / tasks.js reads sched-date input');

// =============================================================================
// SECTION 4: Functional — fire_at build logic
// =============================================================================
console.log('\n=== Section 4: Functional fire_at logic ===');

// Extract the buildFireAt helper from tasks.js and execute it in isolation.
// The fix must expose a pure helper so logic is testable without a DOM.
// Helper contract: _buildOnceFireAt(dateStr, timeStr, now) ->
//    { ok:true, iso } on success, or { ok:false, error } on invalid input.
const helperRe = /function _buildOnceFireAt[\s\S]*?\n  \}/;
const helperMatch = tasksCode.match(helperRe);
assert(helperMatch !== null,
  'tasks.js defines a pure helper _buildOnceFireAt(dateStr, timeStr, now)');

if (helperMatch) {
  // eslint-disable-next-line no-eval
  let _buildOnceFireAt;
  eval(helperMatch[0].replace(/^function /, '_buildOnceFireAt = function '));

  const now = new Date('2026-06-23T12:00:00');

  // Future date -> uses the SELECTED date (not today)
  const r1 = _buildOnceFireAt('2026-08-15', '14:30', now);
  assert(r1 && r1.ok === true, 'future date accepted');
  if (r1 && r1.ok) {
    const d = new Date(r1.iso);
    assert(d.getFullYear() === 2026 && d.getMonth() === 7 && d.getDate() === 15,
      'fire_at uses the SELECTED date (Aug 15, 2026), not today');
    assert(d.getHours() === 14 && d.getMinutes() === 30,
      'fire_at uses the selected time (14:30)');
  }

  // Empty date -> rejected
  const r2 = _buildOnceFireAt('', '09:00', now);
  assert(r2 && r2.ok === false, 'empty date is rejected');

  // Past date -> rejected
  const r3 = _buildOnceFireAt('2020-01-01', '09:00', now);
  assert(r3 && r3.ok === false, 'past date is rejected');

  // Today + future time -> accepted
  const r4 = _buildOnceFireAt('2026-06-23', '23:00', now);
  assert(r4 && r4.ok === true, 'today with a later time is accepted');
}

// =============================================================================
// SECTION 5: Edit prefill references sched-date
// =============================================================================
console.log('\n=== Section 5: Edit prefill ===');

// The edit path (_editTask) must prefill sched-date from the existing fire_at.
const editIdx = tasksCode.indexOf('function _editTask');
const updateIdx = tasksCode.indexOf('function _updateEditedTask');
const editBlock = (editIdx !== -1 && updateIdx !== -1)
  ? tasksCode.substring(editIdx, updateIdx) : '';
assert(/sched-date/.test(editBlock),
  '_editTask references sched-date for prefill');

// The update path must also use sched-date for one-time tasks.
const updateBlock = updateIdx !== -1
  ? tasksCode.substring(updateIdx, updateIdx + 1500) : '';
assert(/sched-date|_buildOnceFireAt/.test(updateBlock),
  '_updateEditedTask uses sched-date / _buildOnceFireAt for one-time tasks');

// =============================================================================
// SECTION 6: Regression — daily/weekly behavior unchanged
// =============================================================================
console.log('\n=== Section 6: Regression (recurring unchanged) ===');

// Recurring tasks must still set recur_type for daily/weekly. The implementation
// branches on type==='daily'||'weekly' and sets body.recur_type = type, so verify
// both the branch and the assignment exist.
assert(/type === 'daily' \|\| type === 'weekly'/.test(tasksCode),
  'recurring branch handles daily and weekly');
assert(/body\.recur_type = type/.test(tasksCode),
  'recurring tasks set recur_type from the selected type (daily/weekly)');
assert(tasksCode.includes('body.recur_time = time'),
  'recurring tasks still send recur_time from the time input');

// --- Summary ---
console.log(`\n${'='.repeat(50)}`);
console.log(`Results: ${passed} passed, ${failed} failed out of ${passed + failed} assertions`);
if (failed > 0) { process.exit(1); }
else { console.log('All tests passed!'); }
