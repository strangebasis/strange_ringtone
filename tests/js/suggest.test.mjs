// The initial selection, docs/PLAN.md 7.2 rule 3 (core/static/audio/suggest.js).
import { test } from "node:test";
import assert from "node:assert/strict";

import { bestWindow, snapToQuiet, suggestSelection } from "../../core/static/audio/suggest.js";

const RATE = 44100;

// Builds a mono signal from [seconds, amplitude] sections of a 440 Hz sine;
// amplitude 0 is digital silence.
function sections(...parts) {
  const total = parts.reduce((sum, [seconds]) => sum + Math.round(seconds * RATE), 0);
  const out = new Float32Array(total);
  let at = 0;
  for (const [seconds, amplitude] of parts) {
    const n = Math.round(seconds * RATE);
    for (let i = 0; i < n; i += 1) {
      out[at + i] = amplitude * Math.sin((2 * Math.PI * 440 * (at + i)) / RATE);
    }
    at += n;
  }
  return out;
}

function near(actual, expected, tolerance, label) {
  assert.ok(Math.abs(actual - expected) <= tolerance, `${label}: ${actual} is not within ${tolerance} of ${expected}`);
}

test("the loudest window wins: quiet, loud, quiet", () => {
  const signal = sections([20, 0.01], [15, 0.5], [25, 0.01]);
  const { start, end } = suggestSelection([signal], RATE, 15);
  near(end - start, 15, 1e-9, "length");
  // The loud part is exactly one window long, so the window starts at 20 s;
  // the snap may then move it at most 1 s, into the quiet lead-in.
  assert.ok(start >= 19 && start <= 21, `start ${start}`);
});

test("the start snaps to the quietest 50 ms within 1 s", () => {
  // 100 ms of digital silence at 19.4 s, inside the quiet lead-in: quieter
  // than anything else within a second of the 20 s start.
  const signal = sections([19.4, 0.01], [0.1, 0], [0.5, 0.01], [15, 0.5], [25, 0.01]);
  const { start, end } = suggestSelection([signal], RATE, 15);
  assert.ok(start >= 19.4 && start <= 19.5, `start ${start}`);
  near(end - start, 15, 1e-9, "length");
});

test("a trough more than 1 s away is out of reach", () => {
  const signal = sections([18.5, 0.01], [0.1, 0], [1.4, 0.01], [15, 0.5], [25, 0.01]);
  const { start } = suggestSelection([signal], RATE, 15);
  assert.ok(start >= 19 && start <= 21, `start ${start}`);
});

test("stereo input works and both channels count", () => {
  const loud = sections([20, 0.01], [15, 0.5], [25, 0.01]);
  const flat = sections([60, 0.01]);
  const { start } = suggestSelection([flat, loud], RATE, 15);
  assert.ok(start >= 19 && start <= 21, `start ${start}`);
});

test("a snap that would run past the end slides the window back", () => {
  // The loudest window is the last one (1 s to 31 s); the trough at 1.5 s
  // pulls the start later, and the window then ends at the clip's end with
  // its full length.
  const signal = sections([1, 0.01], [0.5, 0.5], [0.1, 0], [29.4, 0.5]);
  const { start, end } = suggestSelection([signal], RATE, 30);
  near(end, 31, 1e-9, "end");
  near(start, 1, 1e-9, "start");
});

// Digital silence around the loud part in the next two tests: the K-weighting
// filter is causal, so the silence before it measures exactly zero, and the
// snap only moves the start when the chosen one is not already silent.

test("the last candidate window, ending exactly at the clip's end, is scored", () => {
  // Loud from 10.3 s to the end of a 40 s clip: the best 30 s window is the
  // very last one, 10 s to 40 s, and its start is already silent.
  const signal = sections([10.3, 0], [29.7, 0.5]);
  const { start, end } = suggestSelection([signal], RATE, 30);
  near(start, 10, 1e-9, "start");
  near(end, 40, 1e-9, "end");
});

test("candidate windows start every 100 ms", () => {
  // A 15 s loud part from 20.28 s: of the windows 100 ms apart, the one at
  // 20.3 s holds the most of it (2.98 s in its last short-term block, against
  // 2.92 s for 20.2 s and 2.88 s for 20.4 s). Its start is 20 ms into the loud
  // part, so the snap moves it back to the nearest silent 50 ms, 20.25 s.
  // Candidates 200 ms apart would pick 20.2 s, already silent, and stay there.
  const signal = sections([20.28, 0], [15, 0.5], [24.72, 0]);
  const { start, end } = suggestSelection([signal], RATE, 15);
  near(start, 20.25, 1e-9, "start");
  near(end, 35.25, 1e-9, "end");
});

test("a clip no longer than the cap is selected whole", () => {
  const signal = sections([10, 0.5]);
  assert.deepEqual(suggestSelection([signal], RATE, 30), { start: 0, end: 10 });
  const exact = sections([30, 0.5]);
  assert.deepEqual(suggestSelection([exact], RATE, 30), { start: 0, end: 30 });
});

test("a 0.6 s clip still gets a non-empty selection", () => {
  const { start, end } = suggestSelection([sections([0.6, 0.5])], RATE, 30);
  assert.equal(start, 0);
  near(end, 0.6, 1e-9, "end");
});

test("all silence starts at 0 with the cap's length", () => {
  const silent = new Float32Array(RATE * 60);
  assert.deepEqual(suggestSelection([silent], RATE, 30), { start: 0, end: 30 });
  assert.deepEqual(suggestSelection([silent, silent], RATE, 30), { start: 0, end: 30 });
});

test("a cap shorter than the 3 s short-term window still works", () => {
  const signal = sections([5, 0.01], [2, 0.5], [5, 0.01]);
  const { start, end } = suggestSelection([signal], RATE, 2);
  near(end - start, 2, 1e-9, "length");
  assert.ok(start >= 4 && start <= 6, `start ${start}`);
});

test("heatmap and timestamp options are accepted and ignored until milestone 5", () => {
  const signal = sections([20, 0.01], [15, 0.5], [25, 0.01]);
  const plain = suggestSelection([signal], RATE, 15);
  assert.deepEqual(suggestSelection([signal], RATE, 15, { heatmap: [[0, 1, 1]], timestamp: 3 }), plain);
  assert.deepEqual(suggestSelection([signal], RATE, 15, { heatmap: null, timestamp: null }), plain);
});

test("bad arguments are refused", () => {
  const signal = sections([5, 0.5]);
  assert.throws(() => suggestSelection([signal], RATE, 0), RangeError);
  assert.throws(() => suggestSelection([signal], RATE, NaN), RangeError);
  assert.throws(() => suggestSelection([], RATE, 30), RangeError);
});

test("a file quieter than the -70 LUFS gate everywhere is treated as silence", () => {
  // About -83 LUFS throughout, with a 0.3 s digital gap near the start that
  // the snap would otherwise jump to.
  const faint = sections([0.3, 0.0001], [0.3, 0], [59.4, 0.0001]);
  assert.deepEqual(suggestSelection([faint], RATE, 30), { start: 0, end: 30 });
});

test("input just over the cap keeps the cap's length inside the input", () => {
  const signal = sections([30.05, 0.5]);
  const { start, end } = suggestSelection([signal], RATE, 30);
  near(end - start, 30, 1e-9, "length");
  assert.ok(start >= 0 && end <= 30.05 + 1e-9, `${start} to ${end}`);
});

// Hand-built inputs for the two steps, because equal scores and equal quiet
// spots need exact ties, which the K-weighting filter's ringing never leaves
// in real audio.

test("of equally loud windows the earliest wins", () => {
  assert.equal(bestWindow([-20, -20, -20, -20, -20], 2, 4), 0);
  assert.equal(bestWindow([-30, -20, -20, -20, -30], 2, 4), 1);
  assert.equal(bestWindow([-30, -20, -30, -20, -20], 2, 4), 3);
});

test("silence and anything below -70 count as -70 in a window's score", () => {
  // Without the floor every window after a -Infinity would score NaN or
  // -Infinity, and the loud stretch at 2 could never win.
  assert.equal(bestWindow([-Infinity, -Infinity, -10, -10, -80], 2, 4), 2);
  // Floored, windows 0 and 1 tie at -80 and the earliest wins; counted as
  // they are, window 3 (-85) would beat both.
  assert.equal(bestWindow([-200, -10, -100, -35, -50], 2, 4), 0);
});

test("only the given candidates are scored", () => {
  assert.equal(bestWindow([-30, -30, -30, -10], 1, 3), 0);
  assert.equal(bestWindow([-30, -30, -30, -10], 1, 4), 3);
});

// Granule energies -> the prefix sum snapToQuiet reads.
function prefixOf(energies) {
  const prefix = new Float64Array(energies.length + 1);
  energies.forEach((energy, i) => { prefix[i + 1] = prefix[i] + energy; });
  return prefix;
}

// count granules of energy 1 with digital silence at the given 5-granule spans.
function energies(count, ...silentFrom) {
  const out = new Array(count).fill(1);
  for (const from of silentFrom) out.fill(0, from, from + 5);
  return out;
}

test("the snap takes the nearest of equally quiet spots, the earlier side first", () => {
  // Quiet 50 ms centred 8 granules either side of 200, and another farther out.
  const e = energies(400, 190, 206, 150);
  assert.equal(snapToQuiet(prefixOf(e), e.length, 200), 192);
  // Only the later one: it is taken.
  const later = energies(400, 206);
  assert.equal(snapToQuiet(prefixOf(later), later.length, 200), 208);
});

test("the snap reaches 1 s and no further, and needs something strictly quieter", () => {
  const near1s = energies(400, 98);
  assert.equal(snapToQuiet(prefixOf(near1s), near1s.length, 200), 100);
  // Silence at granules 92 to 96 touches no 50 ms around a point within reach
  // (the nearest, 100, spans 98 to 102).
  const beyond = energies(400, 92);
  assert.equal(snapToQuiet(prefixOf(beyond), beyond.length, 200), 200);
  const flat = energies(400);
  assert.equal(snapToQuiet(prefixOf(flat), flat.length, 200), 200);
});

test("the snap measures 50 ms inside the input at either end", () => {
  // A start at the very beginning still compares its own 50 ms (the first
  // five granules) with the quiet spot 20 granules on.
  const start = energies(400, 20);
  start.fill(10, 0, 5);
  assert.equal(snapToQuiet(prefixOf(start), start.length, 0), 22);
  // Quiet at the very end: the nearest point whose 50 ms is all quiet.
  const end = energies(400, 395);
  assert.equal(snapToQuiet(prefixOf(end), end.length, 390), 397);
  // Shorter than 50 ms: nothing to compare, the start stays.
  assert.equal(snapToQuiet(prefixOf([1, 1, 0]), 3, 1), 1);
});
