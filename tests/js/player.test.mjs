// Looped playback (core/static/player.js) against a fake AudioContext that
// records every node, connection, AudioParam call and start. The preview
// loops the selection with the fades and the preview gain on two separate
// GainNodes; the rendered file plays as it is, because it already has both.
import { test } from "node:test";
import assert from "node:assert/strict";

import { fadeGainAt, stretchCurve } from "../../core/static/audio/fades.js";
import { dbToGain, Player } from "../../core/static/player.js";

class FakeParam {
  constructor() {
    this.value = 1;
    this.calls = [];
  }

  setValueAtTime(value, time) { this.calls.push({ method: "setValueAtTime", value, time }); }
  setTargetAtTime(value, time, constant) { this.calls.push({ method: "setTargetAtTime", value, time, constant }); }
  setValueCurveAtTime(curve, time, duration) {
    this.calls.push({ method: "setValueCurveAtTime", curve, time, duration });
  }
  cancelScheduledValues(time) { this.calls.push({ method: "cancelScheduledValues", time }); }

  named(method) {
    return this.calls.filter((call) => call.method === method);
  }

  last() {
    return this.calls[this.calls.length - 1];
  }
}

class FakeNode {
  constructor(kind) {
    this.kind = kind;
    this.connections = [];
  }

  connect(node) { this.connections.push(node); }
  disconnect() { this.connections = []; }
}

class FakeSource extends FakeNode {
  constructor() {
    super("source");
    this.buffer = null;
    this.loop = false;
    this.starts = [];
    this.stops = 0;
  }

  start(...args) { this.starts.push(args); }
  stop() { this.stops += 1; }
  addEventListener() {}
}

class FakeContext {
  constructor() {
    this.currentTime = 0;
    this.destination = new FakeNode("destination");
    this.gains = [];
    this.sources = [];
  }

  createGain() {
    const node = new FakeNode("gain");
    node.gain = new FakeParam();
    this.gains.push(node);
    return node;
  }

  createBufferSource() {
    const source = new FakeSource();
    this.sources.push(source);
    return source;
  }
}

const BUFFER = { duration: 10 };

// A player on a fresh context; the test body runs, and the player is always
// stopped afterwards so its pump timer does not keep node running.
function withPlayer(body) {
  const context = new FakeContext();
  const player = new Player(context);
  const [fadeGain, levelGain] = context.gains;
  try {
    body({ context, player, fadeGain, levelGain });
  } finally {
    player.stop();
  }
}

test("the chain is source, fade gain, loudness gain, speakers", () => {
  withPlayer(({ context, player, fadeGain, levelGain }) => {
    assert.equal(context.gains.length, 2);
    assert.deepEqual(fadeGain.connections, [levelGain]);
    assert.deepEqual(levelGain.connections, [context.destination]);
    player.play(BUFFER, 2, 3, { fadeIn: 0.2, fadeOut: 0.5, gainDb: 0 });
    assert.ok(context.sources.length > 0);
    for (const source of context.sources) assert.deepEqual(source.connections, [fadeGain]);
  });
});

test("each preview iteration plays exactly the selection with its own fade curve", () => {
  withPlayer(({ context, player, fadeGain }) => {
    player.play(BUFFER, 2, 3, { fadeIn: 0.2, fadeOut: 0.5, gainDb: 0 });
    // One iteration fits in the 0.4 s lookahead of a 1 s selection.
    assert.equal(context.sources.length, 1);
    const [first] = context.sources;
    assert.equal(first.buffer, BUFFER);
    assert.equal(first.loop, false, "the loop is one source per pass, so each pass gets its fades");
    assert.deepEqual(first.starts, [[0.05, 2, 1]]);
    const [curve] = fadeGain.gain.named("setValueCurveAtTime");
    assert.equal(curve.time, 0.05);
    assert.equal(curve.duration, 1);
    assert.ok(curve.curve.length >= 200);
    assert.equal(curve.curve[0], fadeGainAt(0, 1, 0.2, 0.5));
    assert.equal(curve.curve[curve.curve.length - 1], fadeGainAt(1, 1, 0.2, 0.5));
  });
});

test("the next iteration is queued within the lookahead and starts where the last one ends", () => {
  withPlayer(({ context, player, fadeGain }) => {
    player.play(BUFFER, 2, 3, { fadeIn: 0.2, fadeOut: 0.5, gainDb: 0 });
    // 0.65 s before the next start: outside the 0.4 s lookahead.
    context.currentTime = 0.4;
    player.pump();
    assert.equal(context.sources.length, 1);
    // 0.35 s before it: inside.
    context.currentTime = 0.7;
    player.pump();
    assert.equal(context.sources.length, 2);
    const second = context.sources[1];
    // No gap and no overlap: the same sum the AudioParam uses for the curve's end.
    assert.deepEqual(second.starts, [[0.05 + 1, 2, 1]]);
    const curves = fadeGain.gain.named("setValueCurveAtTime");
    assert.equal(curves.length, 2);
    assert.equal(curves[1].time, curves[0].time + curves[0].duration);
  });
});

test("the preview gain is set at the start and then moved live with setTargetAtTime", () => {
  withPlayer(({ context, player, levelGain }) => {
    player.play(BUFFER, 2, 3, { fadeIn: 0, fadeOut: 0, gainDb: -6 });
    assert.deepEqual(levelGain.gain.last(), { method: "setValueAtTime", value: dbToGain(-6), time: 0 });
    context.currentTime = 0.3;
    player.update({ gainDb: 4 });
    const moved = levelGain.gain.last();
    // A short glide rather than a jump, which would click.
    assert.equal(moved.method, "setTargetAtTime");
    assert.equal(moved.value, dbToGain(4));
    assert.equal(moved.time, 0.3);
    assert.ok(moved.constant > 0);
  });
});

test("new fades apply from the next iteration, not to the one already playing", () => {
  withPlayer(({ context, player, fadeGain }) => {
    player.play(BUFFER, 2, 3, { fadeIn: 0, fadeOut: 0, gainDb: 0 });
    player.update({ fadeIn: 0.5 });
    assert.equal(fadeGain.gain.named("setValueCurveAtTime").length, 1);
    context.currentTime = 0.7;
    player.pump();
    const curves = fadeGain.gain.named("setValueCurveAtTime");
    assert.equal(curves.length, 2);
    assert.equal(curves[1].curve[0], 0, "the new fade-in starts from silence");
  });
});

test("the rendered file loops as it is, with no fades and no gain", () => {
  withPlayer(({ context, player, fadeGain, levelGain }) => {
    // A preview with a gain and fades first, so there is something to undo.
    player.play(BUFFER, 2, 3, { fadeIn: 0.2, fadeOut: 0.5, gainDb: -6 });
    const curvesBefore = fadeGain.gain.named("setValueCurveAtTime").length;
    context.currentTime = 1;
    const rendered = { duration: 15 };
    player.playPlain(rendered);
    const source = context.sources[context.sources.length - 1];
    assert.equal(source.buffer, rendered);
    assert.equal(source.loop, true);
    // The whole file from its start: no offset, no duration.
    assert.deepEqual(source.starts, [[1.05]]);
    assert.deepEqual(source.connections, [fadeGain]);
    assert.equal(fadeGain.gain.named("setValueCurveAtTime").length, curvesBefore, "the file already has its fades");
    assert.deepEqual(fadeGain.gain.last(), { method: "setValueAtTime", value: 1, time: 1 });
    assert.deepEqual(levelGain.gain.last(), { method: "setValueAtTime", value: 1, time: 1 });
    // The loudness slider leaves it alone too.
    player.update({ gainDb: 3 });
    assert.deepEqual(levelGain.gain.last(), { method: "setValueAtTime", value: 1, time: 1 });
    // And no iterations are pumped on top of it.
    context.currentTime = 1.5;
    player.pump();
    assert.equal(context.sources[context.sources.length - 1], source);
  });
});

test("stop ends every source and stops pumping", () => {
  withPlayer(({ context, player }) => {
    player.play(BUFFER, 2, 3, { fadeIn: 0, fadeOut: 0, gainDb: 0 });
    player.stop();
    assert.equal(player.playing, false);
    assert.ok(context.sources.every((source) => source.stops === 1));
    context.currentTime = 0.9;
    player.pump();
    assert.equal(context.sources.length, 1);
  });
});

// --- a part of the selection (the loop controls) and the playheads -----------------

function near(actual, expected, label) {
  assert.ok(Math.abs(actual - expected) <= 1e-9, `${label}: ${actual} != ${expected}`);
}

test("a part of the selection loops with the selection's fades, not fades of its own", () => {
  withPlayer(({ context, player, fadeGain }) => {
    // The buffer's 2 s to 6 s is the selection; its first second loops.
    player.play(BUFFER, 2, 3, { fadeIn: 0.5, fadeOut: 1, gainDb: 0 }, { start: 2, end: 6 });
    const [first] = context.sources;
    // Only the part is played.
    assert.deepEqual(first.starts, [[0.05, 2, 1]]);
    const [curve] = fadeGain.gain.named("setValueCurveAtTime");
    assert.equal(curve.time, 0.05);
    assert.equal(curve.duration, 1);
    assert.deepEqual(curve.curve, stretchCurve(0, 1, 4, 0.5, 1));
    // The selection's fade-in is heard, and after it the level is full: the
    // selection's fade-out is three seconds away, so nothing fades towards
    // the part's end but the few milliseconds at the cut.
    assert.equal(curve.curve[0], 0);
    assert.equal(curve.curve[Math.round(curve.curve.length / 2)], 1);
    assert.equal(curve.curve[curve.curve.length - 3], 1);
    assert.equal(curve.curve[curve.curve.length - 1], 0);
  });
});

test("a part at the selection's end is heard with the selection's fade-out", () => {
  withPlayer(({ context, player, fadeGain }) => {
    player.play(BUFFER, 5, 6, { fadeIn: 0.5, fadeOut: 2, gainDb: 0 }, { start: 2, end: 6 });
    assert.deepEqual(context.sources[0].starts, [[0.05, 5, 1]]);
    const [curve] = fadeGain.gain.named("setValueCurveAtTime");
    assert.deepEqual(curve.curve, stretchCurve(3, 1, 4, 0.5, 2));
    // Halfway through the two-second fade-out where the part begins (after
    // the cut's own few milliseconds), and silent at the end.
    near(curve.curve[2], Math.fround(fadeGainAt(3 + 2 / (curve.curve.length - 1), 4, 0.5, 2)), "third point");
    assert.ok(curve.curve[2] < 0.72 && curve.curve[2] > 0.69);
    assert.equal(curve.curve[curve.curve.length - 1], 0);
  });
});

test("every pass of a part gets the same curve, each where the last one ends", () => {
  withPlayer(({ context, player, fadeGain }) => {
    player.play(BUFFER, 3, 4, { fadeIn: 0.5, fadeOut: 1, gainDb: 0 }, { start: 2, end: 6 });
    context.currentTime = 0.7;
    player.pump();
    assert.deepEqual(context.sources.map((source) => source.starts[0]), [[0.05, 3, 1], [1.05, 3, 1]]);
    const curves = fadeGain.gain.named("setValueCurveAtTime");
    assert.equal(curves.length, 2);
    assert.deepEqual(curves[1].curve, stretchCurve(1, 1, 4, 0.5, 1));
    assert.equal(curves[1].time, curves[0].time + curves[0].duration);
  });
});

test("progress says how far the pass being heard has got", () => {
  withPlayer(({ context, player }) => {
    assert.equal(player.progress(), null, "nothing plays");
    player.play(BUFFER, 2, 3, { fadeIn: 0, fadeOut: 0, gainDb: 0 });
    // The first pass starts 50 ms from now.
    assert.deepEqual(player.progress(), { position: 0, duration: 1 });
    context.currentTime = 0.55;
    near(player.progress().position, 0.5, "half the first pass");
    // The third pass: every pass starts a whole number of lengths after the first.
    context.currentTime = 2.3;
    near(player.progress().position, 0.25, "a quarter of the third pass");
    assert.equal(player.progress().duration, 1);
    player.stop();
    assert.equal(player.progress(), null, "stopped");
  });
});

test("the output latency is taken off, so the playhead is where the sound is", () => {
  withPlayer(({ context, player }) => {
    player.play(BUFFER, 2, 3, { fadeIn: 0, fadeOut: 0, gainDb: 0 });
    // What the clock has reached leaves the speakers 0.2 s later.
    context.outputLatency = 0.2;
    context.currentTime = 0.75;
    near(player.progress().position, 0.5, "with latency");
    // Nothing is heard yet: not a position before the pass's start.
    context.currentTime = 0.1;
    assert.equal(player.progress().position, 0);
    // A browser that reports none, or nonsense, changes nothing.
    for (const latency of [undefined, Number.NaN, -1, 0]) {
      context.outputLatency = latency;
      context.currentTime = 0.55;
      near(player.progress().position, 0.5, String(latency));
    }
  });
});

test("after a gap the passes are counted from the restart", () => {
  withPlayer(({ context, player }) => {
    player.play(BUFFER, 2, 3, { fadeIn: 0, fadeOut: 0, gainDb: 0 });
    // The page was throttled for five seconds and the queue ran dry: the
    // loop starts afresh 50 ms from now.
    // Not a whole number of passes after the first start, or counting from
    // the first start would give the same answer.
    context.currentTime = 5.4;
    player.pump();
    assert.deepEqual(context.sources[context.sources.length - 1].starts, [[5.4 + 0.05, 2, 1]]);
    context.currentTime = 5.7;
    near(player.progress().position, 0.25, "counted from the fresh start");
  });
});

test("the finished tone's progress runs over the whole file", () => {
  withPlayer(({ context, player }) => {
    context.currentTime = 1;
    player.playPlain({ duration: 15 });
    assert.deepEqual(player.progress(), { position: 0, duration: 15 });
    context.currentTime = 1.05 + 6;
    near(player.progress().position, 6, "six seconds in");
    // The file loops by itself (source.loop), so the second pass starts 15 s on.
    context.currentTime = 1.05 + 15 + 3;
    near(player.progress().position, 3, "the second pass");
    assert.equal(player.progress().duration, 15);
  });
});
