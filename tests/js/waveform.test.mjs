// Moving the selection's edges (moveEdge in core/static/waveform.js): the cap
// and the minimum hold for every drag and nudge, and an edge pushed past
// either one moves the other edge instead of being stopped. At the end, the
// Waveform class on a fake canvas: device-pixel sizing and pointer drags.
import { test } from "node:test";
import assert from "node:assert/strict";

import { moveEdge, Waveform } from "../../core/static/waveform.js";

const LENGTH = 1_000_000;
const MAX = 150_000;
const MIN = 22_050;
const SELECTION = Object.freeze({ start: 100_000, end: 200_000 });

const move = (edge, frame, selection = SELECTION) => moveEdge(selection, edge, frame, LENGTH, MAX, MIN);

test("dragging the start past the cap pulls the end along", () => {
  assert.deepEqual(move("start", 10_000), { start: 10_000, end: 160_000 });
});

test("dragging the end past the cap pulls the start along", () => {
  assert.deepEqual(move("end", 300_000), { start: 150_000, end: 300_000 });
});

test("within the cap only the dragged edge moves", () => {
  assert.deepEqual(move("start", 80_000), { start: 80_000, end: 200_000 });
  assert.deepEqual(move("end", 220_000), { start: 100_000, end: 220_000 });
});

test("pushing an edge closer than the minimum pushes the other edge", () => {
  assert.deepEqual(move("start", 195_000), { start: 195_000, end: 195_000 + MIN });
  assert.deepEqual(move("end", 110_000), { start: 110_000 - MIN, end: 110_000 });
  // Past the other edge altogether.
  assert.deepEqual(move("start", 400_000), { start: 400_000, end: 400_000 + MIN });
  assert.deepEqual(move("end", 50_000), { start: 50_000 - MIN, end: 50_000 });
});

test("edges stay inside the input with room for the minimum", () => {
  assert.deepEqual(move("start", -5), { start: 0, end: 150_000 });
  assert.deepEqual(move("start", LENGTH + 5), { start: LENGTH - MIN, end: LENGTH });
  assert.deepEqual(move("end", LENGTH + 5), { start: LENGTH - MAX, end: LENGTH });
  assert.deepEqual(move("end", -5), { start: 0, end: MIN });
});

test("move shifts the whole selection and keeps its length", () => {
  assert.deepEqual(move("move", 500_000), { start: 500_000, end: 600_000 });
  assert.deepEqual(move("move", -10), { start: 0, end: 100_000 });
  assert.deepEqual(move("move", LENGTH), { start: LENGTH - 100_000, end: LENGTH });
});

test("frames are whole, and an unknown edge changes nothing", () => {
  assert.deepEqual(move("start", 80_000.6), { start: 80_001, end: 200_000 });
  assert.deepEqual(move("middle", 5), { start: 100_000, end: 200_000 });
  // The selection passed in is not modified.
  assert.deepEqual(SELECTION, { start: 100_000, end: 200_000 });
});

// --- the canvas: device pixels and pointer drags ---------------------------------

// What the Waveform class touches of the browser, recorded.
class FakeCanvas {
  constructor(cssWidth) {
    this.cssWidth = cssWidth;
    this.width = 300;
    this.height = 150;
    this.listeners = new Map();
    this.captured = new Set();
    this.captureCalls = [];
    this.transforms = [];
    const canvas = this;
    // Every drawing call is accepted; only setTransform is looked at.
    this.drawing = new Proxy({}, {
      get: (target, key) => (key === "setTransform" ? (...args) => canvas.transforms.push(args) : () => {}),
      set: () => true,
    });
  }

  getContext() { return this.drawing; }
  getBoundingClientRect() { return { left: 10, top: 0, width: this.cssWidth, height: 160 }; }
  addEventListener(type, listener) { this.listeners.set(type, listener); }
  setPointerCapture(id) { this.captureCalls.push(["set", id]); this.captured.add(id); }
  hasPointerCapture(id) { return this.captured.has(id); }
  releasePointerCapture(id) { this.captureCalls.push(["release", id]); this.captured.delete(id); }

  fire(type, pointerId, clientX) {
    const event = { pointerId, clientX, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } };
    this.listeners.get(type)(event);
    return event;
  }
}

function installBrowser(devicePixelRatio) {
  globalThis.window = { devicePixelRatio, matchMedia: () => ({ addEventListener() {} }), addEventListener() {} };
  globalThis.ResizeObserver = class { observe() {} };
  globalThis.getComputedStyle = () => ({ getPropertyValue: () => "" });
}

test("the canvas is sized in device pixels and drawn in CSS pixels", () => {
  installBrowser(3);
  const canvas = new FakeCanvas(300);
  const waveform = new Waveform(canvas);
  waveform.setAudio([new Float32Array(441_000)], 44_100, 441_000, { maxFrames: MAX, minFrames: MIN });
  // Sharp on a Retina screen: three device pixels per CSS pixel each way.
  assert.equal(canvas.width, 900);
  assert.equal(canvas.height, 480);
  assert.deepEqual(canvas.transforms[canvas.transforms.length - 1], [3, 0, 0, 3, 0, 0]);
});

test("a drag captures the pointer, moves the edge it picked up and releases on lift", () => {
  installBrowser(2);
  // 300 CSS px for 441 000 frames: 1470 frames per pixel, offset 10 px.
  const canvas = new FakeCanvas(300);
  const waveform = new Waveform(canvas);
  waveform.setAudio([new Float32Array(441_000)], 44_100, 441_000, { maxFrames: 441_000, minFrames: MIN });
  waveform.setSelection(147_000, 294_000);
  const changes = [];
  let commits = 0;
  waveform.on("change", (selection) => changes.push(selection));
  waveform.on("commit", () => { commits += 1; });

  // The start handle sits at x = 100 (110 on the page); 15 px off is within reach.
  const down = canvas.fire("pointerdown", 7, 125);
  assert.equal(down.defaultPrevented, true);
  // Captured, so the drag survives the finger leaving the canvas.
  assert.deepEqual(canvas.captureCalls, [["set", 7]]);

  // Another finger does not move the handle.
  canvas.fire("pointermove", 8, 40);
  assert.deepEqual(changes, []);

  canvas.fire("pointermove", 7, 40);
  assert.deepEqual(changes, [{ start: 44_100, end: 294_000 }]);
  canvas.fire("pointerup", 7, 40);
  assert.deepEqual(canvas.captureCalls, [["set", 7], ["release", 7]]);
  assert.equal(commits, 1);

  // Lifted: further moves change nothing.
  canvas.fire("pointermove", 7, 200);
  assert.equal(changes.length, 1);
});

test("clear drops the clip's samples so the next file does not share memory with them", () => {
  installBrowser(1);
  const canvas = new FakeCanvas(300);
  const waveform = new Waveform(canvas);
  waveform.setAudio([new Float32Array(441_000)], 44_100, 441_000, { maxFrames: MAX, minFrames: MIN });
  waveform.setSelection(1000, 50_000);
  waveform.computePeaks(300);
  assert.notEqual(waveform.peaks, null);
  waveform.clear();
  assert.equal(waveform.channels, null);
  assert.equal(waveform.length, 0);
  assert.equal(waveform.peaks, null);
  assert.deepEqual(waveform.selection, { start: 0, end: 0 });
  // Drawing with nothing loaded is still fine.
  waveform.draw();
});
