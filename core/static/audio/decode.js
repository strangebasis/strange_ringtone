// Decoding the picked file in the browser (docs/PLAN.md 4): the server only
// ever sees the PCM WAV the page builds from the selection, so every codec the
// phone can play works without the core parsing it.

/**
 * The longest input accepted, in seconds. A 10-minute stereo clip at 44.1 kHz
 * is already about 210 MB of Float32 plus the decoder's own copy; an hour-long
 * file would crash a Safari tab instead of showing a message.
 */
export const MAX_INPUT_SECONDS = 600;

/**
 * The shortest input accepted, in seconds: the server refuses a selection
 * under half a second, so shorter input could never become a tone.
 */
export const MIN_INPUT_SECONDS = 0.5;

/**
 * Decode an audio file to at most two channels at sampleRate.
 *
 * Decoding uses an OfflineAudioContext, not a live AudioContext: no context
 * is created before a user gesture, and iOS's limit on live contexts is left
 * for playback. decodeAudioData resamples to the context's rate. Input with
 * more than two channels (or, defensively, a buffer that came back at another
 * rate) is rendered once more through an OfflineAudioContext of at most two
 * channels, which downmixes it with the standard speaker rules.
 *
 * Throws an Error whose message is meant for the user.
 * @param {Blob} file the picked file
 * @param {number} [sampleRate=44100] the rate to decode to
 * @returns {Promise<{channels: Float32Array[], sampleRate: number, length: number}>}
 */
export async function decodeFile(file, sampleRate = 44100) {
  const Offline = globalThis.OfflineAudioContext;
  if (typeof Offline !== "function") {
    throw new Error("This browser cannot decode audio. Try a recent Safari, Chrome or Firefox.");
  }
  const bytes = await file.arrayBuffer();
  let decoded;
  try {
    decoded = await new Offline(1, 1, sampleRate).decodeAudioData(bytes);
  } catch {
    throw new Error("This file could not be read as audio. Try another file, such as an MP3, M4A or WAV.");
  }
  if (!(decoded.duration <= MAX_INPUT_SECONDS)) {
    throw new Error(`This file is longer than ${MAX_INPUT_SECONDS / 60} minutes. Trim it first, or pick a shorter file.`);
  }
  if (!(decoded.duration >= MIN_INPUT_SECONDS)) {
    throw new Error("This file is shorter than half a second, which is too short for a ringtone.");
  }
  const count = Math.min(decoded.numberOfChannels, 2);
  let buffer = decoded;
  if (decoded.numberOfChannels > 2 || decoded.sampleRate !== sampleRate) {
    const length = Math.ceil(decoded.duration * sampleRate);
    const context = new Offline(count, length, sampleRate);
    const source = context.createBufferSource();
    source.buffer = decoded;
    source.connect(context.destination);
    source.start(0);
    buffer = await context.startRendering();
  }
  const channels = [];
  for (let c = 0; c < count; c += 1) {
    channels.push(buffer.getChannelData(c));
  }
  return { channels, sampleRate, length: buffer.length };
}
