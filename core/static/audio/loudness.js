// A small ITU-R BS.1770-4 loudness meter for the preview (docs/PLAN.md 5.4).
//
// The server's ffmpeg ebur128 measurement is the authority; this meter only
// has to agree with it closely enough that the Softer-Louder slider previews
// the gain the server will apply. Pure functions on Float32Array channels: no
// DOM, no Web Audio, so node --test runs them unchanged.

// The K-weighting filter's analog prototype, as derived in libebur128. The
// standard publishes biquad coefficients for 48 kHz only; deriving them from
// the prototype gives the same 48 kHz set (pinned in tests/js) and a correct
// set for 44100 Hz, which is the rate the web app decodes to.
//
// Stage 1, the "pre-filter": a high shelf of about +4 dB above ~1.7 kHz that
// models the acoustic effect of the head.
const SHELF_F0 = 1681.974450955533;
const SHELF_GAIN_DB = 3.999843853973347;
const SHELF_Q = 0.7071752369554196;
// The shelf's band-edge gain is Vh raised to this power (libebur128's fit of
// the published 48 kHz coefficients).
const SHELF_VB_EXPONENT = 0.4996667741545416;
// Stage 2, the "RLB" curve: a second-order high-pass near 38 Hz, so rumble and
// DC count for (almost) nothing.
const HIGHPASS_F0 = 38.13547087602444;
const HIGHPASS_Q = 0.5003270373238773;

// BS.1770's offset, chosen so that a 997 Hz sine at 0 dBFS in one front
// channel reads -3.01 LKFS after K-weighting.
const OFFSET = -0.691;
// Gating (BS.1770-4 section 2): 400 ms blocks overlapping by 75 %, so a new
// block starts every 100 ms; blocks at or below -70 LUFS are silence; then
// blocks more than 10 LU below the mean of the rest are dropped.
const BLOCK_SECONDS = 0.4;
const STEP_SECONDS = 0.1;
const ABSOLUTE_GATE = -70;
const RELATIVE_GATE = -10;
// EBU Tech 3341's short-term loudness uses a 3 s window.
const SHORT_TERM_SECONDS = 3;
// Energy is summed per 10 ms granule. Every window the meter and the hook
// suggestion use (400 ms blocks, 100 ms steps, 3 s windows, 50 ms RMS) is a
// whole number of granules, so a prefix sum over granules answers each window
// in O(1) and costs one Float64 per 10 ms instead of one per sample: 600 s of
// input is 60 000 entries, not 26 million.
const GRANULES_PER_SECOND = 100;

/**
 * Biquad coefficients of the two K-weighting stages for a sample rate.
 * Exported so a test pins the 48 kHz set against the standard's table.
 * @param {number} sampleRate in Hz
 * @returns {{shelf: {b: number[], a: number[]}, highpass: {b: number[], a: number[]}}}
 *   a holds [a1, a2]; a0 is normalised to 1.
 */
export function kWeightingCoefficients(sampleRate) {
  if (!Number.isFinite(sampleRate) || sampleRate <= 2 * SHELF_F0) {
    throw new RangeError("sample rate out of range");
  }
  let k = Math.tan((Math.PI * SHELF_F0) / sampleRate);
  const vh = 10 ** (SHELF_GAIN_DB / 20);
  const vb = vh ** SHELF_VB_EXPONENT;
  let a0 = 1 + k / SHELF_Q + k * k;
  const shelf = {
    b: [(vh + (vb * k) / SHELF_Q + k * k) / a0, (2 * (k * k - vh)) / a0, (vh - (vb * k) / SHELF_Q + k * k) / a0],
    a: [(2 * (k * k - 1)) / a0, (1 - k / SHELF_Q + k * k) / a0],
  };
  k = Math.tan((Math.PI * HIGHPASS_F0) / sampleRate);
  a0 = 1 + k / HIGHPASS_Q + k * k;
  // The standard's high-pass numerator is exactly 1, -2, 1 (not normalised
  // to unity pass-band gain), and libebur128 and ffmpeg use it as published.
  const highpass = {
    b: [1, -2, 1],
    a: [(2 * (k * k - 1)) / a0, (1 - k / HIGHPASS_Q + k * k) / a0],
  };
  return { shelf, highpass };
}

function checkChannels(channels) {
  // The web app hands over at most two channels (decode.js downmixes), and
  // BS.1770 weights left and right 1.0. Surround weights are not needed, so
  // anything else is a caller bug rather than something to guess about.
  if (!Array.isArray(channels) || channels.length < 1 || channels.length > 2) {
    throw new RangeError("expected 1 or 2 channels");
  }
  const length = channels[0].length;
  for (const channel of channels) {
    if (!(channel instanceof Float32Array) || channel.length !== length) {
      throw new TypeError("channels must be Float32Arrays of equal length");
    }
  }
  return length;
}

/**
 * K-weighted energy summed over channels per 10 ms granule, as a prefix sum.
 * prefix[i] is the energy of granules 0..i-1, so the energy of granules
 * [a, b) is prefix[b] - prefix[a]. A trailing partial granule is dropped.
 * @param {Float32Array[]} channels 1 or 2 channels of equal length
 * @param {number} sampleRate in Hz
 * @returns {{prefix: Float64Array, granule: number, count: number}}
 *   granule is the granule length in samples, count the number of granules.
 */
export function energyPrefix(channels, sampleRate) {
  const length = checkChannels(channels);
  const granule = Math.max(1, Math.round(sampleRate / GRANULES_PER_SECOND));
  const count = Math.floor(length / granule);
  const prefix = new Float64Array(count + 1);
  const { shelf, highpass } = kWeightingCoefficients(sampleRate);
  const [sb0, sb1, sb2] = shelf.b;
  const [sa1, sa2] = shelf.a;
  const [ha1, ha2] = highpass.a;
  const sums = new Float64Array(count);
  for (const channel of channels) {
    // Direct form I in float64, both stages in one pass, starting from rest
    // (as the server's measurement of the uploaded selection does).
    let x1 = 0, x2 = 0, y1 = 0, y2 = 0, z1 = 0, z2 = 0;
    let n = 0;
    for (let g = 0; g < count; g += 1) {
      let sum = 0;
      for (const end = n + granule; n < end; n += 1) {
        const x = channel[n];
        // Shelf output y, then the high-pass on it (numerator 1, -2, 1).
        const y = sb0 * x + sb1 * x1 + sb2 * x2 - sa1 * y1 - sa2 * y2;
        const z = y - 2 * y1 + y2 - ha1 * z1 - ha2 * z2;
        x2 = x1; x1 = x;
        y2 = y1; y1 = y;
        z2 = z1; z1 = z;
        sum += z * z;
      }
      sums[g] += sum;
    }
  }
  for (let i = 0; i < count; i += 1) {
    prefix[i + 1] = prefix[i] + sums[i];
  }
  return { prefix, granule, count };
}

function lufsOf(energy, samples) {
  // Mean square summed over channels (weights 1.0), then the BS.1770 offset.
  // Zero energy is -Infinity, which every comparison below handles.
  return OFFSET + 10 * Math.log10(energy / samples);
}

/**
 * Integrated loudness of the whole input with BS.1770-4 gating.
 * @param {Float32Array[]} channels 1 or 2 channels of equal length
 * @param {number} sampleRate in Hz
 * @returns {number} LUFS, or -Infinity when no block passes the gates
 *   (silence, or input shorter than one 400 ms block)
 */
export function integratedLufs(channels, sampleRate) {
  const { prefix, granule, count } = energyPrefix(channels, sampleRate);
  const block = Math.round(BLOCK_SECONDS * GRANULES_PER_SECOND);
  const step = Math.round(STEP_SECONDS * GRANULES_PER_SECOND);
  const samples = block * granule;
  const energies = [];
  for (let start = 0; start + block <= count; start += step) {
    const energy = prefix[start + block] - prefix[start];
    if (lufsOf(energy, samples) > ABSOLUTE_GATE) {
      energies.push(energy);
    }
  }
  if (energies.length === 0) {
    return -Infinity;
  }
  const mean = (list) => list.reduce((sum, e) => sum + e, 0) / list.length;
  const threshold = lufsOf(mean(energies), samples) + RELATIVE_GATE;
  const kept = energies.filter((energy) => lufsOf(energy, samples) > threshold);
  // The mean block is always above mean - 10 LU, so kept is never empty.
  return lufsOf(mean(kept), samples);
}

/**
 * Loudness of windows of `size` granules starting every `hop` granules.
 * Input shorter than one window gives one value for the whole input.
 * Used by shortTermCurve and by suggest.js (which may need a window shorter
 * than 3 s when MAX_CLIP_SECONDS is).
 * @param {{prefix: Float64Array, granule: number, count: number}} energy from energyPrefix
 * @param {number} size window length in granules (>= 1)
 * @param {number} hop hop in granules (>= 1)
 * @returns {Float32Array} LUFS per window, -Infinity for digital silence
 */
export function windowCurve(energy, size, hop) {
  const { prefix, granule, count } = energy;
  if (count === 0) {
    return new Float32Array(0);
  }
  const span = Math.min(size, count);
  const n = Math.floor((count - span) / hop) + 1;
  const curve = new Float32Array(n);
  for (let i = 0; i < n; i += 1) {
    const start = i * hop;
    curve[i] = lufsOf(prefix[start + span] - prefix[start], span * granule);
  }
  return curve;
}

/**
 * Short-term (3 s) loudness every `hop` seconds: value i covers
 * [i * hop, i * hop + 3 s). Input shorter than 3 s gives one value.
 * O(N) in the input length.
 * @param {Float32Array[]} channels 1 or 2 channels of equal length
 * @param {number} sampleRate in Hz
 * @param {number} [hop=0.1] seconds between window starts, rounded to 10 ms
 * @returns {Float32Array} LUFS per window, -Infinity for digital silence
 */
export function shortTermCurve(channels, sampleRate, hop = 0.1) {
  if (!(Number.isFinite(hop) && hop > 0)) {
    throw new RangeError("hop must be a positive number of seconds");
  }
  const energy = energyPrefix(channels, sampleRate);
  const hopGranules = Math.max(1, Math.round(hop * GRANULES_PER_SECOND));
  return windowCurve(energy, SHORT_TERM_SECONDS * GRANULES_PER_SECOND, hopGranules);
}

/**
 * The gain in dB that moves `measured` to `target`, clamped like the server's.
 * A measurement at or below -70 LUFS (silence, -Infinity, NaN) counts as -70:
 * ffmpeg's ebur128 never reports below its -70 gate, so for a silent
 * selection the server applies the full clamp, and the preview must too.
 * @param {number} measured integrated loudness in LUFS
 * @param {number} target target loudness in LUFS
 * @param {number} [clampDb=20] the server's GAIN_CLAMP_DB
 * @returns {number} gain in dB, within [-clampDb, clampDb]
 */
export function gainForTarget(measured, target, clampDb = 20) {
  const floor = measured > ABSOLUTE_GATE ? measured : ABSOLUTE_GATE;
  const gain = target - floor;
  return Math.max(-clampDb, Math.min(clampDb, gain));
}

export { GRANULES_PER_SECOND, SHORT_TERM_SECONDS };
