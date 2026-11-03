// The upload encoder and the selection clamp (core/static/audio/wav.js).
// tests/test_frontend_js.py also feeds this encoder's output to wav.py.
import { test } from "node:test";
import assert from "node:assert/strict";

import { clampSelection, encodeWav } from "../../core/static/audio/wav.js";

function hex(bytes) {
  return Buffer.from(bytes).toString("hex");
}

test("the header is exactly RIFF, fmt 16, data, with correct sizes (stereo)", () => {
  const left = new Float32Array([0, 1, -1]);
  const right = new Float32Array([0.5, -0.5, 0]);
  const bytes = encodeWav([left, right], 44100, 0, 3);
  assert.equal(bytes.length, 44 + 3 * 4);
  assert.equal(
    hex(bytes.subarray(0, 44)),
    "52494646" + "30000000" + "57415645" + // RIFF, 48 = 56 bytes - 8, WAVE
    "666d7420" + "10000000" + // "fmt ", 16
    "0100" + "0200" + "44ac0000" + "10b10200" + "0400" + "1000" + // PCM, 2 ch, 44100, 176400, 4, 16
    "64617461" + "0c000000", // "data", 12
  );
  // Interleaved L R, little-endian int16.
  assert.equal(hex(bytes.subarray(44)), "0000" + "0040" + "ff7f" + "00c0" + "0080" + "0000");
});

test("the header is right for mono at 48000 Hz", () => {
  const bytes = encodeWav([new Float32Array(10)], 48000, 2, 7);
  const view = new DataView(bytes.buffer);
  assert.equal(bytes.length, 44 + 5 * 2);
  assert.equal(view.getUint32(4, true), bytes.length - 8);
  assert.equal(view.getUint16(22, true), 1);
  assert.equal(view.getUint32(24, true), 48000);
  assert.equal(view.getUint32(28, true), 96000);
  assert.equal(view.getUint16(32, true), 2);
  assert.equal(view.getUint32(40, true), 10);
});

test("samples clip to [-1, 1] and round half away from zero", () => {
  const values = [2, -2, 1, -1, 1.5 / 32768, -1.5 / 32768, 0.4 / 32768, -0.4 / 32768, NaN, Infinity, -Infinity, -0];
  const bytes = encodeWav([new Float32Array(values)], 44100, 0, values.length);
  const view = new DataView(bytes.buffer, 44);
  const out = values.map((_, i) => view.getInt16(i * 2, true));
  assert.deepEqual(out, [32767, -32768, 32767, -32768, 2, -2, 0, 0, 0, 32767, -32768, 0]);
});

test("only the selected frames are written", () => {
  const channel = new Float32Array([0.1, 0.2, 0.3, 0.4]);
  const bytes = encodeWav([channel], 44100, 1, 3);
  const view = new DataView(bytes.buffer, 44);
  assert.equal(bytes.length, 48);
  assert.equal(view.getInt16(0, true), Math.round(Math.fround(0.2) * 32768));
  assert.equal(view.getInt16(2, true), Math.round(Math.fround(0.3) * 32768));
});

test("the encoder refuses what the server would refuse", () => {
  const a = new Float32Array(100);
  assert.throws(() => encodeWav([], 44100, 0, 10), RangeError);
  assert.throws(() => encodeWav([a, a, a], 44100, 0, 10), RangeError);
  assert.throws(() => encodeWav([a], 22050, 0, 10), RangeError);
  assert.throws(() => encodeWav([a], 44100, 5, 5), RangeError);
  assert.throws(() => encodeWav([a], 44100, -1, 5), RangeError);
  assert.throws(() => encodeWav([a], 44100, 0, 101), RangeError);
  assert.throws(() => encodeWav([a], 44100, 0.5, 10), RangeError);
  assert.throws(() => encodeWav([a, new Float32Array(5)], 44100, 0, 10), RangeError);
  assert.throws(() => encodeWav([[0, 0, 0]], 44100, 0, 2), RangeError);
});

const MAX = 30 * 44100;
const MIN = 22050;
const LENGTH = 60 * 44100;

test("a selection inside the limits is kept", () => {
  assert.deepEqual(clampSelection(1000, 1000 + MIN, LENGTH, MAX, MIN), [1000, 1000 + MIN]);
  assert.deepEqual(clampSelection(0, MAX, LENGTH, MAX, MIN), [0, MAX]);
  assert.deepEqual(clampSelection(LENGTH - MAX, LENGTH, LENGTH, MAX, MIN), [LENGTH - MAX, LENGTH]);
});

test("the minimum defaults to half a second at 44100 Hz", () => {
  assert.deepEqual(clampSelection(0, 10, LENGTH, MAX), [0, 22050]);
});

test("over the cap, the kept edge stays and the other is pulled along", () => {
  assert.deepEqual(clampSelection(1000, 1000 + MAX + 1, LENGTH, MAX, MIN), [1000, 1000 + MAX]);
  assert.deepEqual(clampSelection(1000, 1000 + MAX + 1, LENGTH, MAX, MIN, "start"), [1000, 1000 + MAX]);
  assert.deepEqual(clampSelection(1000, 1000 + MAX + 1, LENGTH, MAX, MIN, "end"), [1001, 1001 + MAX]);
});

test("under the minimum, the selection grows from the kept edge", () => {
  assert.deepEqual(clampSelection(5000, 5001, LENGTH, MAX, MIN), [5000, 5000 + MIN]);
  assert.deepEqual(clampSelection(50000, 50001, LENGTH, MAX, MIN, "end"), [50001 - MIN, 50001]);
});

test("a window pushed past either end slides back inside at full length", () => {
  assert.deepEqual(clampSelection(LENGTH - 10, LENGTH, LENGTH, MAX, MIN), [LENGTH - MIN, LENGTH]);
  assert.deepEqual(clampSelection(0, 10, LENGTH, MAX, MIN, "end"), [0, MIN]);
  assert.deepEqual(clampSelection(-500, MAX + 500, LENGTH, MAX, MIN, "end"), [500, MAX + 500]);
  assert.deepEqual(clampSelection(-500, LENGTH + 500, LENGTH, MAX, MIN), [0, MAX]);
  assert.deepEqual(clampSelection(LENGTH - 5, LENGTH + 500, LENGTH, MAX, MIN), [LENGTH - MIN, LENGTH]);
});

test("edges are whole frames and in order", () => {
  assert.deepEqual(clampSelection(100.4, 100.6 + MIN, LENGTH, MAX, MIN), [100, 101 + MIN]);
  assert.deepEqual(clampSelection(1000 + MIN, 1000, LENGTH, MAX, MIN), [1000, 1000 + MIN]);
});

test("short audio: the whole input once it is at least the minimum", () => {
  assert.deepEqual(clampSelection(0, MIN + 3, MIN + 3, MAX, MIN), [0, MIN + 3]);
  assert.deepEqual(clampSelection(0, MIN, MIN, MAX, MIN), [0, MIN]);
  assert.throws(() => clampSelection(0, MIN - 1, MIN - 1, MAX, MIN), RangeError);
});

test("every result is between the minimum and the maximum", () => {
  for (const [s, e, keep] of [[0, 0, "start"], [0, 0, "end"], [LENGTH, LENGTH, "start"], [LENGTH, LENGTH, "end"],
    [7, LENGTH * 2, "start"], [-LENGTH, 7, "end"], [123456, 123457, "end"]]) {
    const [start, end] = clampSelection(s, e, LENGTH, MAX, MIN, keep);
    assert.ok(start >= 0 && end <= LENGTH && Number.isInteger(start) && Number.isInteger(end));
    assert.ok(end - start >= MIN && end - start <= MAX, `${s} ${e} ${keep} -> ${start} ${end}`);
  }
});

test("inconsistent limits and non-finite edges are refused", () => {
  assert.throws(() => clampSelection(NaN, 10, LENGTH, MAX, MIN), RangeError);
  assert.throws(() => clampSelection(0, Infinity, LENGTH, MAX, MIN), RangeError);
  assert.throws(() => clampSelection(0, 10, LENGTH, MIN - 1, MIN), RangeError);
  assert.throws(() => clampSelection(0, 10, LENGTH, MAX, 0), RangeError);
  assert.throws(() => clampSelection(0, 10, LENGTH + 0.5, MAX, MIN), RangeError);
});
