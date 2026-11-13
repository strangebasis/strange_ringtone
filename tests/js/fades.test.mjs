// The preview's fades must sound like the render's (core/static/audio/fades.js).
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  fadeCurve, fadeGainAt, scheduleFades, scheduleStretch, SEAM_SECONDS, stretchCurve, stretchGainAt,
} from "../../core/static/audio/fades.js";

function near(actual, expected, label) {
  assert.ok(Math.abs(actual - expected) <= 1e-9, `${label}: ${actual} != ${expected}`);
}

// ffmpeg's afade with curve=qsin: gain = sin(progress * pi / 2), progress
// running 0 -> 1 over a fade-in and 1 -> 0 over a fade-out.
const qsin = (progress) => Math.sin((progress * Math.PI) / 2);

test("the fade-in is a quarter sine from 0 to 1", () => {
  near(fadeGainAt(0, 10, 2, 0), 0, "start");
  near(fadeGainAt(0.5, 10, 2, 0), qsin(0.25), "quarter");
  near(fadeGainAt(1, 10, 2, 0), qsin(0.5), "half");
  near(fadeGainAt(2, 10, 2, 0), 1, "end of fade");
  near(fadeGainAt(5, 10, 2, 0), 1, "middle");
});

test("the fade-out is a quarter sine from 1 to 0", () => {
  near(fadeGainAt(8, 10, 0, 2), 1, "start of fade");
  near(fadeGainAt(9, 10, 0, 2), qsin(0.5), "half");
  near(fadeGainAt(9.5, 10, 0, 2), qsin(0.25), "three quarters");
  near(fadeGainAt(10, 10, 0, 2), 0, "end");
});

test("between the fades the gain is exactly 1", () => {
  // Just past the fade-in and just before the fade-out, and everywhere in
  // between: a fade that reached further would dip the plateau.
  for (const t of [2 + 1e-9, 2.001, 2.5, 3, 5, 7.5, 8.5 - 0.001, 8.5 - 1e-9]) {
    assert.equal(fadeGainAt(t, 10, 2, 1.5), 1, `t=${t}`);
  }
  assert.ok(fadeGainAt(2 - 1e-3, 10, 2, 1.5) < 1, "still fading in");
  assert.ok(fadeGainAt(8.5 + 1e-3, 10, 2, 1.5) < 1, "already fading out");
});

test("the shape is not linear", () => {
  // A linear ramp would give 0.5 halfway; qsin gives 0.707.
  assert.ok(fadeGainAt(1, 10, 2, 1.5) > 0.7);
});

test("overlapping fades multiply, as two afade filters in series do", () => {
  near(fadeGainAt(0.5, 1, 1, 1), qsin(0.5) * qsin(0.5), "middle");
  near(fadeGainAt(0.25, 1, 1, 1), qsin(0.25) * qsin(0.75), "quarter");
});

test("each fade is clamped to the duration, as render.py does", () => {
  near(fadeGainAt(0.5, 1, 5, 0), qsin(0.5), "long fade-in");
  near(fadeGainAt(0.5, 1, 0, 5), qsin(0.5), "long fade-out");
});

test("no fades means unity gain, and t is clamped to the selection", () => {
  for (const t of [0, 0.3, 1]) {
    near(fadeGainAt(t, 1, 0, 0), 1, `t=${t}`);
  }
  near(fadeGainAt(-1, 10, 2, 2), 0, "before");
  near(fadeGainAt(11, 10, 2, 2), 0, "after");
  near(fadeGainAt(1, 10, -1, NaN), 1, "bad fades are none");
  near(fadeGainAt(1, 0, 1, 1), 0, "empty selection");
});

test("the curve has at least 200 points per second and samples fadeGainAt", () => {
  const curve = fadeCurve(2.5, 0.2, 1.5);
  assert.ok(curve instanceof Float32Array);
  assert.ok(curve.length >= 2.5 * 200);
  assert.equal(curve[0], 0);
  assert.equal(curve[curve.length - 1], 0);
  for (let i = 0; i < curve.length; i += 1) {
    const t = (i / (curve.length - 1)) * 2.5;
    assert.equal(curve[i], Math.fround(fadeGainAt(t, 2.5, 0.2, 1.5)));
  }
  assert.ok(fadeCurve(0.001, 0, 0).length >= 2);
  assert.throws(() => fadeCurve(0, 1, 1), RangeError);
});

test("scheduleFades makes exactly one setValueCurveAtTime call per iteration", () => {
  // A fake AudioParam with only setValueCurveAtTime: any other automation
  // call (a ramp, setValueAtTime) would throw here.
  const calls = [];
  const param = {
    setValueCurveAtTime(values, startTime, duration) {
      calls.push({ values, startTime, duration });
    },
  };
  scheduleFades(param, 3.25, 30, 0.2, 1.5);
  assert.equal(calls.length, 1);
  const [{ values, startTime, duration }] = calls;
  assert.equal(startTime, 3.25);
  assert.equal(duration, 30);
  assert.ok(values.length >= 30 * 200);
  assert.equal(values[0], 0);
  assert.equal(values[values.length - 1], 0);
  assert.deepEqual(values, fadeCurve(30, 0.2, 1.5));
});

// --- a stretch of the selection: what the loop controls play (docs/PLAN.md 7.1) ---

// A 10 s selection with a 2 s fade-in and a 1.5 s fade-out.
const stretch = (t, offset, length) => stretchGainAt(t, offset, length, 10, 2, 1.5);

test("a stretch over the whole selection is the selection's own fade curve", () => {
  assert.deepEqual(stretchCurve(0, 2.5, 2.5, 0.2, 1.5), fadeCurve(2.5, 0.2, 1.5));
  assert.deepEqual(stretchCurve(0, 30, 30, 0, 0), fadeCurve(30, 0, 0));
});

test("inside a stretch the gain is what the finished tone has there", () => {
  // 1 s to 9.5 s: the fade-in is half done where the stretch begins, and the
  // fade-out half done where it ends. Away from the two cuts the gain is the
  // selection's, not a fade of the stretch's own.
  for (const t of [0.1, 0.5, 0.99, 3, 7.5, 8, 8.4]) {
    near(stretch(t, 1, 8.5), fadeGainAt(1 + t, 10, 2, 1.5), `t=${t}`);
  }
  assert.ok(stretch(0.5, 1, 8.5) < 1, "still fading in");
  assert.equal(stretch(3, 1, 8.5), 1);
  assert.ok(stretch(8, 1, 8.5) < 1, "already fading out");
});

test("a cut in mid-audio gets a 5 ms ramp, at either end of the stretch", () => {
  assert.equal(SEAM_SECONDS, 0.005);
  // 4 s to 6 s: full level in the tone, and both ends are cuts.
  assert.equal(stretch(0, 4, 2), 0);
  near(stretch(SEAM_SECONDS / 2, 4, 2), 0.5, "halfway up");
  near(stretch(SEAM_SECONDS, 4, 2), 1, "up");
  assert.equal(stretch(1, 4, 2), 1);
  near(stretch(2 - SEAM_SECONDS, 4, 2), 1, "not yet down");
  near(stretch(2 - SEAM_SECONDS / 2, 4, 2), 0.5, "halfway down");
  assert.equal(stretch(2, 4, 2), 0);
});

test("an end of the stretch that is an end of the selection gets no ramp: the tone has none there", () => {
  // No fades, so only a seam could lower the gain.
  const plain = (t, offset, length) => stretchGainAt(t, offset, length, 10, 0, 0);
  // From the selection's start: no ramp at the start, one at the cut.
  assert.equal(plain(0, 0, 4), 1);
  assert.equal(plain(4, 0, 4), 0);
  // To the selection's end: a ramp at the cut, none at the end.
  assert.equal(plain(0, 6, 4), 0);
  assert.equal(plain(4, 6, 4), 1);
});

test("a stretch at the selection's start begins with the fade-in itself", () => {
  near(stretch(0, 0, 4), 0, "silence");
  near(stretch(1, 0, 4), qsin(0.5), "half");
  near(stretch(2, 0, 4), 1, "end of fade");
});

test("frame arithmetic does not put a seam on a stretch that ends on the selection's end", () => {
  // Offsets and lengths come from whole frames divided by the sample rate,
  // and their sum can miss the selection's length by a rounding error.
  const rate = 44100;
  for (const [from, to, frames] of [[13_230, 661_500, 661_500], [1, 1_323_000, 1_323_000], [22_051, 99_999, 99_999]]) {
    const offset = from / rate;
    const length = (to - from) / rate;
    const duration = frames / rate;
    assert.equal(stretchGainAt(length, offset, length, duration, 0, 0), 1, `${from}..${to}`);
  }
  // A sum that is off by far more than any rounding, yet far less than a frame.
  assert.equal(stretchGainAt(1, 0, 1, 1 + 1e-9, 0, 0), 1);
  assert.equal(stretchGainAt(0, 1e-9, 1, 1 + 1e-9, 0, 0), 1);
  // One frame short of the end, or one frame in from the start, is a cut.
  assert.equal(stretchGainAt(1, 0, 1, 1 + 1 / rate, 0, 0), 0);
  assert.equal(stretchGainAt(0, 1 / rate, 1, 1 + 1 / rate, 0, 0), 0);
});

test("the stretch curve has the fade curve's grid and samples stretchGainAt", () => {
  const curve = stretchCurve(4, 2, 10, 2, 1.5);
  assert.ok(curve instanceof Float32Array);
  assert.ok(curve.length >= 2 * 200);
  assert.equal(curve[0], 0);
  assert.equal(curve[curve.length - 1], 0);
  for (let i = 0; i < curve.length; i += 1) {
    const t = (i / (curve.length - 1)) * 2;
    assert.equal(curve[i], Math.fround(stretchGainAt(t, 4, 2, 10, 2, 1.5)));
  }
  // The ramp is over by the second point: one grid step is at most 5 ms.
  assert.ok(curve[1] > 0.99);
  assert.ok(curve[curve.length - 2] > 0.99);
  assert.throws(() => stretchCurve(0, 0, 10, 1, 1), RangeError);
});

test("scheduleStretch makes exactly one setValueCurveAtTime call, as long as the stretch", () => {
  const calls = [];
  const param = {
    setValueCurveAtTime(values, startTime, duration) {
      calls.push({ values, startTime, duration });
    },
  };
  scheduleStretch(param, 3.25, 4, 2, 10, 2, 1.5);
  assert.equal(calls.length, 1);
  const [{ values, startTime, duration }] = calls;
  assert.equal(startTime, 3.25);
  assert.equal(duration, 2);
  assert.deepEqual(values, stretchCurve(4, 2, 10, 2, 1.5));
});
