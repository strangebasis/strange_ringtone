// decodeFile's limits and channel handling (core/static/audio/decode.js), with
// a stand-in OfflineAudioContext: node has no Web Audio.
import { afterEach, test } from "node:test";
import assert from "node:assert/strict";

import { MAX_INPUT_SECONDS, MIN_INPUT_SECONDS, decodeFile } from "../../core/static/audio/decode.js";
// The byte cap through the namespace: a named import of a missing export would
// stop this whole file from loading instead of failing the tests that need it.
import * as decodeModule from "../../core/static/audio/decode.js";

const MAX_INPUT_BYTES = decodeModule.MAX_INPUT_BYTES;
const MIB = 1024 * 1024;

function fakeBuffer(channels, length, sampleRate) {
  const data = Array.from({ length: channels }, (_, c) => new Float32Array(length).fill(c + 1));
  return {
    numberOfChannels: channels,
    length,
    sampleRate,
    duration: length / sampleRate,
    getChannelData: (c) => data[c],
  };
}

const contexts = [];
let decoded = null;

class FakeOffline {
  constructor(channels, length, sampleRate) {
    this.args = [channels, length, sampleRate];
    this.rendered = false;
    contexts.push(this);
  }

  decodeAudioData() {
    return decoded === null ? Promise.reject(new Error("EncodingError")) : Promise.resolve(decoded);
  }

  createBufferSource() {
    return { buffer: null, connect() {}, start() {} };
  }

  startRendering() {
    this.rendered = true;
    const [channels, length, sampleRate] = this.args;
    return Promise.resolve(fakeBuffer(channels, length, sampleRate));
  }
}

// A Blob always has a size; decodeFile looks at it before reading anything.
const file = { size: 8, arrayBuffer: async () => new ArrayBuffer(8) };

// A file of the given size that records every read of its bytes.
function sizedFile(size) {
  const reads = [];
  return {
    size,
    reads,
    arrayBuffer: async () => {
      reads.push(size);
      return new ArrayBuffer(8);
    },
  };
}

function install(buffer) {
  decoded = buffer;
  contexts.length = 0;
  globalThis.OfflineAudioContext = FakeOffline;
}

afterEach(() => {
  delete globalThis.OfflineAudioContext;
});

test("the limits are 10 minutes and half a second", () => {
  assert.equal(MAX_INPUT_SECONDS, 600);
  assert.equal(MIN_INPUT_SECONDS, 0.5);
});

test("stereo at 44100 Hz is returned as decoded, with no second pass", async () => {
  install(fakeBuffer(2, 44100 * 3, 44100));
  const result = await decodeFile(file);
  assert.equal(result.sampleRate, 44100);
  assert.equal(result.length, 44100 * 3);
  assert.equal(result.channels.length, 2);
  assert.ok(result.channels.every((c) => c instanceof Float32Array));
  // Decoding uses an offline context at the target rate, never a live one.
  assert.deepEqual(contexts.map((c) => c.args), [[1, 1, 44100]]);
});

test("more than two channels are rendered down to stereo", async () => {
  install(fakeBuffer(6, 44100 * 2, 44100));
  const result = await decodeFile(file);
  assert.equal(result.channels.length, 2);
  assert.deepEqual(contexts[1].args, [2, 44100 * 2, 44100]);
  assert.ok(contexts[1].rendered);
});

test("a buffer at another rate is resampled to the target", async () => {
  install(fakeBuffer(1, 48000 * 2, 48000));
  const result = await decodeFile(file, 44100);
  assert.equal(result.channels.length, 1);
  assert.equal(result.length, 44100 * 2);
  assert.deepEqual(contexts[1].args, [1, 44100 * 2, 44100]);
});

test("a resampled length that falls between frames rounds up, keeping the last frame", async () => {
  // 48007 frames at 48 kHz is 44106.43 frames at 44.1 kHz: rounding down
  // (or to nearest) would cut the decoded audio's final partial frame.
  install(fakeBuffer(1, 48007, 48000));
  const result = await decodeFile(file, 44100);
  assert.deepEqual(contexts[1].args, [1, 44107, 44100]);
  assert.equal(result.length, 44107);
  assert.equal(result.channels[0].length, 44107);
});

test("input longer than MAX_INPUT_SECONDS is refused with a message", async () => {
  install(fakeBuffer(1, 44100 * MAX_INPUT_SECONDS + 1, 44100));
  await assert.rejects(decodeFile(file), /longer than 10 minutes/);
  assert.equal(contexts.length, 1);
  install(fakeBuffer(1, 44100 * MAX_INPUT_SECONDS, 44100));
  assert.equal((await decodeFile(file)).length, 44100 * MAX_INPUT_SECONDS);
});

test("input shorter than half a second is refused with a message", async () => {
  install(fakeBuffer(1, 22049, 44100));
  await assert.rejects(decodeFile(file), /shorter than half a second/);
  install(fakeBuffer(1, 0, 44100));
  await assert.rejects(decodeFile(file), /shorter than half a second/);
});

test("an undecodable file and a missing Web Audio give plain messages", async () => {
  install(null);
  await assert.rejects(decodeFile(file), /could not be read as audio/);
  delete globalThis.OfflineAudioContext;
  await assert.rejects(decodeFile(file), /cannot decode audio/);
});

test("the byte cap is 64 MiB", () => {
  assert.equal(MAX_INPUT_BYTES, 64 * MIB, "decode.js exports MAX_INPUT_BYTES, 64 MiB");
});

test("a file one byte over the byte cap is refused before any of it is read", async () => {
  install(fakeBuffer(2, 44100 * 3, 44100));
  const big = sizedFile(64 * MIB + 1);
  await assert.rejects(decodeFile(big), (error) => {
    // app.js shows the words of a plain Error only.
    assert.equal(error.constructor, Error);
    assert.match(error.message, /64 MB/);
    // What to do about it: trim it, or save it smaller, then pick it again.
    assert.match(error.message, /trim/i);
    assert.match(error.message, /MP3/);
    assert.match(error.message, /M4A/);
    return true;
  });
  assert.deepEqual(big.reads, [], "the oversized file was read");
  assert.deepEqual(contexts, [], "a decoder was made for the oversized file");
});

test("a file exactly at the byte cap goes on to the decoder", async () => {
  assert.equal(MAX_INPUT_BYTES, 64 * MIB, "decode.js exports MAX_INPUT_BYTES, 64 MiB");
  install(fakeBuffer(2, 44100 * 3, 44100));
  const atCap = sizedFile(MAX_INPUT_BYTES);
  const result = await decodeFile(atCap);
  assert.equal(result.length, 44100 * 3);
  assert.deepEqual(atCap.reads, [MAX_INPUT_BYTES]);
  assert.equal(contexts.length, 1);
});

test("a file whose size is not a number is refused unread", async () => {
  // The comparison fails closed: something that is not a Blob does not slip
  // past the cap by having no size.
  for (const size of [undefined, Number.NaN]) {
    install(fakeBuffer(2, 44100 * 3, 44100));
    const odd = sizedFile(size);
    await assert.rejects(decodeFile(odd), Error, String(size));
    assert.deepEqual(odd.reads, [], String(size));
  }
});
