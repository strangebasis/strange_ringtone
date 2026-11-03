// Looped playback through Web Audio. The preview chain is
//   source -> fade gain -> loudness gain -> speakers
// with the two gains kept apart so the loudness slider can move live while the
// fade curves, which are fixed per loop iteration, keep running.
import { scheduleFades } from "./audio/fades.js";

// Iterations are queued this far ahead of the audio clock. Short enough that a
// fade change is heard on the next loop, long enough to survive a busy main
// thread.
const LOOKAHEAD_SECONDS = 0.4;
const TICK_MS = 100;
const START_DELAY_SECONDS = 0.05;
const GAIN_TIME_CONSTANT = 0.03;

export function dbToGain(db) {
  return Math.pow(10, db / 20);
}

export class Player {
  constructor(context) {
    this.context = context;
    this.fadeGain = context.createGain();
    this.levelGain = context.createGain();
    this.fadeGain.connect(this.levelGain);
    this.levelGain.connect(context.destination);
    this.sources = new Set();
    this.timer = null;
    this.mode = null;
    this.opts = { fadeIn: 0, fadeOut: 0, gainDb: 0 };
  }

  get playing() {
    return this.mode !== null;
  }

  /**
   * Loop buffer from start to end (seconds) with the fades and the preview
   * gain. Each iteration is its own AudioBufferSourceNode with its own fade
   * curve, because the fades belong to each pass of the loop, as they will in
   * the ringtone.
   */
  play(buffer, start, end, opts) {
    this.stop();
    this.mode = "preview";
    this.buffer = buffer;
    this.start = start;
    this.duration = end - start;
    this.opts = { ...this.opts, ...opts };
    const now = this.context.currentTime;
    this.levelGain.gain.cancelScheduledValues(0);
    this.levelGain.gain.setValueAtTime(dbToGain(this.opts.gainDb), now);
    this.nextTime = now + START_DELAY_SECONDS;
    this.pump();
    this.timer = setInterval(() => this.pump(), TICK_MS);
  }

  /** Loop a rendered result as it is: the file already has its fades and gain. */
  playPlain(buffer) {
    this.stop();
    this.mode = "plain";
    const now = this.context.currentTime;
    this.levelGain.gain.cancelScheduledValues(0);
    this.levelGain.gain.setValueAtTime(1, now);
    this.fadeGain.gain.setValueAtTime(1, now);
    const source = this.context.createBufferSource();
    source.buffer = buffer;
    source.loop = true;
    source.connect(this.fadeGain);
    this.track(source);
    source.start(now + START_DELAY_SECONDS);
  }

  /** New fades or gain while playing: the gain moves now, fades from the next loop. */
  update(opts) {
    this.opts = { ...this.opts, ...opts };
    if (this.mode !== "preview") return;
    this.levelGain.gain.setTargetAtTime(dbToGain(this.opts.gainDb), this.context.currentTime, GAIN_TIME_CONSTANT);
  }

  stop() {
    if (this.timer !== null) {
      clearInterval(this.timer);
      this.timer = null;
    }
    for (const source of this.sources) {
      try {
        source.stop();
      } catch {
        // A source that never started or already ended; nothing to stop.
      }
      source.disconnect();
    }
    this.sources.clear();
    this.fadeGain.gain.cancelScheduledValues(0);
    this.fadeGain.gain.setValueAtTime(1, this.context.currentTime);
    this.mode = null;
  }

  track(source) {
    this.sources.add(source);
    source.addEventListener("ended", () => {
      this.sources.delete(source);
      source.disconnect();
    });
  }

  pump() {
    if (this.mode !== "preview") return;
    const now = this.context.currentTime;
    if (this.nextTime < now) {
      // The page was throttled (a background tab) and the queue ran dry:
      // start a fresh loop rather than scheduling in the past.
      this.fadeGain.gain.cancelScheduledValues(0);
      this.nextTime = now + START_DELAY_SECONDS;
    }
    while (this.nextTime - now < LOOKAHEAD_SECONDS) {
      this.scheduleIteration(this.nextTime);
      // The same sum the AudioParam uses for the curve's end, so the next
      // curve starts exactly where this one stops and the two never overlap.
      this.nextTime = this.nextTime + this.duration;
    }
  }

  scheduleIteration(when) {
    const { fadeIn, fadeOut } = this.opts;
    try {
      scheduleFades(this.fadeGain.gain, when, this.duration, fadeIn, fadeOut);
    } catch {
      // An overlapping curve throws NotSupportedError; clear what is queued
      // from here on and try once more rather than stopping the loop.
      this.fadeGain.gain.cancelScheduledValues(when);
      scheduleFades(this.fadeGain.gain, when, this.duration, fadeIn, fadeOut);
    }
    const source = this.context.createBufferSource();
    source.buffer = this.buffer;
    source.connect(this.fadeGain);
    this.track(source);
    source.start(when, this.start, this.duration);
  }
}
