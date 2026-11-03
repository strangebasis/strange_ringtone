// The BS.1770 preview meter (core/static/audio/loudness.js).
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  energyPrefix,
  gainForTarget,
  integratedLufs,
  kWeightingCoefficients,
  shortTermCurve,
} from "../../core/static/audio/loudness.js";

function sine(seconds, sampleRate, frequency, amplitude) {
  const out = new Float32Array(Math.round(seconds * sampleRate));
  for (let i = 0; i < out.length; i += 1) {
    out[i] = amplitude * Math.sin((2 * Math.PI * frequency * i) / sampleRate);
  }
  return out;
}

function near(actual, expected, tolerance, label) {
  assert.ok(Math.abs(actual - expected) <= tolerance, `${label}: ${actual} is not within ${tolerance} of ${expected}`);
}

test("the 48 kHz K-weighting coefficients are the standard's published values", () => {
  // ITU-R BS.1770-4, Tables 1 and 2.
  const { shelf, highpass } = kWeightingCoefficients(48000);
  const published = {
    shelf: { b: [1.53512485958697, -2.69169618940638, 1.19839281085285], a: [-1.69065929318241, 0.73248077421585] },
    highpass: { b: [1, -2, 1], a: [-1.99004745483398, 0.99007225036621] },
  };
  for (const [name, got, want] of [["shelf", shelf, published.shelf], ["highpass", highpass, published.highpass]]) {
    for (const key of ["b", "a"]) {
      assert.equal(got[key].length, want[key].length);
      want[key].forEach((value, i) => near(got[key][i], value, 1e-12, `${name}.${key}[${i}]`));
    }
  }
});

test("a sample rate at or below twice the shelf frequency is refused", () => {
  // tan() of the shelf's pre-warped frequency passes pi/2 there, and the
  // coefficients it gives are nonsense rather than an error.
  for (const rate of [2000, 3363, 0, -44100, NaN, Infinity]) {
    assert.throws(() => kWeightingCoefficients(rate), RangeError, String(rate));
  }
  assert.doesNotThrow(() => kWeightingCoefficients(8000));
});

test("the 44.1 kHz coefficients differ from the 48 kHz set (derived, not copied)", () => {
  const a = kWeightingCoefficients(44100);
  const b = kWeightingCoefficients(48000);
  assert.ok(Math.abs(a.shelf.a[0] - b.shelf.a[0]) > 1e-3);
  assert.ok(Math.abs(a.highpass.a[0] - b.highpass.a[0]) > 1e-5);
});

for (const rate of [44100, 48000]) {
  test(`a -20 dBFS 1 kHz sine reads -20.0 LUFS in stereo and -23.0 in mono at ${rate} Hz`, () => {
    // -20 dBFS is peak amplitude 0.1. Each channel's mean square is 0.005;
    // stereo sums two of them. K-weighting's ~+0.69 dB at 1 kHz cancels the
    // -0.691 offset, so stereo is 10*log10(0.01) = -20 and mono 3 dB less.
    const tone = sine(10, rate, 1000, 0.1);
    near(integratedLufs([tone, tone], rate), -20.0, 0.3, "stereo");
    near(integratedLufs([tone], rate), -23.0, 0.3, "mono");
  });
}

test("silence and input shorter than one 400 ms block measure -Infinity", () => {
  assert.equal(integratedLufs([new Float32Array(44100 * 5)], 44100), -Infinity);
  assert.equal(integratedLufs([new Float32Array(44100 * 5), new Float32Array(44100 * 5)], 44100), -Infinity);
  assert.equal(integratedLufs([sine(0.39, 44100, 1000, 0.5)], 44100), -Infinity);
  assert.ok(Number.isFinite(integratedLufs([sine(0.4, 44100, 1000, 0.5)], 44100)));
});

test("the absolute gate drops blocks at or below -70 LUFS", () => {
  // 5 s of tone, then 5 s of a tone 80 dB lower: without the absolute gate
  // the quiet half would pull the mean down by about 3 dB.
  const loud = sine(5, 44100, 1000, 0.1);
  const faint = sine(5, 44100, 1000, 0.00001);
  const mixed = new Float32Array(loud.length + faint.length);
  mixed.set(loud);
  mixed.set(faint, loud.length);
  // The blocks straddling the edge hold part of the tone, pass both gates and
  // cost about 0.13 dB; without the gate the result would be ~3 dB lower.
  near(integratedLufs([mixed], 44100), integratedLufs([loud], 44100), 0.2, "absolute gate");
});

test("a steady tone below -70 LUFS measures -Infinity; one just above is measured", () => {
  // A mono 1 kHz sine at amplitude A reads about 20 log10(A / 0.1) - 23 LUFS.
  // With nothing louder present the relative gate cannot hide the absolute
  // one: every block of the -80 LUFS tone is at or below -70 and dropped.
  const amplitude = (lufs) => 0.1 * 10 ** ((lufs + 23) / 20);
  assert.equal(integratedLufs([sine(3, 44100, 1000, amplitude(-80))], 44100), -Infinity);
  assert.equal(integratedLufs([sine(3, 44100, 1000, amplitude(-72))], 44100), -Infinity);
  near(integratedLufs([sine(3, 44100, 1000, amplitude(-68))], 44100), -68, 0.3, "just above the gate");
});

test("near the gate, blocks the relative gate keeps are still dropped by the absolute one", () => {
  // -65 LUFS then -73 LUFS: the quiet half is within 10 LU of the loud one,
  // so only the absolute gate removes it. Counting it would read ~2.5 dB lower.
  const amplitude = (lufs) => 0.1 * 10 ** ((lufs + 23) / 20);
  const loud = sine(5, 44100, 1000, amplitude(-65));
  const quiet = sine(5, 44100, 1000, amplitude(-73));
  const mixed = new Float32Array(loud.length + quiet.length);
  mixed.set(loud);
  mixed.set(quiet, loud.length);
  near(integratedLufs([mixed], 44100), integratedLufs([loud], 44100), 0.2, "absolute gate near -70");
});

test("the relative gate drops blocks more than 10 LU below the gated mean", () => {
  // A tone 20 dB lower (about -43 LUFS) passes the absolute gate but not the
  // relative one, so the result is the loud part alone.
  const loud = sine(5, 44100, 1000, 0.1);
  const quiet = sine(5, 44100, 1000, 0.01);
  const mixed = new Float32Array(loud.length + quiet.length);
  mixed.set(loud);
  mixed.set(quiet, loud.length);
  near(integratedLufs([mixed], 44100), integratedLufs([loud], 44100), 0.2, "relative gate");
  // 6 dB lower is inside the gate, so it does pull the result down.
  const nearby = sine(5, 44100, 1000, 0.05);
  mixed.set(nearby, loud.length);
  assert.ok(integratedLufs([mixed], 44100) < integratedLufs([loud], 44100) - 1);
});

test("blocks are 400 ms long and start every 100 ms", () => {
  // 1 s of silence, a 0.4 s tone, 1 s of silence. Counted by hand, the blocks
  // that touch the tone hold 1/4, 2/4, 3/4, 4/4, 3/4, 2/4 and 1/4 of a full
  // block's energy; silent blocks fall to the absolute gate and none falls to
  // the relative one. So the result is the steady tone's loudness plus
  // 10*log10(4/7). Steps of 200 ms would give 10*log10(2/3) (0.67 dB higher),
  // 400 ms 10*log10(1/2), 50 ms 10*log10(8/15).
  const rate = 44100;
  const burst = new Float32Array(Math.round(2.4 * rate));
  burst.set(sine(0.4, rate, 1000, 0.1), rate);
  const steady = integratedLufs([sine(10, rate, 1000, 0.1)], rate);
  near(integratedLufs([burst], rate), steady + 10 * Math.log10(4 / 7), 0.02, "gated burst");
});

test("K-weighting makes low frequencies count for less", () => {
  const low = sine(5, 44100, 20, 0.1);
  const mid = sine(5, 44100, 1000, 0.1);
  assert.ok(integratedLufs([low], 44100) < integratedLufs([mid], 44100) - 10);
});

test("the short-term curve has one 3 s window every 100 ms", () => {
  const tone = sine(10, 44100, 1000, 0.1);
  const curve = shortTermCurve([tone], 44100);
  assert.ok(curve instanceof Float32Array);
  assert.equal(curve.length, 71);
  for (const value of curve) {
    near(value, -23.0, 0.3, "short-term");
  }
  assert.equal(shortTermCurve([tone], 44100, 1).length, 8);
});

test("input shorter than 3 s gives one short-term value; silence is -Infinity", () => {
  const curve = shortTermCurve([sine(2, 44100, 1000, 0.1)], 44100);
  assert.equal(curve.length, 1);
  near(curve[0], -23.0, 0.3, "short input");
  assert.deepEqual([...shortTermCurve([new Float32Array(44100 * 4)], 44100)].every((v) => v === -Infinity), true);
});

test("the energy prefix sums whole 10 ms granules", () => {
  const { prefix, granule, count } = energyPrefix([new Float32Array(44100 + 100)], 44100);
  assert.equal(granule, 441);
  assert.equal(count, 100);
  assert.equal(prefix.length, 101);
  assert.ok(prefix instanceof Float64Array);
});

test("the meter takes one or two equal channels only", () => {
  const a = new Float32Array(44100);
  assert.throws(() => integratedLufs([], 44100), RangeError);
  assert.throws(() => integratedLufs([a, a, a], 44100), RangeError);
  assert.throws(() => integratedLufs([a, new Float32Array(10)], 44100), TypeError);
  assert.throws(() => shortTermCurve([a], 44100, 0), RangeError);
});

test("gainForTarget is target minus measured, clamped to +-20 dB", () => {
  assert.equal(gainForTarget(-20, -14), 6);
  assert.equal(gainForTarget(-8, -14), -6);
  assert.equal(gainForTarget(-50, -14), 20);
  assert.equal(gainForTarget(-1, -24), -20);
  assert.equal(gainForTarget(-30, -14, 10), 10);
});

test("a silent or unmeasurable selection previews the server's full clamp", () => {
  // ebur128 never reports below -70, so the server applies +20 dB.
  assert.equal(gainForTarget(-Infinity, -14), 20);
  assert.equal(gainForTarget(NaN, -14), 20);
  assert.equal(gainForTarget(-90, -24), 20);
  assert.equal(gainForTarget(-Infinity, -24, 50), 46);
});
