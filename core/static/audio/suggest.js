// The initial selection: docs/PLAN.md 7.2. The first rule that applies wins:
//
// 1. A t= timestamp from the shared link: the selection starts there.
// 2. YouTube's "Most Replayed" heatmap: the window with the highest summed
//    value, each point weighted by the seconds of it the window covers.
// 3. Otherwise the MAX_CLIP_SECONDS window with the highest mean short-term
//    loudness, since choruses tend to be the loudest part of a song.
//
// Whichever rule placed it, the start then moves to the quietest 50 ms within
// +-1 s so it does not cut into a word, and the window keeps its full length.
//
// Everything is O(N) in the input: one K-weighting pass builds a prefix sum of
// energy per 10 ms (loudness.js), after which every 3 s window and every 50 ms
// RMS is a subtraction, and a second prefix sum over the curve scores every
// candidate window in O(1). A 10-minute file stays fast on a phone. The
// heatmap's score is O(points x candidates), with at most 200 points.

import { GRANULES_PER_SECOND, SHORT_TERM_SECONDS, energyPrefix, windowCurve } from "./loudness.js";

// Candidate windows start every 100 ms, the short-term curve's hop.
const HOP_GRANULES = GRANULES_PER_SECOND / 10;
// How far the start may move to find a quiet spot, and how long that spot is.
const SNAP_GRANULES = GRANULES_PER_SECOND;
const RMS_GRANULES = GRANULES_PER_SECOND / 20;
// Silence and anything below the absolute gate count as -70 LUFS, so one
// silent gap does not drag a window's mean to -Infinity.
const FLOOR_LUFS = -70;
// Rule 1 needs at least this much audio after the timestamp: a t= in the last
// half second would leave nothing of what the link pointed at once the
// window slides back to fit.
const TIMESTAMP_MARGIN_SECONDS = 0.5;
// The fetcher re-emits at most 200 heatmap points (docs/PLAN.md 6). A longer
// list did not come from it, and would make the O(points) scoring slow.
const MAX_HEATMAP_POINTS = 200;
// YouTube's last heatmap segment follows the video's duration, which can run
// a little past the decoded audio's.
const HEATMAP_END_SLACK_SECONDS = 1;
// Candidate heatmap windows start every 100 ms, as rule 3's do.
const HOP_SECONDS = 0.1;
// Sums of a few hundred products differ in their last bits for windows that
// are really equal; within this they tie, and the earliest wins.
const SCORE_EPSILON = 1e-9;

/**
 * Suggest the initial selection.
 * @param {Float32Array[]} channels 1 or 2 channels of equal length
 * @param {number} sampleRate in Hz
 * @param {number} maxSeconds MAX_CLIP_SECONDS
 * @param {{heatmap?: unknown, timestamp?: unknown}} [options] the shared
 *   link's t= in seconds (rule 1) and the fetcher's heatmap, an array of
 *   {start, end, value} (rule 2); an invalid value is ignored, never thrown on
 * @returns {{start: number, end: number}} seconds; end - start is maxSeconds,
 *   or the whole input when it is no longer than that
 */
export function suggestSelection(channels, sampleRate, maxSeconds, options = { heatmap: null, timestamp: null }) {
  if (!(Number.isFinite(maxSeconds) && maxSeconds > 0)) {
    throw new RangeError("maxSeconds must be positive");
  }
  if (!Array.isArray(channels) || channels.length === 0) {
    throw new RangeError("expected 1 or 2 channels");
  }
  const duration = channels[0].length / sampleRate;
  if (duration <= maxSeconds) {
    return { start: 0, end: duration };
  }
  const energy = energyPrefix(channels, sampleRate);
  const { prefix, granule, count } = energy;
  const toGranule = (seconds) => Math.min(Math.max(Math.round((seconds * sampleRate) / granule), 0), count);
  const { heatmap = null, timestamp = null } = options ?? {};

  let chosen;
  const rule = suggestionRule(duration, { heatmap, timestamp });
  if (rule === "timestamp") {
    chosen = toGranule(timestamp);
  } else if (rule === "heatmap") {
    chosen = toGranule(scoreHeatmap(heatmap, maxSeconds, duration, HOP_SECONDS));
  } else {
    const maxGranules = Math.round(maxSeconds * GRANULES_PER_SECOND);
    // A cap under 3 s gets windows as long as the cap, so every candidate
    // still has at least one loudness value inside it.
    const windowGranules = Math.min(SHORT_TERM_SECONDS * GRANULES_PER_SECOND, maxGranules);
    const curve = windowCurve(energy, windowGranules, HOP_GRANULES);
    if (!curve.some((value) => value > FLOOR_LUFS)) {
      return { start: 0, end: Math.min(maxSeconds, duration) };
    }
    const perWindow = Math.floor((maxGranules - windowGranules) / HOP_GRANULES) + 1;
    const candidates = Math.max(0, Math.floor((count - maxGranules) / HOP_GRANULES)) + 1;
    chosen = bestWindow(curve, perWindow, candidates) * HOP_GRANULES;
  }
  const snapped = snapToQuiet(prefix, count, chosen);

  let start = (snapped * granule) / sampleRate;
  let end = start + maxSeconds;
  if (end > duration) {
    // Moving the start later must not shorten the selection: slide back.
    end = duration;
    start = duration - maxSeconds;
  }
  return { start, end };
}

/**
 * Which rule places the selection in input this long, when it is longer than
 * MAX_CLIP_SECONDS (shorter input is selected whole). Exported so the page
 * can say why the selection is where it is.
 * @param {number} duration the input's length in seconds
 * @param {{heatmap?: unknown, timestamp?: unknown}} [options] as suggestSelection's
 * @returns {"timestamp" | "heatmap" | "loudness"}
 */
export function suggestionRule(duration, options = {}) {
  const { heatmap = null, timestamp = null } = options ?? {};
  // typeof first: a string such as "90" must never be coerced into a position.
  if (isFiniteNumber(timestamp) && timestamp >= 0 && timestamp < duration - TIMESTAMP_MARGIN_SECONDS) {
    return "timestamp";
  }
  if (validHeatmap(heatmap, duration)) {
    return "heatmap";
  }
  return "loudness";
}

function isFiniteNumber(value) {
  return typeof value === "number" && Number.isFinite(value);
}

/**
 * Whether every heatmap point is usable. One bad point disables rule 2
 * altogether: a list that is partly wrong is not trusted to be partly right,
 * and the loudness rule is a better guess than a window placed by bad data.
 */
function validHeatmap(points, duration) {
  if (!Array.isArray(points) || points.length === 0 || points.length > MAX_HEATMAP_POINTS) {
    return false;
  }
  return points.every((point) => point !== null && typeof point === "object"
    && isFiniteNumber(point.start) && isFiniteNumber(point.end) && isFiniteNumber(point.value)
    && point.start >= 0 && point.start < point.end && point.end <= duration + HEATMAP_END_SLACK_SECONDS
    && point.value >= 0 && point.value <= 1);
}

/**
 * Rule 2's choice: of the windows [s, s + maxSeconds] that start every
 * hopSeconds and end within the input, the one with the highest sum over the
 * points of value x the seconds of the point the window covers. Exported for
 * the tests. The points must already be valid (see validHeatmap).
 * @param {{start: number, end: number, value: number}[]} points
 * @param {number} maxSeconds the window's length
 * @param {number} duration the input's length in seconds (more than maxSeconds)
 * @param {number} hopSeconds the distance between candidate starts
 * @returns {number} the best window's start in seconds; of equal ones the earliest
 */
export function scoreHeatmap(points, maxSeconds, duration, hopSeconds) {
  // The allowance keeps the window that ends exactly at the input's end, when
  // that is a whole number of hops, from being lost to rounding.
  const candidates = Math.max(0, Math.floor((duration - maxSeconds) / hopSeconds + 1e-9)) + 1;
  let best = 0;
  let bestScore = -Infinity;
  for (let k = 0; k < candidates; k += 1) {
    const from = k * hopSeconds;
    const to = from + maxSeconds;
    let score = 0;
    for (const point of points) {
      const overlap = Math.min(to, point.end) - Math.max(from, point.start);
      if (overlap > 0) {
        score += point.value * overlap;
      }
    }
    // Clearly greater only: of equal windows the earliest wins.
    if (score > bestScore + SCORE_EPSILON) {
      bestScore = score;
      best = from;
    }
  }
  return best;
}

/**
 * Rule 3's choice: the candidate window with the highest mean of the hop
 * loudness values inside it, -Infinity and anything below -70 counted as -70.
 * Window k holds curve[k] to curve[k + perWindow - 1]. Exported for the tests,
 * which need exact ties that real audio never gives.
 * @param {Float32Array | number[]} curve hop loudness values in LUFS
 * @param {number} perWindow hop values inside one window (>= 1)
 * @param {number} candidates windows to consider, starting at hop 0
 * @returns {number} the index of the best window; of equal ones the earliest
 */
export function bestWindow(curve, perWindow, candidates) {
  // Equal windows have the same number of values, so comparing sums is
  // comparing means, and a prefix sum makes each window O(1).
  const sums = new Float64Array(curve.length + 1);
  for (let i = 0; i < curve.length; i += 1) {
    sums[i + 1] = sums[i] + Math.max(curve[i], FLOOR_LUFS);
  }
  let best = 0;
  let bestScore = -Infinity;
  for (let k = 0; k < candidates && k + perWindow <= curve.length; k += 1) {
    const score = sums[k + perWindow] - sums[k];
    // Strictly greater: of equally loud windows the earliest wins.
    if (score > bestScore) {
      bestScore = score;
      best = k;
    }
  }
  return best;
}

/**
 * The cut point near a chosen start: the granule within 1 s whose 50 ms
 * around it holds the least energy, so the selection does not open mid-word.
 * Exported for the tests, as bestWindow is.
 * @param {Float64Array} prefix energy prefix sum per granule (energyPrefix)
 * @param {number} count granules in the input
 * @param {number} chosen the start granule bestWindow picked
 * @returns {number} the start granule; of equally quiet spots the nearest,
 *   then the earlier one; chosen itself unless somewhere is strictly quieter
 */
export function snapToQuiet(prefix, count, chosen) {
  if (count < RMS_GRANULES) {
    return chosen;
  }
  const rms = (at) => {
    // The 50 ms around a candidate cut point, kept inside the input.
    const from = Math.min(Math.max(at - Math.floor(RMS_GRANULES / 2), 0), count - RMS_GRANULES);
    return prefix[from + RMS_GRANULES] - prefix[from];
  };
  let snapped = chosen;
  let quietest = rms(chosen);
  // Outward from the chosen start, earlier side first, strictly lower only:
  // of equally quiet spots the nearest wins (then the earlier one).
  for (let distance = 1; distance <= SNAP_GRANULES; distance += 1) {
    for (const at of [chosen - distance, chosen + distance]) {
      if (at < 0 || at > count) {
        continue;
      }
      const value = rms(at);
      if (value < quietest) {
        quietest = value;
        snapped = at;
      }
    }
  }
  return snapped;
}
