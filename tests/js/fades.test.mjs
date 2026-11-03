// The preview's fades must sound like the render's (core/static/audio/fades.js).
import { test } from "node:test";
import assert from "node:assert/strict";

import { fadeCurve, fadeGainAt, scheduleFades } from "../../core/static/audio/fades.js";

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
