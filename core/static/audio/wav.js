// The WAV the browser uploads for a render (docs/PLAN.md 5.2).
//
// The server's strict RIFF walk (core/strange_ringtone/wav.py) accepts exactly
// one shape and refuses everything else, so this encoder writes exactly that
// shape and nothing more:
//   RIFF <size = file length - 8> WAVE
//   "fmt " 16  tag 1 (PCM), channels 1|2, rate, byte rate, block align, 16 bits
//   "data" <frames * block align>  interleaved little-endian int16
// No LIST/fact chunk, no WAVE_FORMAT_EXTENSIBLE, no pad byte (the data size is
// always even, since a 16-bit frame is 2 or 4 bytes), nothing after data.
// Any other layout is a valid WAV that the server answers 400 bad_wav.

const HEADER_BYTES = 44;
const FMT_BYTES = 16;
const PCM = 1;
const BITS = 16;
// The rates wav.py accepts (ALLOWED_RATES); the web app always uses 44100.
const RATES = [44100, 48000];
// wav.py refuses a clip under half a second ('too_short').
export const MIN_SECONDS = 0.5;

function writeAscii(view, offset, text) {
  for (let i = 0; i < text.length; i += 1) {
    view.setUint8(offset + i, text.charCodeAt(i));
  }
}

function toInt16(sample) {
  // Scale by 32768 and clip, so an int16 sample decoded to float (x / 32768,
  // as Web Audio does) round-trips exactly. Round half away from zero, so the
  // result does not depend on the sign; NaN becomes silence.
  if (Number.isNaN(sample)) {
    return 0;
  }
  const scaled = Math.max(-1, Math.min(1, sample)) * 32768;
  const rounded = Math.sign(scaled) * Math.round(Math.abs(scaled));
  return Math.max(-32768, Math.min(32767, rounded));
}

/**
 * Encode frames [startFrame, endFrame) as a 16-bit PCM WAV of exactly the
 * layout the server's RIFF walk accepts (44-byte header: fmt then data).
 * Samples are clipped to [-1, 1].
 * @param {Float32Array[]} channels 1 or 2 channels
 * @param {number} sampleRate 44100 or 48000
 * @param {number} startFrame first frame, inclusive (integer)
 * @param {number} endFrame last frame, exclusive (integer)
 * @returns {Uint8Array} the whole file
 */
export function encodeWav(channels, sampleRate, startFrame, endFrame) {
  if (!Array.isArray(channels) || channels.length < 1 || channels.length > 2) {
    // wav.py refuses anything but mono or stereo ('bad_channels').
    throw new RangeError("a WAV for the server has 1 or 2 channels");
  }
  if (!RATES.includes(sampleRate)) {
    throw new RangeError("a WAV for the server is 44100 or 48000 Hz");
  }
  if (!Number.isInteger(startFrame) || !Number.isInteger(endFrame) || startFrame < 0 || endFrame <= startFrame) {
    throw new RangeError("the frame range is empty or not whole frames");
  }
  for (const channel of channels) {
    if (!(channel instanceof Float32Array) || channel.length < endFrame) {
      throw new RangeError("the frame range runs past the audio");
    }
  }
  const count = channels.length;
  const blockAlign = count * (BITS / 8);
  const frames = endFrame - startFrame;
  const dataBytes = frames * blockAlign;
  // The RIFF size is a uint32. A 40 s clip is about 7 MB, so this only stops
  // a caller bug from writing a header that wraps around.
  if (HEADER_BYTES - 8 + dataBytes > 0xffffffff) {
    throw new RangeError("the selection is too long for a WAV");
  }
  const bytes = new Uint8Array(HEADER_BYTES + dataBytes);
  const view = new DataView(bytes.buffer);
  writeAscii(view, 0, "RIFF");
  view.setUint32(4, HEADER_BYTES - 8 + dataBytes, true);
  writeAscii(view, 8, "WAVE");
  writeAscii(view, 12, "fmt ");
  view.setUint32(16, FMT_BYTES, true);
  view.setUint16(20, PCM, true);
  view.setUint16(22, count, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * blockAlign, true);
  view.setUint16(32, blockAlign, true);
  view.setUint16(34, BITS, true);
  writeAscii(view, 36, "data");
  view.setUint32(40, dataBytes, true);
  let offset = HEADER_BYTES;
  for (let frame = startFrame; frame < endFrame; frame += 1) {
    for (let c = 0; c < count; c += 1) {
      view.setInt16(offset, toInt16(channels[c][frame]), true);
      offset += 2;
    }
  }
  return bytes;
}

/**
 * Make a selection the server will accept: whole frames inside [0, length],
 * at least minFrames and at most maxFrames long. The server has no tolerance
 * (one frame over MAX_CLIP_SECONDS * rate is 'too_long', under half a second
 * is 'too_short'), so the selection is clamped here, before it is encoded.
 *
 * When the span must change, `keep` names the edge that stays put and the
 * other edge moves; if that would leave [0, length], the window slides back
 * inside keeping its length. Dragging the start past the cap therefore pulls
 * the end along (keep 'start'), and dragging the end pulls the start
 * (keep 'end').
 * @param {number} startFrame
 * @param {number} endFrame
 * @param {number} length frames in the audio; must be at least minFrames
 * @param {number} maxFrames MAX_CLIP_SECONDS * sampleRate
 * @param {number} [minFrames=22050] half a second at 44100 Hz
 * @param {"start"|"end"} [keep="start"] the edge that stays when the span changes
 * @returns {[number, number]} [start, end] in whole frames
 */
export function clampSelection(startFrame, endFrame, length, maxFrames, minFrames = 22050, keep = "start") {
  for (const value of [startFrame, endFrame]) {
    if (!Number.isFinite(value)) {
      throw new RangeError("selection edges must be finite");
    }
  }
  if (!Number.isInteger(length) || !Number.isInteger(maxFrames) || !Number.isInteger(minFrames)) {
    throw new RangeError("length and limits are whole frames");
  }
  if (minFrames < 1 || maxFrames < minFrames) {
    throw new RangeError("the limits are inconsistent");
  }
  if (length < minFrames) {
    // No selection of this audio can be rendered. decode.js refuses such
    // input first, so reaching this is a caller bug.
    throw new RangeError("the audio is shorter than the minimum selection");
  }
  let start = Math.min(Math.max(Math.round(Math.min(startFrame, endFrame)), 0), length);
  let end = Math.min(Math.max(Math.round(Math.max(startFrame, endFrame)), 0), length);
  const span = Math.min(Math.max(end - start, minFrames), maxFrames, length);
  if (keep === "end") {
    start = end - span;
    if (start < 0) {
      start = 0;
      end = span;
    }
  } else {
    end = start + span;
    if (end > length) {
      end = length;
      start = length - span;
    }
  }
  return [start, end];
}
