// Fade shapes for the preview, matching the server's render exactly.
//
// render.py fades with ffmpeg's `afade curve=qsin` (FADE_CURVE), a quarter
// sine: gain = sin(pi/2 * progress). A linear ramp would preview a different
// sound from the one the user downloads, so the preview draws the same curve,
// and the waveform overlay uses fadeGainAt too.

// setValueCurveAtTime interpolates linearly between points. A point every
// 5 ms follows the quarter sine closely for any fade long enough to hear as a
// fade; a fade shorter than 5 ms is a near-click
// in the render as well, so the cruder preview of it changes nothing audible.
const POINTS_PER_SECOND = 200;

function clampFade(fade, duration) {
  // render.py clamps each fade to [0, duration] before handing it to ffmpeg.
  if (!Number.isFinite(fade) || fade <= 0) {
    return 0;
  }
  return Math.min(fade, duration);
}

/**
 * Gain of the faded selection at time t, the shape ffmpeg's afade qsin draws.
 * Fade-in: sin(pi/2 * t / fadeIn) for t < fadeIn. Fade-out:
 * sin(pi/2 * (duration - t) / fadeOut) for t > duration - fadeOut. Where the
 * two overlap (a short selection with long fades) the gains multiply, as the
 * two afade filters in series do. 1 elsewhere; t is clamped to [0, duration].
 * @param {number} t seconds from the selection's start
 * @param {number} duration selection length in seconds
 * @param {number} fadeIn fade-in length in seconds (0 for none)
 * @param {number} fadeOut fade-out length in seconds (0 for none)
 * @returns {number} gain in [0, 1]
 */
export function fadeGainAt(t, duration, fadeIn, fadeOut) {
  if (!(Number.isFinite(duration) && duration > 0)) {
    return 0;
  }
  const time = Math.min(Math.max(Number.isFinite(t) ? t : 0, 0), duration);
  const fin = clampFade(fadeIn, duration);
  const fout = clampFade(fadeOut, duration);
  let gain = 1;
  if (fin > 0 && time < fin) {
    gain *= Math.sin((Math.PI / 2) * (time / fin));
  }
  if (fout > 0 && time > duration - fout) {
    gain *= Math.sin((Math.PI / 2) * ((duration - time) / fout));
  }
  return gain;
}

/**
 * The fade curve sampled for setValueCurveAtTime: at least 200 points per
 * second and never fewer than 2, evenly spaced from 0 to duration inclusive.
 * @param {number} duration selection length in seconds (> 0)
 * @param {number} fadeIn seconds
 * @param {number} fadeOut seconds
 * @returns {Float32Array}
 */
export function fadeCurve(duration, fadeIn, fadeOut) {
  if (!(Number.isFinite(duration) && duration > 0)) {
    throw new RangeError("duration must be positive");
  }
  const points = Math.max(2, Math.ceil(duration * POINTS_PER_SECOND) + 1);
  const curve = new Float32Array(points);
  for (let i = 0; i < points; i += 1) {
    curve[i] = fadeGainAt((i / (points - 1)) * duration, duration, fadeIn, fadeOut);
  }
  return curve;
}

/**
 * Schedule one loop iteration's fades on a GainNode's gain AudioParam.
 *
 * ONE setValueCurveAtTime per iteration: an AudioParam refuses a curve that
 * overlaps another event (NotSupportedError), and linear ramps cannot draw a
 * quarter sine. The loudness gain lives on a separate GainNode so the slider
 * can move it live without touching this curve.
 * @param {AudioParam} gainParam the fade GainNode's gain
 * @param {number} startTime AudioContext time the iteration starts
 * @param {number} duration iteration length in seconds (> 0)
 * @param {number} fadeIn seconds (0 for none)
 * @param {number} fadeOut seconds (0 for none)
 */
export function scheduleFades(gainParam, startTime, duration, fadeIn, fadeOut) {
  gainParam.setValueCurveAtTime(fadeCurve(duration, fadeIn, fadeOut), startTime, duration);
}
