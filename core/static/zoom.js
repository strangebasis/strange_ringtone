// The magnified view under the waveform (docs/PLAN.md 7.1): the selection
// stretched to the full width, with a strip of the audio outside it on either
// side. Its trim bars move the same two edges as the waveform's handles, only
// finer, because a pixel here is far less audio. With loop controls on, two
// loop bars mark the part of the selection that Play repeats. While the
// preview plays, the audio heard so far on this pass is drawn in the
// waveform's colour, which is the playhead.
//
// The bars are drawn on the canvas, and each has an element laid over it that
// takes the pointer. So a touch anywhere else on the view scrolls the page as
// usual, and which bar a touch picks is decided by plain stacking: the loop
// bars lie in front, and a trim bar under one is reached by its tip.
import { fadeGainAt } from "./audio/fades.js";
import { moveLoopEdge } from "./loop.js";
import { moveEdge } from "./waveform.js";

// The view from top to bottom, in CSS pixels: the diagonals that join the
// waveform's handles to the trim bars, the trim bars' tips, the magnified
// waveform, and the loop bars' feet.
const LINK_CSS_PX = 32;
// The tip is all that is left of a trim bar's touch target when a loop bar
// sits on it, so with the grip above it, it is about Apple's 44 px minimum.
const TIP_CSS_PX = 36;
const WAVE_CSS_PX = 112;
const FOOT_CSS_PX = 16;
// style.css gives #zoom-canvas this height; a test keeps the two equal.
export const HEIGHT_CSS_PX = LINK_CSS_PX + TIP_CSS_PX + WAVE_CSS_PX + FOOT_CSS_PX;

// The strip of audio shown outside the selection on either side, as a share
// of the width, and never so thin that a bar's touch target leaves the view.
const MARGIN_SHARE = 0.06;
const MIN_MARGIN_CSS_PX = 24;

// The trim bars are a little thicker than the loop bars and stand taller, so
// a trim bar still shows around and above a loop bar that sits on it.
const TRIM_DRAWN_CSS_PX = 6;
const LOOP_DRAWN_CSS_PX = 4;
const LINK_DRAWN_CSS_PX = 4;
const TRIM_GRIP_CSS_PX = 14;
const LOOP_GRIP_CSS_PX = 12;
// The audio outside the selection is drawn fainter than the audio inside it.
const OUTSIDE_ALPHA = 0.45;

// A loop bar let go this close to a trim bar rests on it. "On it" has to be
// exact for the loop bar to travel with the trim bar afterwards, and a finger
// cannot place a bar to the pixel.
const SNAP_CSS_PX = 4;

// When a trim bar is let go the view re-fits to the new selection. A short
// glide shows where the bar went; a jump would look like another picture.
const GLIDE_MS = 150;

const TRIM_BARS = { start: "start", end: "end" };
const LOOP_BARS = { loopStart: "start", loopEnd: "end" };

function clamp(value, low, high) {
  return Math.min(Math.max(value, low), high);
}

/**
 * The view that shows the selection between its two margins: the frame at the
 * left edge (it may lie before the audio's start) and the frames per CSS pixel.
 * @param {{start: number, end: number}} selection frames
 * @param {number} width CSS pixels
 * @returns {{start: number, perPx: number}}
 */
export function fitView(selection, width) {
  const margin = Math.max(MIN_MARGIN_CSS_PX, width * MARGIN_SHARE);
  const perPx = (selection.end - selection.start) / Math.max(1, width - 2 * margin);
  return { start: selection.start - margin * perPx, perPx };
}

export class ZoomView {
  /**
   * @param {{canvas: HTMLCanvasElement, top: HTMLElement,
   *   bars: {start: HTMLElement, end: HTMLElement, loopStart: HTMLElement, loopEnd: HTMLElement}}} parts
   *   the view's canvas, the waveform's canvas above it (the diagonals start
   *   at its handles), and the four elements that take the pointer
   */
  constructor({ canvas, top, bars }) {
    this.canvas = canvas;
    this.context = canvas.getContext("2d");
    this.top = top;
    this.bars = bars;
    this.channels = null;
    this.sampleRate = 44100;
    this.length = 0;
    this.selection = { start: 0, end: 0 };
    this.loop = null;
    this.fades = { fadeIn: 0, fadeOut: 0 };
    this.limits = { maxFrames: 0, minFrames: 0, minLoopFrames: 0 };
    this.view = null;
    this.peaks = null;
    this.peaksKey = "";
    this.played = null;
    this.drag = null;
    this.glide = null;
    this.width = 1;
    this.topLeft = 0;
    this.topWidth = 1;
    this.listeners = new Map();

    const tipTop = LINK_CSS_PX - TRIM_GRIP_CSS_PX / 2;
    const waveTop = LINK_CSS_PX + TIP_CSS_PX;
    for (const [name, element] of Object.entries(bars)) {
      // A trim bar is taken from its grip down to the waveform's foot, a loop
      // bar from the waveform's top down to its own grip. CSSOM, not a style
      // attribute: the CSP's style-src 'self' allows this.
      const trim = name in TRIM_BARS;
      element.style.setProperty("--top", (trim ? tipTop : waveTop) + "px");
      element.style.setProperty("--height", (trim ? waveTop + WAVE_CSS_PX - tipTop : WAVE_CSS_PX + FOOT_CSS_PX) + "px");
      element.addEventListener("pointerdown", (event) => this.onPointerDown(name, event));
      element.addEventListener("pointermove", (event) => this.onPointerMove(event));
      element.addEventListener("pointerup", (event) => this.onPointerUp(event));
      element.addEventListener("pointercancel", (event) => this.onPointerUp(event));
    }
    // The diagonals depend on where the waveform's canvas is, so its size
    // counts as much as this canvas's own.
    if (typeof ResizeObserver === "function") {
      const observer = new ResizeObserver(() => this.resize());
      observer.observe(canvas);
      observer.observe(top);
    } else {
      window.addEventListener("resize", () => this.resize());
    }
    const scheme = window.matchMedia("(prefers-color-scheme: dark)");
    scheme.addEventListener("change", () => this.draw());
  }

  /**
   * Register a listener: "change" fires on every move of a trim bar and
   * "commit" when its drag ends, "loopchange" and "loopcommit" likewise for a
   * loop bar.
   */
  on(name, listener) {
    if (!this.listeners.has(name)) this.listeners.set(name, []);
    this.listeners.get(name).push(listener);
  }

  emit(name, detail) {
    for (const listener of this.listeners.get(name) ?? []) listener(detail);
  }

  setAudio(channels, sampleRate, length, limits) {
    this.channels = channels;
    this.sampleRate = sampleRate;
    this.length = length;
    this.limits = limits;
    this.peaks = null;
    this.played = null;
    // The selection and the loop belong to a clip: this one's are set next.
    this.selection = { start: 0, end: 0 };
    this.loop = null;
    this.resize();
  }

  /** Drop the audio, as Waveform.clear does and for the same reason. */
  clear() {
    this.stopGlide();
    this.channels = null;
    this.length = 0;
    this.peaks = null;
    this.view = null;
    this.selection = { start: 0, end: 0 };
    this.loop = null;
    this.played = null;
    this.draw();
  }

  /**
   * Show a selection. The view re-fits to it at once, except while one of
   * this view's own trim bars is being dragged: then the picture holds still,
   * so the bar stays under the finger and a pixel stays the same amount of
   * audio from the press to the lift.
   */
  setSelection(start, end) {
    const same = start === this.selection.start && end === this.selection.end;
    this.selection = { start, end };
    if (this.draggingTrim() || (same && this.view !== null)) {
      this.draw();
      return;
    }
    this.fit(false);
  }

  setFades(fadeIn, fadeOut) {
    this.fades = { fadeIn, fadeOut };
    this.draw();
  }

  /** The loop bars' frames, or null to take the loop bars away. */
  setLoop(loop) {
    this.loop = loop === null ? null : { start: loop.start, end: loop.end };
    this.draw();
  }

  /** The frames heard so far on this pass, [from, to), or null when nothing plays. */
  setPlayed(played) {
    if (played === null && this.played === null) return;
    if (played !== null && this.played !== null && played.from === this.played.from && played.to === this.played.to) {
      return;
    }
    this.played = played === null ? null : { from: played.from, to: played.to };
    this.draw();
  }

  draggingTrim() {
    return this.drag !== null && this.drag.name in TRIM_BARS;
  }

  measure() {
    const rect = this.canvas.getBoundingClientRect();
    const topRect = this.top.getBoundingClientRect();
    this.width = Math.max(1, Math.round(rect.width));
    this.topLeft = topRect.left - rect.left;
    this.topWidth = Math.max(1, Math.round(topRect.width));
  }

  resize() {
    const ratio = window.devicePixelRatio || 1;
    this.measure();
    const pixelWidth = Math.round(this.width * ratio);
    const pixelHeight = Math.round(HEIGHT_CSS_PX * ratio);
    if (this.canvas.width !== pixelWidth || this.canvas.height !== pixelHeight) {
      this.canvas.width = pixelWidth;
      this.canvas.height = pixelHeight;
    }
    // Drawn in CSS pixels, as the waveform is.
    this.context.setTransform(ratio, 0, 0, ratio, 0, 0);
    this.fit(false);
  }

  // --- the view ------------------------------------------------------------------

  frameToX(frame) {
    return (frame - this.view.start) / this.view.perPx;
  }

  xToFrame(x) {
    return this.view.start + x * this.view.perPx;
  }

  /** Fit the view to the selection, gliding there when asked and allowed. */
  fit(glide) {
    this.stopGlide();
    // No audio, or no selection yet (setAudio comes before setSelection):
    // there is no scale to draw at, so nothing is drawn and no bar is shown.
    if (this.length === 0 || this.selection.end <= this.selection.start) {
      this.view = null;
      this.draw();
      return;
    }
    const target = fitView(this.selection, this.width);
    const from = this.view;
    if (!glide || from === null || !this.canGlide()) {
      this.view = target;
      this.draw();
      return;
    }
    const state = { frame: 0, startedAt: null };
    const step = (now) => {
      if (state.startedAt === null) state.startedAt = now;
      const t = clamp((now - state.startedAt) / GLIDE_MS, 0, 1);
      // Ease out: quick away from where the bar was let go, gentle on arrival.
      const eased = 1 - (1 - t) * (1 - t);
      this.view = {
        start: from.start + (target.start - from.start) * eased,
        perPx: from.perPx + (target.perPx - from.perPx) * eased,
      };
      if (t < 1) {
        state.frame = requestAnimationFrame(step);
      } else {
        this.view = target;
        this.glide = null;
      }
      this.draw();
    };
    this.glide = state;
    state.frame = requestAnimationFrame(step);
  }

  canGlide() {
    if (typeof requestAnimationFrame !== "function") return false;
    // Someone who asked the device for less motion gets the jump.
    return window.matchMedia("(prefers-reduced-motion: reduce)").matches !== true;
  }

  stopGlide() {
    if (this.glide === null) return;
    cancelAnimationFrame(this.glide.frame);
    this.glide = null;
  }

  // One min/max pair per CSS pixel column of the view, across all channels.
  // Kept until the view moves: a drag and the playhead redraw many times over
  // the same picture.
  viewPeaks() {
    const key = this.view.start + "|" + this.view.perPx + "|" + this.width;
    if (this.peaks !== null && this.peaksKey === key) return this.peaks;
    const width = this.width;
    const mins = new Float32Array(width);
    const maxs = new Float32Array(width);
    // A column with no audio under it: before the clip's start or after its end.
    const empty = new Uint8Array(width);
    for (let column = 0; column < width; column += 1) {
      const first = Math.floor(this.xToFrame(column));
      const past = Math.floor(this.xToFrame(column + 1));
      if (past <= 0 || first >= this.length) {
        empty[column] = 1;
        continue;
      }
      const from = Math.max(0, first);
      const to = Math.min(this.length, Math.max(from + 1, past));
      let low = 0;
      let high = 0;
      for (const data of this.channels) {
        for (let i = from; i < to; i += 1) {
          const value = data[i];
          if (value < low) low = value;
          if (value > high) high = value;
        }
      }
      mins[column] = low;
      maxs[column] = high;
    }
    this.peaks = { mins, maxs, empty };
    this.peaksKey = key;
    return this.peaks;
  }

  // --- drawing -------------------------------------------------------------------

  colours() {
    const style = getComputedStyle(this.canvas);
    const read = (name, fallback) => style.getPropertyValue(name).trim() || fallback;
    return {
      wave: read("--wave", "#0a64d8"),
      waveDim: read("--wave-dim", "#9aa5b1"),
      selection: read("--selection", "rgba(10, 100, 216, 0.12)"),
      fade: read("--fade", "rgba(0, 0, 0, 0.35)"),
      handle: read("--handle", "#d93025"),
      loop: read("--loop", "#a21caf"),
    };
  }

  draw() {
    const ctx = this.context;
    ctx.clearRect(0, 0, this.width, HEIGHT_CSS_PX);
    this.placeBars();
    if (this.channels === null || this.view === null) return;
    const colours = this.colours();
    const startX = this.frameToX(this.selection.start);
    const endX = this.frameToX(this.selection.end);
    // The waveform's tint behind the selection: the same stretch of audio,
    // and what the fade shading is seen against.
    ctx.fillStyle = colours.selection;
    ctx.fillRect(startX, LINK_CSS_PX + TIP_CSS_PX, endX - startX, WAVE_CSS_PX);
    this.drawWave(startX, endX, colours);
    this.drawFades(startX, endX, colours);
    this.drawLink(this.selection.start, startX, colours);
    this.drawLink(this.selection.end, endX, colours);
    this.drawTrimBar(startX, colours);
    this.drawTrimBar(endX, colours);
    if (this.loop !== null) {
      // After the trim bars: a loop bar on a trim bar is the one in front.
      this.drawLoopBar(this.frameToX(this.loop.start), colours);
      this.drawLoopBar(this.frameToX(this.loop.end), colours);
    }
  }

  // Grey for the selection, fainter grey for the audio outside it, and the
  // waveform's own colour for what this pass has played so far.
  drawWave(startX, endX, colours) {
    const ctx = this.context;
    const peaks = this.viewPeaks();
    const top = LINK_CSS_PX + TIP_CSS_PX;
    const middle = top + WAVE_CSS_PX / 2;
    const scale = WAVE_CSS_PX / 2 - 4;
    for (let column = 0; column < this.width; column += 1) {
      if (peaks.empty[column] === 1) continue;
      const centre = column + 0.5;
      const inside = centre >= startX && centre <= endX;
      const frame = this.xToFrame(centre);
      const heard = this.played !== null && frame >= this.played.from && frame < this.played.to;
      ctx.globalAlpha = inside ? 1 : OUTSIDE_ALPHA;
      ctx.fillStyle = heard ? colours.wave : colours.waveDim;
      const high = middle - peaks.maxs[column] * scale;
      const low = middle - peaks.mins[column] * scale;
      ctx.fillRect(column, high, 1, Math.max(1, low - high));
    }
    ctx.globalAlpha = 1;
  }

  // The waveform's fade shading, at this view's scale: here a fade is wide
  // enough to see its shape.
  drawFades(startX, endX, colours) {
    const { fadeIn, fadeOut } = this.fades;
    if (fadeIn <= 0 && fadeOut <= 0) return;
    const duration = (this.selection.end - this.selection.start) / this.sampleRate;
    const span = endX - startX;
    if (span < 1 || duration <= 0) return;
    const ctx = this.context;
    const top = LINK_CSS_PX + TIP_CSS_PX;
    ctx.beginPath();
    ctx.moveTo(startX, top);
    const steps = Math.max(2, Math.ceil(span));
    for (let step = 0; step <= steps; step += 1) {
      const t = (step / steps) * duration;
      const gain = fadeGainAt(t, duration, fadeIn, fadeOut);
      ctx.lineTo(startX + (step / steps) * span, top + WAVE_CSS_PX * (1 - gain));
    }
    ctx.lineTo(endX, top);
    ctx.closePath();
    ctx.fillStyle = colours.fade;
    ctx.fill();
  }

  // From the foot of the waveform's handle to the head of the trim bar: the
  // two are the same edge.
  drawLink(frame, x, colours) {
    const ctx = this.context;
    const above = this.topLeft + (this.length === 0 ? 0 : (frame / this.length) * this.topWidth);
    ctx.strokeStyle = colours.handle;
    ctx.lineWidth = LINK_DRAWN_CSS_PX;
    ctx.lineCap = "round";
    ctx.beginPath();
    ctx.moveTo(above, 0);
    ctx.lineTo(x, LINK_CSS_PX);
    ctx.stroke();
  }

  drawTrimBar(x, colours) {
    const ctx = this.context;
    ctx.fillStyle = colours.handle;
    ctx.fillRect(x - TRIM_DRAWN_CSS_PX / 2, LINK_CSS_PX, TRIM_DRAWN_CSS_PX, TIP_CSS_PX + WAVE_CSS_PX);
    ctx.beginPath();
    ctx.arc(x, LINK_CSS_PX, TRIM_GRIP_CSS_PX / 2, 0, Math.PI * 2);
    ctx.fill();
  }

  drawLoopBar(x, colours) {
    const ctx = this.context;
    const top = LINK_CSS_PX + TIP_CSS_PX;
    ctx.fillStyle = colours.loop;
    ctx.fillRect(x - LOOP_DRAWN_CSS_PX / 2, top, LOOP_DRAWN_CSS_PX, WAVE_CSS_PX + FOOT_CSS_PX - LOOP_GRIP_CSS_PX / 2);
    ctx.beginPath();
    ctx.arc(x, HEIGHT_CSS_PX - LOOP_GRIP_CSS_PX / 2, LOOP_GRIP_CSS_PX / 2, 0, Math.PI * 2);
    ctx.fill();
  }

  // Each bar's element follows the bar, so the pointer finds it where it is drawn.
  placeBars() {
    const shown = this.channels !== null && this.view !== null;
    for (const [name, element] of Object.entries(this.bars)) {
      const frame = shown ? this.barFrame(name) : null;
      element.hidden = frame === null;
      if (frame !== null) element.style.setProperty("--x", this.frameToX(frame) + "px");
    }
  }

  barFrame(name) {
    if (name in TRIM_BARS) return this.selection[TRIM_BARS[name]];
    return this.loop === null ? null : this.loop[LOOP_BARS[name]];
  }

  // --- the pointer ---------------------------------------------------------------

  localX(event) {
    return event.clientX - this.canvas.getBoundingClientRect().left;
  }

  onPointerDown(name, event) {
    if (this.channels === null || this.view === null || this.drag !== null) return;
    const frame = this.barFrame(name);
    if (frame === null) return;
    event.preventDefault();
    const element = this.bars[name];
    // Capture keeps the drag alive when the finger leaves the bar's element.
    element.setPointerCapture(event.pointerId);
    // A press during the glide holds the picture where it has got to.
    this.stopGlide();
    this.drag = {
      pointerId: event.pointerId,
      name,
      element,
      // Where on the bar's 44 px the press landed. Without it the bar would
      // jump to the finger on the first move, by up to two seconds of audio.
      grab: this.localX(event) - this.frameToX(frame),
    };
  }

  onPointerMove(event) {
    if (this.drag === null || event.pointerId !== this.drag.pointerId) return;
    event.preventDefault();
    const { name } = this.drag;
    // The bar cannot leave the picture, which holds still for the whole drag.
    const x = clamp(this.localX(event) - this.drag.grab, 0, this.width);
    if (name in TRIM_BARS) {
      const edge = TRIM_BARS[name];
      const next = moveEdge(this.selection, edge, this.xToFrame(x), this.length,
        this.limits.maxFrames, this.limits.minFrames);
      if (next.start === this.selection.start && next.end === this.selection.end) return;
      this.selection = next;
      this.draw();
      this.emit("change", { start: next.start, end: next.end, edge });
      return;
    }
    if (this.loop === null) return;
    const edge = LOOP_BARS[name];
    const next = moveLoopEdge(this.loop, edge, this.loopFrameAt(x, edge), this.selection, this.limits.minLoopFrames);
    if (next.start === this.loop.start && next.end === this.loop.end) return;
    this.loop = next;
    this.draw();
    this.emit("loopchange", { start: next.start, end: next.end });
  }

  // A loop bar within a few pixels of the trim bar on its side rests on it.
  loopFrameAt(x, edge) {
    const trim = this.selection[edge];
    return Math.abs(x - this.frameToX(trim)) <= SNAP_CSS_PX ? trim : this.xToFrame(x);
  }

  onPointerUp(event) {
    if (this.drag === null || event.pointerId !== this.drag.pointerId) return;
    const { name, element } = this.drag;
    if (element.hasPointerCapture(event.pointerId)) element.releasePointerCapture(event.pointerId);
    this.drag = null;
    if (name in TRIM_BARS) {
      this.fit(true);
      this.emit("commit");
    } else {
      this.emit("loopcommit");
    }
  }
}
