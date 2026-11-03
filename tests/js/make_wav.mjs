// Writes a WAV with the web app's own encoder, so tests/test_frontend_js.py
// can check that wav.py accepts exactly what the browser uploads.
// Usage: node tests/js/make_wav.mjs <out_path> <frames> <channels>
import { writeFileSync } from "node:fs";

import { encodeWav } from "../../core/static/audio/wav.js";

const RATE = 44100;
const [outPath, framesText, channelsText] = process.argv.slice(2);
const frames = Number(framesText);
const count = Number(channelsText);
if (!outPath || !Number.isInteger(frames) || frames < 1 || !Number.isInteger(count)) {
  process.stderr.write("usage: make_wav.mjs <out_path> <frames> <channels>\n");
  process.exit(2);
}

// A deterministic signal: a 440 Hz sine per channel, a different phase each,
// at about -6 dBFS, so the samples are not all zero.
const channels = [];
for (let c = 0; c < count; c += 1) {
  const channel = new Float32Array(frames);
  for (let i = 0; i < frames; i += 1) {
    channel[i] = 0.5 * Math.sin((2 * Math.PI * 440 * i) / RATE + c);
  }
  channels.push(channel);
}
writeFileSync(outPath, encodeWav(channels, RATE, 0, frames));
