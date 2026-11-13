// The loop controls' rules (core/static/loop.js, docs/PLAN.md 7.1): where the
// two loop bars may be, and what they do when a trim bar moves.
import { test } from "node:test";
import assert from "node:assert/strict";

import { defaultLoop, followSelection, MIN_LOOP_SECONDS, moveLoopEdge } from "../../core/static/loop.js";

const MIN = 10;
const SELECTION = Object.freeze({ start: 100, end: 300 });

const follow = (loop, after, how = "edge", before = SELECTION) => followSelection(loop, before, after, how, MIN);
const moveBar = (loop, edge, frame, selection = SELECTION) => moveLoopEdge(loop, edge, frame, selection, MIN);

test("the shortest loop is half the shortest clip, so the default always fits", () => {
  // The server refuses a clip under 0.5 s; the default loop is half the
  // selection, so it must be allowed to be half of that.
  assert.equal(MIN_LOOP_SECONDS, 0.25);
});

test("the default loop runs from the selection's start to its middle", () => {
  assert.deepEqual(defaultLoop(SELECTION, MIN), { start: 100, end: 200 });
  // An odd length: whole frames.
  assert.deepEqual(defaultLoop({ start: 0, end: 25 }, MIN), { start: 0, end: 13 });
  // The shortest selection: its half is exactly the shortest loop.
  assert.deepEqual(defaultLoop({ start: 50, end: 70 }, MIN), { start: 50, end: 60 });
  // Never shorter than the minimum, and never past the selection's end.
  assert.deepEqual(defaultLoop({ start: 50, end: 62 }, MIN), { start: 50, end: 60 });
});

test("a loop bar on a trim bar travels with it, in both directions", () => {
  const atStart = { start: 100, end: 180 };
  // Earlier: without this the loop would no longer begin at the start, and
  // the new start would not be heard.
  assert.deepEqual(follow(atStart, { start: 60, end: 300 }), { start: 60, end: 180 });
  assert.deepEqual(follow(atStart, { start: 130, end: 300 }), { start: 130, end: 180 });
  const atEnd = { start: 220, end: 300 };
  assert.deepEqual(follow(atEnd, { start: 100, end: 340 }), { start: 220, end: 340 });
  assert.deepEqual(follow(atEnd, { start: 100, end: 260 }), { start: 220, end: 260 });
  // A loop over the whole selection follows both bars.
  assert.deepEqual(follow({ start: 100, end: 300 }, { start: 80, end: 320 }), { start: 80, end: 320 });
});

test("a loop bar anywhere else keeps its place in the audio", () => {
  const middle = { start: 150, end: 250 };
  assert.deepEqual(follow(middle, { start: 60, end: 300 }), middle);
  assert.deepEqual(follow(middle, { start: 140, end: 300 }), middle);
  assert.deepEqual(follow(middle, { start: 100, end: 260 }), middle);
  assert.deepEqual(follow(middle, { start: 100, end: 400 }), middle);
  // One frame off the trim bar is not on it.
  assert.deepEqual(follow({ start: 101, end: 180 }, { start: 60, end: 300 }), { start: 101, end: 180 });
  assert.deepEqual(follow({ start: 220, end: 299 }, { start: 100, end: 340 }), { start: 220, end: 299 });
});

test("a trim bar that reaches a loop bar pushes it", () => {
  const middle = { start: 150, end: 250 };
  assert.deepEqual(follow(middle, { start: 170, end: 300 }), { start: 170, end: 250 });
  assert.deepEqual(follow(middle, { start: 100, end: 230 }), { start: 150, end: 230 });
});

test("a pushed loop keeps the shortest length, inside the selection", () => {
  const middle = { start: 150, end: 250 };
  // The start bar passes the loop's end: the whole loop is pushed along.
  assert.deepEqual(follow(middle, { start: 260, end: 300 }), { start: 260, end: 270 });
  // The end bar passes the loop's start.
  assert.deepEqual(follow(middle, { start: 100, end: 140 }), { start: 130, end: 140 });
  // A loop on the start, squeezed by the start bar moving in.
  assert.deepEqual(follow({ start: 100, end: 115 }, { start: 112, end: 300 }), { start: 112, end: 122 });
  // Against the selection's end there is still room for the shortest loop.
  assert.deepEqual(follow({ start: 280, end: 300 }, { start: 295, end: 305 }, "edge"), { start: 295, end: 305 });
});

test("sliding the whole selection carries the loop along", () => {
  const middle = { start: 150, end: 250 };
  assert.deepEqual(follow(middle, { start: 400, end: 600 }, "move"), { start: 450, end: 550 });
  assert.deepEqual(follow(middle, { start: 40, end: 240 }, "move"), { start: 90, end: 190 });
});

test("an edge that pulled the other one along is not a slide", () => {
  // At the cap, dragging the start earlier pulls the end with it: both edges
  // move by the same amount. The numbers alone look like a slide, so the
  // gesture is what decides. A loop in the middle stays where it is.
  const middle = { start: 150, end: 250 };
  assert.deepEqual(follow(middle, { start: 60, end: 260 }, "edge"), middle);
  assert.deepEqual(follow(middle, { start: 60, end: 260 }, "move"), { start: 110, end: 210 });
});

test("the selection and the loop passed in are not modified", () => {
  const loop = Object.freeze({ start: 150, end: 250 });
  follow(loop, { start: 170, end: 300 });
  moveBar(loop, "start", 200);
  assert.deepEqual(loop, { start: 150, end: 250 });
  assert.deepEqual(SELECTION, { start: 100, end: 300 });
});

test("a loop bar cannot be dragged past its trim bar, but it can rest on it", () => {
  const loop = { start: 150, end: 250 };
  assert.deepEqual(moveBar(loop, "start", 100), { start: 100, end: 250 });
  assert.deepEqual(moveBar(loop, "start", 20), { start: 100, end: 250 });
  assert.deepEqual(moveBar(loop, "end", 300), { start: 150, end: 300 });
  assert.deepEqual(moveBar(loop, "end", 900), { start: 150, end: 300 });
  // Both: the whole selection is a valid loop.
  assert.deepEqual(moveBar(moveBar(loop, "start", 0), "end", 900), { start: 100, end: 300 });
});

test("a loop bar stops at the shortest loop and never pushes the other bar", () => {
  const loop = { start: 150, end: 250 };
  assert.deepEqual(moveBar(loop, "start", 245), { start: 240, end: 250 });
  assert.deepEqual(moveBar(loop, "start", 400), { start: 240, end: 250 });
  assert.deepEqual(moveBar(loop, "end", 155), { start: 150, end: 160 });
  assert.deepEqual(moveBar(loop, "end", 0), { start: 150, end: 160 });
});

test("frames are whole, and an unknown bar changes nothing", () => {
  const loop = { start: 150, end: 250 };
  assert.deepEqual(moveBar(loop, "start", 160.6), { start: 161, end: 250 });
  assert.deepEqual(moveBar(loop, "middle", 5), loop);
});
