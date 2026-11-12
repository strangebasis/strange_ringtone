// The initial selection, docs/PLAN.md 7.2 rules 1 to 3 (core/static/audio/suggest.js).
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  bestWindow, scoreHeatmap, snapToQuiet, suggestionRule, suggestSelection, suggestWindowStart, windowHeatmap,
  windowStartCandidates,
} from "../../core/static/audio/suggest.js";

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

// --- rules 1 (t=) and 2 (the heatmap) ---------------------------------------------

// 30 s of digital silence, 15 s loud, 15 s of silence: rule 3 picks 30 s, and
// a start anywhere in the leading silence stays exactly where it is put,
// because the snap moves only to somewhere strictly quieter than silence.
const LOUD_AT_30 = sections([30, 0], [15, 0.5], [15, 0]);

// 1 s heatmap points over `seconds`, value 1 from `from` for `length` seconds
// and `rest` everywhere else.
function heatmap(seconds, from, length, rest = 0) {
  return Array.from({ length: seconds }, (_, i) => ({
    start: i, end: i + 1, value: i >= from && i < from + length ? 1 : rest,
  }));
}

test("rule 1: the t= timestamp places the start, over the loudest stretch", () => {
  assert.deepEqual(suggestSelection([LOUD_AT_30], RATE, 15, { timestamp: 10, heatmap: null }), { start: 10, end: 25 });
  // Without it, rule 3 goes to the loud part.
  const plain = suggestSelection([LOUD_AT_30], RATE, 15);
  assert.ok(plain.start >= 29 && plain.start <= 31, `rule 3 start ${plain.start}`);
  // 0 is a timestamp too (t=0 means the very start).
  assert.deepEqual(suggestSelection([LOUD_AT_30], RATE, 15, { timestamp: 0 }), { start: 0, end: 15 });
});

test("rule 1 wins over the heatmap", () => {
  const options = { timestamp: 10, heatmap: heatmap(60, 40, 15, 0.1) };
  assert.deepEqual(suggestSelection([LOUD_AT_30], RATE, 15, options), { start: 10, end: 25 });
  assert.equal(suggestionRule(60, options), "timestamp");
});

test("a t= in the last half second, past the end or not a number falls through", () => {
  const plain = suggestSelection([LOUD_AT_30], RATE, 15);
  for (const timestamp of [59.5, 59.9, 60, 61, 1e9, -1, -0.001, NaN, Infinity, -Infinity, "10", null, undefined,
    [10], { valueOf: () => 10 }]) {
    assert.deepEqual(suggestSelection([LOUD_AT_30], RATE, 15, { timestamp, heatmap: null }), plain, String(timestamp));
  }
  // Falling through reaches rule 2 when there is a heatmap.
  const options = { timestamp: 59.5, heatmap: heatmap(60, 5, 15) };
  assert.deepEqual(suggestSelection([LOUD_AT_30], RATE, 15, options), { start: 5, end: 20 });
  assert.equal(suggestionRule(60, options), "heatmap");
});

test("rule 1: the end slides back at the file's end, keeping the full length", () => {
  // Just inside the margin: 59.4 + 0.5 < 60.
  assert.deepEqual(suggestSelection([LOUD_AT_30], RATE, 15, { timestamp: 59.4 }), { start: 45, end: 60 });
  assert.deepEqual(suggestSelection([LOUD_AT_30], RATE, 15, { timestamp: 50 }), { start: 45, end: 60 });
});

test("rule 2: the heatmap window with the highest sum wins over a louder stretch", () => {
  const options = { timestamp: null, heatmap: heatmap(60, 5, 15) };
  assert.deepEqual(suggestSelection([LOUD_AT_30], RATE, 15, options), { start: 5, end: 20 });
  assert.equal(suggestionRule(60, options), "heatmap");
  // A heatmap that peaks at the end slides back like any other window.
  assert.deepEqual(suggestSelection([LOUD_AT_30], RATE, 15, { heatmap: heatmap(60, 50, 10) }), { start: 45, end: 60 });
});

test("rule 2: candidate windows start every 100 ms", () => {
  // The best window starts at 1.3 s, which a coarser step would miss: with
  // 0.5 s it would be 1.5 s. The start is in silence, so the snap keeps it.
  const points = [{ start: 0, end: 1.3, value: 0 }, { start: 1.3, end: 16.3, value: 1 }, { start: 16.3, end: 60, value: 0 }];
  const { start, end } = suggestSelection([LOUD_AT_30], RATE, 15, { heatmap: points });
  near(start, 1.3, 1e-9, "start");
  near(end, 16.3, 1e-9, "end");
});

test("rule 2: a point may end up to 1 s after the audio does", () => {
  const points = heatmap(60, 5, 15);
  points[59] = { start: 59, end: 61, value: 0 };
  assert.deepEqual(suggestSelection([LOUD_AT_30], RATE, 15, { heatmap: points }), { start: 5, end: 20 });
  points[59] = { start: 59, end: 61.01, value: 0 };
  assert.equal(suggestionRule(60, { heatmap: points }), "loudness");
});

test("rule 2: the fetcher's list for audio longer than its whole-second duration is used", () => {
  // What strange_ringtone_fetch.metadata.clean_heatmap sends for 100 segments
  // of a 45.9 s video whose yt-dlp duration is 45: the segments that start
  // past 45 s are left out and the last kept one ends at 45 s, so no point is
  // zero-length (tests/test_fetcher_ytdlp.py pins that side).
  const length = 45.9;
  const points = [];
  for (let i = 0; i < 100; i += 1) {
    const start = (i * length) / 100;
    const end = Math.min(((i + 1) * length) / 100, 45);
    if (end > start) points.push({ start, end, value: i >= 60 && i < 75 ? 1 : 0.1 });
  }
  assert.equal(points.length, 99);
  assert.equal(suggestionRule(length, { heatmap: points }), "heatmap");
  // Kept zero-length, the last point would have turned rule 2 off.
  assert.equal(suggestionRule(length, { heatmap: [...points, { start: 45, end: 45, value: 0.1 }] }), "loudness");
});

test("an invalid heatmap falls through to rule 3", () => {
  const plain = suggestSelection([LOUD_AT_30], RATE, 15);
  const good = () => heatmap(60, 5, 15);
  const withPoint = (point) => {
    const points = good();
    points[3] = point;
    return points;
  };
  const cases = {
    "not an array": { 0: { start: 0, end: 1, value: 1 } },
    "empty": [],
    "201 points": Array.from({ length: 201 }, (_, i) => ({ start: i * 0.25, end: (i + 1) * 0.25, value: 1 })),
    "null point": withPoint(null),
    "a number": withPoint(1),
    "value above 1": withPoint({ start: 3, end: 4, value: 1.5 }),
    "negative value": withPoint({ start: 3, end: 4, value: -0.1 }),
    "NaN value": withPoint({ start: 3, end: 4, value: NaN }),
    "string value": withPoint({ start: 3, end: 4, value: "1" }),
    "missing value": withPoint({ start: 3, end: 4 }),
    "negative start": withPoint({ start: -1, end: 4, value: 1 }),
    "start after end": withPoint({ start: 4, end: 3, value: 1 }),
    "empty span": withPoint({ start: 3, end: 3, value: 1 }),
    "end past the audio": withPoint({ start: 3, end: 62, value: 1 }),
    "infinite end": withPoint({ start: 3, end: Infinity, value: 1 }),
    "the server's names": withPoint({ start_time: 3, end_time: 4, value: 1 }),
  };
  for (const [label, points] of Object.entries(cases)) {
    assert.deepEqual(suggestSelection([LOUD_AT_30], RATE, 15, { heatmap: points }), plain, label);
    assert.equal(suggestionRule(60, { heatmap: points }), "loudness", label);
  }
  // The same list without the bad point is used.
  assert.deepEqual(suggestSelection([LOUD_AT_30], RATE, 15, { heatmap: good() }), { start: 5, end: 20 });
  // 200 points are still fine.
  const many = Array.from({ length: 200 }, (_, i) => ({ start: i * 0.3, end: (i + 1) * 0.3, value: i < 50 ? 1 : 0 }));
  assert.equal(suggestionRule(60, { heatmap: many }), "heatmap");
});

test("the snap applies to rules 1 and 2", () => {
  // Quiet tone throughout, 100 ms of digital silence at 19.4 s, and a much
  // louder stretch at 40 s that rule 3 would choose.
  const signal = sections([19.4, 0.01], [0.1, 0], [20.5, 0.01], [15, 0.5], [5, 0.01]);
  const byTimestamp = suggestSelection([signal], RATE, 15, { timestamp: 20 });
  assert.ok(byTimestamp.start >= 19.4 && byTimestamp.start <= 19.5, `t= start ${byTimestamp.start}`);
  near(byTimestamp.end - byTimestamp.start, 15, 1e-9, "t= length");
  const byHeatmap = suggestSelection([signal], RATE, 15, { heatmap: heatmap(60, 20, 15) });
  assert.ok(byHeatmap.start >= 19.4 && byHeatmap.start <= 19.5, `heatmap start ${byHeatmap.start}`);
  near(byHeatmap.end - byHeatmap.start, 15, 1e-9, "heatmap length");
  // Out of reach: a timestamp 2 s after the gap stays within 1 s of itself.
  const far = suggestSelection([signal], RATE, 15, { timestamp: 21.5 });
  assert.ok(far.start >= 20.5 && far.start <= 22.5, `far start ${far.start}`);
});

test("the options are ignored when the whole input fits", () => {
  const short = sections([10, 0.5]);
  assert.deepEqual(suggestSelection([short], RATE, 15, { timestamp: 5, heatmap: heatmap(10, 2, 3) }), { start: 0, end: 10 });
  // And a missing or null options object is rule 3.
  assert.deepEqual(suggestSelection([LOUD_AT_30], RATE, 15, null), suggestSelection([LOUD_AT_30], RATE, 15));
  assert.deepEqual(suggestSelection([LOUD_AT_30], RATE, 15, {}), suggestSelection([LOUD_AT_30], RATE, 15));
});

test("scoreHeatmap weights each point by the seconds the window covers", () => {
  // One strong point of 2 s against a weak one of 10 s: 0.9 x 2 = 1.8 beats
  // 0.15 x 10 = 1.5 for a 10 s window that can hold only one of them.
  const points = [{ start: 2, end: 4, value: 0.9 }, { start: 20, end: 30, value: 0.15 }];
  assert.equal(scoreHeatmap(points, 10, 30, 0.1), 0);
  // At 0.7 (1.4) the long one wins, in the one window that covers it whole.
  points[0].value = 0.7;
  near(scoreHeatmap(points, 10, 30, 0.1), 20, 1e-9, "the long point");
  // A window shorter than a point scores the seconds it covers: 1 s windows
  // from 10 to 11 all score 1, the one at 9.5 only 0.5, and the earliest wins.
  assert.equal(scoreHeatmap([{ start: 10, end: 12, value: 1 }], 1, 20, 0.5), 10);
});

test("scoreHeatmap: of equal windows the earliest wins, and candidates step by the hop", () => {
  // Every window covering [5, 6] scores 1: starts 0 to 5 are equal.
  assert.equal(scoreHeatmap([{ start: 5, end: 6, value: 1 }], 10, 30, 0.1), 0);
  // Equal sums from many small products still tie.
  const flat = Array.from({ length: 100 }, (_, i) => ({ start: i * 0.3, end: (i + 1) * 0.3, value: 0.1 }));
  assert.equal(scoreHeatmap(flat, 10, 30, 0.1), 0);
  // A 1 s point at 7.4 s and 1 s windows: with a 0.1 s hop the window at 7.4
  // covers it whole (7.3 and 7.5 cover 0.9); with a 0.5 s hop only 7.0 (0.6)
  // and 7.5 (0.9) exist.
  const peak = [{ start: 7.4, end: 8.4, value: 1 }];
  near(scoreHeatmap(peak, 1, 30, 0.1), 7.4, 1e-9, "0.1 s hop");
  near(scoreHeatmap(peak, 1, 30, 0.5), 7.5, 1e-9, "0.5 s hop");
  // The last candidate ends exactly at the input's end, although 20 / 0.1
  // falls just short of 200 in floating point.
  near(scoreHeatmap([{ start: 29, end: 30, value: 1 }], 10, 30, 0.1), 20, 1e-9, "last window");
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

// --- a window of a longer video (docs/PLAN.md 6.2) -----------------------------------------

// An hour in 200 points of 18 s, hot from 1206 s to 1800 s.
const HOUR = 3600;
const HOUR_MAP = Array.from({ length: 200 }, (_, i) => ({
  start: i * 18, end: (i + 1) * 18, value: i >= 67 && i < 100 ? 1 : 0.1,
}));

// The latest second a window may start at: 99:59:59, the fetcher's own limit.
const LATEST = 359999;

test("the default window is the ten minutes with the most replays, from a whole second", () => {
  // The hot stretch is 594 s long: every window from 1200 to 1206 holds it
  // whole and six more seconds of the same value, so the earliest wins.
  assert.equal(suggestWindowStart(HOUR_MAP, HOUR, 600, LATEST), 1200);
  const start = suggestWindowStart(HOUR_MAP, HOUR, 300, LATEST);
  assert.ok(Number.isInteger(start) && start >= 1206 && start <= 1500, String(start));
});

test("a hot stretch at the very end gives the last window", () => {
  const map = HOUR_MAP.map((point, i) => ({ ...point, value: i >= 190 ? 1 : 0.1 }));
  assert.equal(suggestWindowStart(map, HOUR, 600, LATEST), 3000);
  // And one at the beginning gives the first.
  const early = HOUR_MAP.map((point, i) => ({ ...point, value: i < 10 ? 1 : 0.1 }));
  assert.equal(suggestWindowStart(early, HOUR, 600, LATEST), 0);
});

test("without a usable heatmap there is no default window", () => {
  for (const heatmap of [null, undefined, [], "x", {}, [{ start: 0, end: 5 }], [{ start: 5, end: 5, value: 1 }],
    [{ start: 0, end: 5, value: 2 }], [...HOUR_MAP, { start: 0, end: 1, value: 0.5 }],
    [{ start: 0, end: HOUR + 5, value: 1 }]]) {
    assert.equal(suggestWindowStart(heatmap, HOUR, 600, LATEST), null, JSON.stringify(heatmap)?.slice(0, 60));
  }
  for (const [duration, windowSeconds] of [[0, 600], [-1, 600], [Number.NaN, 600], [HOUR, 0], [HOUR, Number.NaN],
    ["3600", 600]]) {
    assert.equal(suggestWindowStart(HOUR_MAP, duration, windowSeconds, LATEST), null);
  }
  // Nor without the latest start a window may have, as a whole number of seconds.
  for (const latest of [undefined, null, Number.NaN, -1, 1200.5, "359999", Number.POSITIVE_INFINITY]) {
    assert.equal(suggestWindowStart(HOUR_MAP, HOUR, 600, latest), null, String(latest));
  }
});

test("a video no longer than the window starts at zero", () => {
  const map = [{ start: 0, end: 300, value: 0.2 }, { start: 300, end: 600, value: 1 }];
  assert.equal(suggestWindowStart(map, 600, 600, LATEST), 0);
  assert.equal(suggestWindowStart(map, 600, 900, LATEST), 0);
});

test("a ten-hour video's default is found quickly", () => {
  const points = Array.from({ length: 100 }, (_, i) => ({ start: i * 360, end: (i + 1) * 360, value: i === 50 ? 1 : 0.2 }));
  const started = Date.now();
  const start = suggestWindowStart(points, 36000, 600, LATEST);
  assert.ok(Date.now() - started < 2000, "too slow for a phone");
  // The hot six minutes run from 18000 s to 18360 s: the earliest window that
  // holds all of them ends where they end.
  assert.equal(start, 17760);
});

test("the default window never starts later than a window may", () => {
  // Hotter and hotter towards the end: the later the window, the better.
  const rising = HOUR_MAP.map((point, i) => ({ ...point, value: (i + 1) / 200 }));
  assert.equal(suggestWindowStart(rising, HOUR, 600, LATEST), 3000);
  // 2000 s lies beside no point's edge (they are 18 s apart): it is the
  // limit itself that is taken, and nothing after it.
  assert.equal(suggestWindowStart(rising, HOUR, 600, 2000), 2000);
  assert.equal(suggestWindowStart(rising, HOUR, 600, 0), 0);
});

test("the search does not grow with the length the video is said to have", () => {
  // What the fetcher's own checks let through: any length, and a point as long as it.
  const endless = [{ start: 0, end: 1e12, value: 1 }];
  assert.equal(suggestWindowStart(endless, 1e12, 600, LATEST), 0);
  // A hot minute 83 hours into such a video is found without walking there:
  // the earliest window that holds all of it ends where it ends.
  const far = [{ start: 0, end: 300000, value: 0.1 }, { start: 300000, end: 300060, value: 1 },
    { start: 300060, end: 1e12, value: 0.1 }];
  assert.equal(suggestWindowStart(far, 1e12, 600, LATEST), 299460);
  // Four starts a point at most, and the first and the last there is.
  assert.deepEqual(windowStartCandidates(far, 600, LATEST), [0, 299400, 299460, 300000, 300060, LATEST]);
  // The most there can be: 200 points whose edges all lie between whole
  // seconds. Second zero is both the first start and the second before the
  // first point's start.
  const uneven = Array.from({ length: 200 }, (_, i) => ({ start: i * 5e9 + 0.5, end: (i + 1) * 5e9 + 0.25, value: 0.5 }));
  const starts = windowStartCandidates(uneven, 600, 1e12);
  assert.equal(starts.length, 4 * 200 + 1);
  // In order as numbers, not as text.
  assert.deepEqual(starts, [...starts].sort((a, b) => a - b));
});

test("the best start is the whole second just before or just after a point's edge", () => {
  // A long point from 7.5 s: every window from 8 s on lies inside it, and the one from 7 s misses half a second.
  assert.equal(suggestWindowStart([{ start: 7.5, end: 100, value: 1 }], 100, 10, LATEST), 8);
  // A short one that ends at 27.5 s: the window from 18 s is the first to hold it whole.
  assert.equal(suggestWindowStart([{ start: 22.5, end: 27.5, value: 1 }], 100, 10, LATEST), 18);
  // The second before the edge wins where the window loses more by moving on
  // than it still gains: here it is leaving a point of 0.75 while it moves
  // into one of 1 from 15.5 s, and from 15 s it holds 13.25 against 13 from 16 s.
  assert.equal(suggestWindowStart([{ start: 0, end: 20, value: 0.75 }, { start: 15.5, end: 40, value: 1 }], 100, 10, LATEST), 15);
  // The same at a point's end: the window holds the short point whole from
  // 17.5 s, and 10.5 from 17 s against 10.25 from 18 s.
  assert.equal(suggestWindowStart([{ start: 0, end: 25, value: 0.75 }, { start: 22.5, end: 27.5, value: 1 }], 100, 10, LATEST), 17);
});

test("the last window looked at ends within the video", () => {
  // A video of 100.5 s whose last half second is the hottest: the window
  // from 91 s would hold it and run past the end. Of the windows that end
  // within the video, those from 81 s to 90 s hold the warm second at 90 s.
  const points = [{ start: 90, end: 91, value: 0.25 }, { start: 100, end: 100.5, value: 1 }];
  assert.equal(suggestWindowStart(points, 100.5, 10, LATEST), 81);
});

test("a point at the very beginning gives second zero, never a start before the video", () => {
  // Every window up to 2.5 s holds the point whole; the first of them would begin 3.5 s before the video.
  assert.equal(suggestWindowStart([{ start: 2.5, end: 6.5, value: 1 }], 100, 10, LATEST), 0);
});

// The same numbers every run: a small generator with a fixed seed.
function seeded(seed) {
  let state = seed;
  return () => {
    state = (state + 0x6D2B79F5) | 0;
    let mixed = Math.imul(state ^ (state >>> 15), 1 | state);
    mixed = (mixed + Math.imul(mixed ^ (mixed >>> 7), 61 | mixed)) ^ mixed;
    return ((mixed ^ (mixed >>> 14)) >>> 0) / 4294967296;
  };
}

test("the search finds what a walk over every whole second finds", () => {
  const random = seeded(20261112);
  const pick = (list) => list[Math.floor(random() * list.length)];
  let compared = 0;
  for (let round = 0; round < 400; round += 1) {
    const duration = 40 + Math.floor(random() * 160);
    const windowSeconds = pick([5, 7.5, 10, 12.25, 30]);
    // Quarter seconds and eighths, so every score is exact and equal windows are exactly equal.
    const points = Array.from({ length: 1 + Math.floor(random() * 6) }, () => {
      const start = Math.floor(random() * duration * 4) / 4;
      return { start, end: Math.min(start + pick([0.25, 1.5, 4, 9.75, 20, 60]), duration), value: pick([0, 0.125, 0.25, 0.5, 0.75, 1]) };
    }).filter((point) => point.end > point.start);
    if (points.length === 0) continue;
    const latest = pick([LATEST, Math.floor(duration / 3)]);
    // The walk is rule 2's own, a second at a time, over the starts a window may have.
    const walked = scoreHeatmap(points, windowSeconds, Math.min(duration, latest + windowSeconds), 1);
    assert.equal(suggestWindowStart(points, duration, windowSeconds, latest), walked,
      JSON.stringify({ points, duration, windowSeconds, latest }));
    compared += 1;
  }
  assert.ok(compared > 350, String(compared));
});

test("a window's heatmap is the video's, moved to the window's own time and cut to it", () => {
  // A window of 60 s that begins at 1195 s: the last 11 s of a cool point,
  // then hot points to its end.
  assert.deepEqual(windowHeatmap(HOUR_MAP, 1195, 60), [
    { start: 0, end: 11, value: 0.1 }, { start: 11, end: 29, value: 1 }, { start: 29, end: 47, value: 1 },
    { start: 47, end: 60, value: 1 },
  ]);
  // A point that ends where the window begins, or begins where it ends, covers none of it.
  assert.deepEqual(windowHeatmap(HOUR_MAP, 1206, 18), [{ start: 0, end: 18, value: 1 }]);
  assert.deepEqual(windowHeatmap([{ start: 0, end: 10, value: 1 }, { start: 10, end: 20, value: 0.5 }], 0, 15),
    [{ start: 0, end: 10, value: 1 }, { start: 10, end: 15, value: 0.5 }]);
});

test("what a moved heatmap gives rule 2 is a list it accepts", () => {
  const moved = windowHeatmap(HOUR_MAP, 1195, 60);
  assert.equal(suggestionRule(60, { heatmap: moved }), "heatmap");
  // The best 15 s of that window begin where its hot part begins.
  near(scoreHeatmap(moved, 15, 60, 0.1), 11, 1e-9, "the start");
  // Unmoved, the video's own list reaches far past a 60 s clip and is not used at all.
  assert.equal(suggestionRule(60, { heatmap: HOUR_MAP }), "loudness");
});

test("a heatmap that reaches nowhere into the window, or is no heatmap, gives none", () => {
  assert.equal(windowHeatmap([{ start: 0, end: 100, value: 1 }], 100, 60), null);
  assert.equal(windowHeatmap([{ start: 500, end: 600, value: 1 }], 100, 60), null);
  for (const heatmap of [null, undefined, [], "x", [{ start: 0, end: 5 }], [{ start: 5, end: 5, value: 1 }],
    [{ start: -1, end: 5, value: 1 }], [{ start: 0, end: 5, value: 1.5 }], [null]]) {
    assert.equal(windowHeatmap(heatmap, 0, 60), null);
  }
  for (const [windowStart, windowSeconds] of [[-1, 60], [Number.NaN, 60], [0, 0], [0, -5], ["0", 60]]) {
    assert.equal(windowHeatmap(HOUR_MAP, windowStart, windowSeconds), null);
  }
});
