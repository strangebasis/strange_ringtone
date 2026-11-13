// The magnified view (core/static/zoom.js, docs/PLAN.md 7.1) on a fake canvas
// and fake bar elements: how the view fits the selection, that it holds still
// while one of its trim bars is dragged and re-fits on the lift, what the loop
// bars may do, which element takes which touch, and what is drawn for the
// playhead.
import { test } from "node:test";
import assert from "node:assert/strict";

import { fitView, HEIGHT_CSS_PX, ZoomView } from "../../core/static/zoom.js";

const RATE = 44_100;
const LENGTH = 100 * RATE;
// 30 s to 50 s. In a 348 px view the margins are 24 px each, which leaves
// 300 px for the selection: 2940 frames a pixel.
const START = 30 * RATE;
const END = 50 * RATE;
const WIDTH = 348;
const PER_PX = 2940;
const VIEW_START = START - 24 * PER_PX;
const LIMITS = Object.freeze({ maxFrames: 30 * RATE, minFrames: RATE / 2, minLoopFrames: RATE / 4 });
// The colours zoom.js falls back on when the stylesheet gives none.
const WAVE = "#0a64d8";
const WAVE_DIM = "#9aa5b1";
const HANDLE = "#d93025";
const FADE = "rgba(0, 0, 0, 0.35)";

// A 2D context that records every call with the styles in force at the time.
function recordingContext() {
  const state = { fillStyle: "", strokeStyle: "", globalAlpha: 1, lineWidth: 1, lineCap: "butt" };
  const ops = [];
  const context = new Proxy({}, {
    get: (target, key) => (key in state ? state[key] : (...args) => {
      ops.push({ op: key, args, fillStyle: state.fillStyle, strokeStyle: state.strokeStyle, alpha: state.globalAlpha });
    }),
    set: (target, key, value) => {
      state[key] = value;
      return true;
    },
  });
  return { ops, context };
}

class FakeCanvas {
  constructor(left, cssWidth) {
    this.left = left;
    this.cssWidth = cssWidth;
    this.width = 300;
    this.height = 150;
    const { ops, context } = recordingContext();
    this.ops = ops;
    this.context = context;
  }

  getContext() { return this.context; }
  getBoundingClientRect() { return { left: this.left, top: 0, width: this.cssWidth, height: HEIGHT_CSS_PX }; }
}

// The element laid over a bar: what zoom.js touches of it, recorded.
class FakeBar {
  constructor() {
    this.listeners = new Map();
    this.props = {};
    this.style = { setProperty: (name, value) => { this.props[name] = value; } };
    this.hidden = true;
    this.captured = new Set();
    this.captureCalls = [];
  }

  addEventListener(type, listener) { this.listeners.set(type, listener); }
  setPointerCapture(id) { this.captureCalls.push(["set", id]); this.captured.add(id); }
  hasPointerCapture(id) { return this.captured.has(id); }
  releasePointerCapture(id) { this.captureCalls.push(["release", id]); this.captured.delete(id); }

  fire(type, pointerId, clientX) {
    const event = { pointerId, clientX, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } };
    this.listeners.get(type)(event);
    return event;
  }

  /** Where the element is, in CSS pixels from the view's left edge. */
  get x() { return Number.parseFloat(this.props["--x"]); }
}

function installBrowser({ ratio = 1, reducedMotion = false } = {}) {
  const frames = new Map();
  let id = 0;
  globalThis.window = {
    devicePixelRatio: ratio,
    matchMedia: (query) => ({ matches: reducedMotion && query.includes("reduced-motion"), addEventListener() {} }),
    addEventListener() {},
  };
  globalThis.ResizeObserver = class { observe() {} };
  globalThis.getComputedStyle = () => ({ getPropertyValue: () => "" });
  globalThis.requestAnimationFrame = (callback) => {
    id += 1;
    frames.set(id, callback);
    return id;
  };
  globalThis.cancelAnimationFrame = (handle) => { frames.delete(handle); };
  return {
    /** Let one frame pass at time `now` (ms); returns how many callbacks ran. */
    frame(now) {
      const due = [...frames.values()];
      frames.clear();
      for (const callback of due) callback(now);
      return due.length;
    },
    pending: () => frames.size,
  };
}

// A view 10 px in from the page's left edge, under a waveform canvas that is
// 30 px in and 300 px wide, showing START to END.
function makeView(options = {}) {
  const browser = installBrowser(options);
  const canvas = new FakeCanvas(10, options.width ?? WIDTH);
  const top = new FakeCanvas(30, 300);
  const bars = { start: new FakeBar(), end: new FakeBar(), loopStart: new FakeBar(), loopEnd: new FakeBar() };
  const view = new ZoomView({ canvas, top, bars });
  view.setAudio([new Float32Array(LENGTH)], RATE, LENGTH, LIMITS);
  view.setSelection(options.start ?? START, options.end ?? END);
  const events = [];
  for (const name of ["change", "commit", "loopchange", "loopcommit"]) {
    view.on(name, (detail) => events.push(detail === undefined ? [name] : [name, detail]));
  }
  return { view, canvas, top, bars, browser, events };
}

// The waveform's columns as last drawn: the 1 px wide rectangles, by x.
function columns(canvas) {
  const found = new Map();
  for (const { op, args, fillStyle, alpha } of canvas.ops) {
    if (op === "fillRect" && args[2] === 1) found.set(args[0], { fillStyle, alpha });
  }
  return found;
}

function redraw(view, canvas) {
  canvas.ops.length = 0;
  view.draw();
}

test("the view shows the selection between two margins of at least 24 px", () => {
  // 6 % of 348 px is less than 24 px, so the margin is 24 px.
  assert.deepEqual(fitView({ start: START, end: END }, WIDTH), { start: VIEW_START, perPx: PER_PX });
  // A wide view gets 6 % of its width on either side.
  const wide = fitView({ start: START, end: END }, 1000);
  assert.equal(wide.perPx, (END - START) / 880);
  assert.equal(wide.start, START - 60 * wide.perPx);
  // The margin may reach before the audio's start: the view still fits.
  assert.equal(fitView({ start: 0, end: END - START }, WIDTH).start, -24 * PER_PX);
});

test("audio without a selection yet shows no bar and draws at no scale", () => {
  installBrowser();
  const canvas = new FakeCanvas(10, WIDTH);
  const bars = { start: new FakeBar(), end: new FakeBar(), loopStart: new FakeBar(), loopEnd: new FakeBar() };
  const view = new ZoomView({ canvas, top: new FakeCanvas(30, 300), bars });
  // The page sets the audio first and the selection right after.
  view.setAudio([new Float32Array(LENGTH)], RATE, LENGTH, LIMITS);
  assert.equal(view.view, null);
  for (const bar of Object.values(bars)) assert.equal(bar.hidden, true);
  assert.equal(bars.start.props["--x"], undefined, "no position from a scale of zero");
  assert.equal(bars.start.fire("pointerdown", 7, 34).defaultPrevented, false);
  view.setSelection(START, END);
  assert.deepEqual(view.view, { start: VIEW_START, perPx: PER_PX });
  assert.equal(bars.start.hidden, false);
  assert.equal(bars.start.x, 24);
});

test("the canvas is sized in device pixels and drawn in CSS pixels", () => {
  const { canvas } = makeView({ ratio: 2 });
  assert.equal(canvas.width, WIDTH * 2);
  assert.equal(canvas.height, HEIGHT_CSS_PX * 2);
  const transforms = canvas.ops.filter(({ op }) => op === "setTransform");
  assert.deepEqual(transforms[transforms.length - 1].args, [2, 0, 0, 2, 0, 0]);
});

test("each trim bar's element sits on its bar, and the loop bars' elements are hidden without a loop", () => {
  const { view, bars } = makeView();
  assert.equal(bars.start.hidden, false);
  assert.equal(bars.end.hidden, false);
  assert.equal(bars.start.x, 24);
  assert.equal(bars.end.x, 324);
  assert.equal(bars.loopStart.hidden, true);
  assert.equal(bars.loopEnd.hidden, true);
  view.setLoop({ start: START, end: START + 150 * PER_PX });
  assert.equal(bars.loopStart.hidden, false);
  assert.equal(bars.loopStart.x, 24);
  assert.equal(bars.loopEnd.x, 174);
  view.setLoop(null);
  assert.equal(bars.loopStart.hidden, true);
  assert.equal(bars.loopEnd.hidden, true);
});

test("a trim bar under a loop bar keeps a touch target of its own: the tip above the loop bar", () => {
  const { bars } = makeView();
  const top = (bar) => Number.parseFloat(bar.props["--top"]);
  const height = (bar) => Number.parseFloat(bar.props["--height"]);
  for (const trim of [bars.start, bars.end]) {
    for (const loop of [bars.loopStart, bars.loopEnd]) {
      // The loop bars lie in front (style.css), so where the two overlap the
      // loop bar takes the touch. The trim bar must stand well clear above.
      assert.ok(top(loop) - top(trim) >= 40, `tip of ${top(loop) - top(trim)} px`);
      // And the loop bar hangs below the trim bar, where it is alone.
      assert.ok(top(loop) + height(loop) > top(trim) + height(trim));
      assert.equal(top(loop) + height(loop), HEIGHT_CSS_PX);
    }
  }
});

test("dragging a trim bar moves its edge by the view's scale, and the bar does not jump to the finger", () => {
  const { view, bars, events } = makeView();
  // 10 px right of the bar's centre, still on its 44 px element.
  const down = bars.start.fire("pointerdown", 7, 10 + 24 + 10);
  assert.equal(down.defaultPrevented, true);
  assert.deepEqual(bars.start.captureCalls, [["set", 7]]);
  // A press alone moves nothing.
  bars.start.fire("pointermove", 7, 10 + 24 + 10);
  assert.deepEqual(events, []);
  // 50 px to the right: 50 px of audio at this view's scale, not 60.
  bars.start.fire("pointermove", 7, 10 + 24 + 10 + 50);
  assert.deepEqual(events, [["change", { start: START + 50 * PER_PX, end: END, edge: "start" }]]);
  assert.deepEqual(view.selection, { start: START + 50 * PER_PX, end: END });
  assert.equal(bars.start.x, 74);
});

test("the picture holds still while a trim bar is dragged", () => {
  const { view, bars } = makeView();
  bars.start.fire("pointerdown", 7, 34);
  bars.start.fire("pointermove", 7, 84);
  // The other bar has not moved: re-fitting would have slid it.
  assert.equal(bars.end.x, 324);
  assert.deepEqual(view.view, { start: VIEW_START, perPx: PER_PX });
  // The page answers every change by showing the selection it applied.
  view.setSelection(START + 50 * PER_PX, END);
  assert.deepEqual(view.view, { start: VIEW_START, perPx: PER_PX });
  assert.equal(bars.start.x, 74);
  // Also when the page corrected it: the answer is shown on the same picture.
  view.setSelection(START + 51 * PER_PX, END - PER_PX);
  assert.deepEqual(view.view, { start: VIEW_START, perPx: PER_PX });
  assert.equal(bars.start.x, 75);
  assert.equal(bars.end.x, 323);
  view.setSelection(START + 50 * PER_PX, END);
  // A second move is measured on the same picture.
  bars.start.fire("pointermove", 7, 94);
  assert.deepEqual(view.selection, { start: START + 60 * PER_PX, end: END });
});

test("letting a trim bar go commits, and the view glides to fit the new selection", () => {
  const { view, bars, browser, events } = makeView();
  bars.start.fire("pointerdown", 7, 34);
  bars.start.fire("pointermove", 7, 84);
  bars.start.fire("pointerup", 7, 84);
  assert.deepEqual(bars.start.captureCalls, [["set", 7], ["release", 7]]);
  assert.deepEqual(events[events.length - 1], ["commit"]);
  // Not yet: the glide runs on animation frames.
  assert.equal(bars.start.x, 74);
  assert.equal(browser.frame(1000), 1);
  assert.equal(browser.frame(1075), 1);
  assert.ok(bars.start.x > 24 && bars.start.x < 74, `halfway: ${bars.start.x}`);
  assert.equal(browser.frame(1150), 1);
  // Fitted: 735 000 frames over the same 300 px.
  assert.deepEqual(view.view, fitView({ start: START + 50 * PER_PX, end: END }, WIDTH));
  assert.equal(bars.start.x, 24);
  assert.equal(bars.end.x, 324);
  assert.equal(browser.pending(), 0, "the glide is over");
  // Lifted: further moves change nothing.
  bars.start.fire("pointermove", 7, 200);
  assert.equal(events.filter(([name]) => name === "change").length, 1);
});

test("with reduced motion the view jumps to fit instead of gliding", () => {
  const { view, bars, browser } = makeView({ reducedMotion: true });
  bars.start.fire("pointerdown", 7, 34);
  bars.start.fire("pointermove", 7, 84);
  bars.start.fire("pointerup", 7, 84);
  assert.equal(browser.pending(), 0);
  assert.deepEqual(view.view, fitView({ start: START + 50 * PER_PX, end: END }, WIDTH));
  assert.equal(bars.start.x, 24);
});

test("a press during the glide holds the picture where it has got to", () => {
  const { view, bars, browser } = makeView();
  bars.start.fire("pointerdown", 7, 34);
  bars.start.fire("pointermove", 7, 84);
  bars.start.fire("pointerup", 7, 84);
  browser.frame(1000);
  browser.frame(1075);
  const held = { ...view.view };
  const at = bars.end.x;
  bars.end.fire("pointerdown", 8, 10 + at);
  assert.equal(browser.pending(), 0, "the glide stopped");
  browser.frame(1150);
  assert.deepEqual(view.view, held);
  bars.end.fire("pointerup", 8, 10 + at);
});

test("a selection set from outside re-fits the view at once", () => {
  const { view, bars, browser } = makeView();
  // The waveform's handle, or a nudge button: no drag of this view's own.
  view.setSelection(START + 10 * RATE, END);
  assert.equal(browser.pending(), 0);
  assert.deepEqual(view.view, fitView({ start: START + 10 * RATE, end: END }, WIDTH));
  assert.equal(bars.start.x, 24);
  assert.equal(bars.end.x, 324);
});

test("a trim bar cannot leave the picture, and the cap and the minimum hold as for the waveform's handles", () => {
  const { view, bars } = makeView();
  bars.start.fire("pointerdown", 7, 34);
  // Far past the left edge: the bar stops at the view's first frame.
  bars.start.fire("pointermove", 7, -500);
  assert.equal(view.selection.start, VIEW_START);
  // 20 s plus the margin is under the 30 s cap, so the end stayed.
  assert.equal(view.selection.end, END);
  // Far to the right: the start stops half a second before the view's last
  // frame and pushes the end along (moveEdge's minimum).
  bars.start.fire("pointermove", 7, 5000);
  const last = VIEW_START + WIDTH * PER_PX;
  assert.deepEqual(view.selection, { start: last, end: last + LIMITS.minFrames });
  bars.start.fire("pointerup", 7, 5000);
});

test("another finger does not move a bar that is being dragged", () => {
  const { bars, events } = makeView();
  bars.start.fire("pointerdown", 7, 34);
  bars.start.fire("pointermove", 8, 200);
  // A second press, on the other bar, is not a second drag.
  const second = bars.end.fire("pointerdown", 8, 334);
  assert.equal(second.defaultPrevented, false);
  assert.deepEqual(bars.end.captureCalls, []);
  bars.end.fire("pointermove", 8, 200);
  bars.start.fire("pointerup", 8, 200);
  assert.deepEqual(events, []);
  bars.start.fire("pointerup", 7, 34);
  assert.deepEqual(events, [["commit"]]);
});

test("dragging a loop bar moves the loop and nothing else", () => {
  const { view, bars, browser, events } = makeView();
  view.setLoop({ start: START, end: START + 150 * PER_PX });
  bars.loopEnd.fire("pointerdown", 7, 10 + 174);
  bars.loopEnd.fire("pointermove", 7, 10 + 250);
  assert.deepEqual(events, [["loopchange", { start: START, end: START + 226 * PER_PX }]]);
  assert.equal(bars.loopEnd.x, 250);
  // The selection and the picture are as they were.
  assert.deepEqual(view.selection, { start: START, end: END });
  assert.deepEqual(view.view, { start: VIEW_START, perPx: PER_PX });
  bars.loopEnd.fire("pointerup", 7, 10 + 250);
  assert.deepEqual(events[events.length - 1], ["loopcommit"]);
  assert.deepEqual(bars.loopEnd.captureCalls, [["set", 7], ["release", 7]]);
  // No re-fit: nothing about the selection changed.
  assert.equal(browser.pending(), 0);
});

test("a loop bar stops at its trim bar, where it may rest, and at the shortest loop from the other bar", () => {
  const { view, bars } = makeView();
  view.setLoop({ start: START + 100 * PER_PX, end: START + 150 * PER_PX });
  bars.loopEnd.fire("pointerdown", 7, 10 + 174);
  // Past the trim bar at 324 px.
  bars.loopEnd.fire("pointermove", 7, 10 + 345);
  assert.equal(view.loop.end, END);
  // Back towards the loop's start: a quarter of a second is the shortest.
  bars.loopEnd.fire("pointermove", 7, 10 + 30);
  assert.deepEqual(view.loop, { start: START + 100 * PER_PX, end: START + 100 * PER_PX + LIMITS.minLoopFrames });
  bars.loopEnd.fire("pointerup", 7, 10 + 30);
  bars.loopStart.fire("pointerdown", 8, 10 + 124);
  bars.loopStart.fire("pointermove", 8, 10 + 2);
  assert.equal(view.loop.start, START);
  bars.loopStart.fire("pointerup", 8, 10 + 2);
});

test("a loop bar let go within 4 px of its trim bar rests exactly on it", () => {
  const { view, bars } = makeView();
  view.setLoop({ start: START + 100 * PER_PX, end: START + 150 * PER_PX });
  bars.loopEnd.fire("pointerdown", 7, 10 + 174);
  // 5 px short of the trim bar at 324 px: where the finger is.
  bars.loopEnd.fire("pointermove", 7, 10 + 319);
  assert.equal(view.loop.end, VIEW_START + 319 * PER_PX);
  // 4 px short: on the trim bar, to the frame, so it will travel with it.
  bars.loopEnd.fire("pointermove", 7, 10 + 320);
  assert.equal(view.loop.end, END);
  bars.loopEnd.fire("pointerup", 7, 10 + 320);
  bars.loopStart.fire("pointerdown", 8, 10 + 124);
  bars.loopStart.fire("pointermove", 8, 10 + 27);
  assert.equal(view.loop.start, START);
  // The other side's trim bar does not attract it.
  bars.loopStart.fire("pointermove", 8, 10 + 29);
  assert.equal(view.loop.start, VIEW_START + 29 * PER_PX);
  bars.loopStart.fire("pointerup", 8, 10 + 29);
});

test("without a loop the loop bars' elements take no drag", () => {
  const { bars, events } = makeView();
  const down = bars.loopStart.fire("pointerdown", 7, 34);
  assert.equal(down.defaultPrevented, false);
  assert.deepEqual(bars.loopStart.captureCalls, []);
  bars.loopStart.fire("pointermove", 7, 100);
  assert.deepEqual(events, []);
});

test("the selection is grey, the audio outside it fainter, and nothing is drawn where there is no audio", () => {
  const { view, canvas } = makeView({ start: 0, end: END - START });
  redraw(view, canvas);
  const drawn = columns(canvas);
  // The left margin lies before the clip's start.
  for (let column = 0; column < 24; column += 1) assert.equal(drawn.has(column), false, `column ${column}`);
  assert.deepEqual(drawn.get(24), { fillStyle: WAVE_DIM, alpha: 1 });
  assert.deepEqual(drawn.get(323), { fillStyle: WAVE_DIM, alpha: 1 });
  // Past the end bar: the same grey, fainter.
  assert.equal(drawn.get(324).fillStyle, WAVE_DIM);
  assert.ok(drawn.get(324).alpha < 1);
  assert.equal(drawn.get(347).fillStyle, WAVE_DIM);
});

test("what has played so far on this pass takes the waveform's colour", () => {
  const { view, canvas } = makeView();
  // The first 100 px of the selection have been heard.
  view.setPlayed({ from: START, to: START + 100 * PER_PX });
  redraw(view, canvas);
  let drawn = columns(canvas);
  assert.equal(drawn.get(23).fillStyle, WAVE_DIM, "the margin is never played");
  assert.equal(drawn.get(24).fillStyle, WAVE);
  assert.equal(drawn.get(123).fillStyle, WAVE);
  assert.equal(drawn.get(124).fillStyle, WAVE_DIM, "yet to be played");
  assert.equal(drawn.get(323).fillStyle, WAVE_DIM);
  // A loop in the middle: the fill starts at the loop's start bar.
  view.setPlayed({ from: START + 100 * PER_PX, to: START + 150 * PER_PX });
  redraw(view, canvas);
  drawn = columns(canvas);
  assert.equal(drawn.get(123).fillStyle, WAVE_DIM);
  assert.equal(drawn.get(124).fillStyle, WAVE);
  assert.equal(drawn.get(173).fillStyle, WAVE);
  assert.equal(drawn.get(174).fillStyle, WAVE_DIM);
  // Stopped: all grey again.
  view.setPlayed(null);
  redraw(view, canvas);
  drawn = columns(canvas);
  assert.ok([...drawn.values()].every(({ fillStyle }) => fillStyle === WAVE_DIM));
});

test("a diagonal joins each of the waveform's handles to its trim bar", () => {
  const { view, canvas } = makeView();
  redraw(view, canvas);
  const lines = canvas.ops.filter(({ op, strokeStyle }) => (op === "moveTo" || op === "lineTo") && strokeStyle === HANDLE);
  // The waveform's canvas is 20 px in from the view's left edge and 300 px
  // wide: its handles stand at 30 % and 50 % of that.
  assert.deepEqual(lines.map(({ op, args }) => [op, ...args]), [
    ["moveTo", 20 + 90, 0], ["lineTo", 24, 32],
    ["moveTo", 20 + 150, 0], ["lineTo", 324, 32],
  ]);
  assert.equal(canvas.ops.filter(({ op }) => op === "stroke").length, 2);
});

test("the fades are shaded over the selection, and only when there are fades", () => {
  const { view, canvas } = makeView();
  redraw(view, canvas);
  assert.equal(canvas.ops.filter(({ op, fillStyle }) => op === "fill" && fillStyle === FADE).length, 0);
  view.setFades(0.2, 1.5);
  redraw(view, canvas);
  assert.equal(canvas.ops.filter(({ op, fillStyle }) => op === "fill" && fillStyle === FADE).length, 1);
});

test("clear drops the clip's samples and takes every bar's element away", () => {
  const { view, bars } = makeView();
  view.setLoop({ start: START, end: START + 150 * PER_PX });
  view.setPlayed({ from: START, to: START + 10 });
  view.clear();
  assert.equal(view.channels, null);
  assert.equal(view.length, 0);
  assert.equal(view.peaks, null);
  assert.equal(view.loop, null);
  assert.equal(view.played, null);
  for (const bar of Object.values(bars)) assert.equal(bar.hidden, true);
  // A press with nothing loaded starts no drag.
  assert.equal(bars.start.fire("pointerdown", 7, 34).defaultPrevented, false);
  view.draw();
});
