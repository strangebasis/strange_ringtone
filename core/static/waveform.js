// The waveform canvas: peaks, the selection, its two handles and the fade
// shapes. Positions are whole frames at the decoded sample rate, so the cap on
// the selection is exact rather than a rounded number of seconds.
import { fadeGainAt } from "./audio/fades.js";

const HEIGHT_CSS_PX = 160;
// Apple's minimum touch target. The handles are drawn thinner, but a finger
// anywhere within 22 px of one picks it up.
const HANDLE_HIT_CSS_PX = 44;
const HANDLE_DRAWN_CSS_PX = 4;
const GRIP_CSS_PX = 14;

/**
 * Move one edge of the selection to `frame`, keeping the length between
 * minFrames and maxFrames: dragging an edge past the cap pulls the other edge
 * along, and pushing it closer than the minimum pushes the other edge too.
 * edge "move" shifts the whole selection, keeping its length.
 */
export function moveEdge(selection, edge, frame, length, maxFrames, minFrames) {
  let { start, end } = selection;
  const target = Math.round(frame);
  if (edge === "start") {
    start = Math.min(Math.max(target, 0), length - minFrames);
    if (end - start > maxFrames) end = start + maxFrames;
    if (end - start < minFrames) end = start + minFrames;
  } else if (edge === "end") {
    end = Math.min(Math.max(target, minFrames), length);
    if (end - start > maxFrames) start = end - maxFrames;
    if (end - start < minFrames) start = end - minFrames;
  } else if (edge === "move") {
    const span = end - start;
    start = Math.min(Math.max(target, 0), length - span);
    end = start + span;
  }
  return { start, end };
}

export class Waveform {
  constructor(canvas) {
    this.canvas = canvas;
    this.context = canvas.getContext("2d");
    this.channels = null;
    this.sampleRate = 44100;
    this.length = 0;
    this.selection = { start: 0, end: 0 };
    this.fades = { fadeIn: 0, fadeOut: 0 };
    this.limits = { maxFrames: 0, minFrames: 0 };
    this.peaks = null;
    this.peakWidth = 0;
    this.drag = null;
    this.listeners = new Map();

    canvas.addEventListener("pointerdown", (event) => this.onPointerDown(event));
    canvas.addEventListener("pointermove", (event) => this.onPointerMove(event));
    canvas.addEventListener("pointerup", (event) => this.onPointerUp(event));
    canvas.addEventListener("pointercancel", (event) => this.onPointerUp(event));
    // Peaks depend on the width in device pixels, so a rotation or a resize
    // recomputes them.
    if (typeof ResizeObserver === "function") {
      new ResizeObserver(() => this.resize()).observe(canvas);
    } else {
      window.addEventListener("resize", () => this.resize());
    }
    const scheme = window.matchMedia("(prefers-color-scheme: dark)");
    scheme.addEventListener("change", () => this.draw());
  }

  /** Register a listener: "change" fires on every move, "commit" when a drag ends. */
  on(name, listener) {
    if (!this.listeners.has(name)) this.listeners.set(name, []);
    this.listeners.get(name).push(listener);
  }

  emit(name) {
    const detail = { start: this.selection.start, end: this.selection.end };
    for (const listener of this.listeners.get(name) ?? []) listener(detail);
  }

  setAudio(channels, sampleRate, length, limits) {
    this.channels = channels;
    this.sampleRate = sampleRate;
    this.length = length;
    this.limits = limits;
    this.peaks = null;
    this.resize();
  }

  /**
   * Drop the audio. A ten-minute stereo clip is about 200 MB of samples, and
   * holding the old one while the next file decodes would need room for both.
   */
  clear() {
    this.channels = null;
    this.length = 0;
    this.peaks = null;
    this.selection = { start: 0, end: 0 };
  }

  setSelection(start, end) {
    this.selection = { start, end };
    this.draw();
  }

  setFades(fadeIn, fadeOut) {
    this.fades = { fadeIn, fadeOut };
    this.draw();
  }

  cssWidth() {
    return Math.max(1, Math.round(this.canvas.getBoundingClientRect().width));
  }

  resize() {
    const ratio = window.devicePixelRatio || 1;
    const width = this.cssWidth();
    const pixelWidth = Math.round(width * ratio);
    const pixelHeight = Math.round(HEIGHT_CSS_PX * ratio);
    if (this.canvas.width !== pixelWidth || this.canvas.height !== pixelHeight) {
      this.canvas.width = pixelWidth;
      this.canvas.height = pixelHeight;
    }
    // Drawing happens in CSS pixels; the transform maps them onto the
    // device pixels, so lines stay sharp on a Retina screen.
    this.context.setTransform(ratio, 0, 0, ratio, 0, 0);
    if (this.peaks === null || this.peakWidth !== width) this.computePeaks(width);
    this.draw();
  }

  // One min/max pair per CSS pixel column across all channels: one pass over
  // the samples, so a ten-minute file stays fast to redraw.
  computePeaks(width) {
    this.peakWidth = width;
    if (this.channels === null || this.length === 0) {
      this.peaks = null;
      return;
    }
    const mins = new Float32Array(width);
    const maxs = new Float32Array(width);
    const perColumn = this.length / width;
    for (let column = 0; column < width; column += 1) {
      const from = Math.floor(column * perColumn);
      const to = Math.max(from + 1, Math.min(this.length, Math.floor((column + 1) * perColumn)));
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
    this.peaks = { mins, maxs };
  }

  frameToX(frame, width) {
    return this.length === 0 ? 0 : (frame / this.length) * width;
  }

  xToFrame(x, width) {
    return (Math.min(Math.max(x, 0), width) / width) * this.length;
  }

  colours() {
    const style = getComputedStyle(this.canvas);
    const read = (name, fallback) => style.getPropertyValue(name).trim() || fallback;
    return {
      wave: read("--wave", "#0a64d8"),
      waveDim: read("--wave-dim", "#9aa5b1"),
      selection: read("--selection", "rgba(10, 100, 216, 0.12)"),
      fade: read("--fade", "rgba(0, 0, 0, 0.35)"),
      handle: read("--handle", "#d93025"),
    };
  }

  draw() {
    const ctx = this.context;
    const width = this.peakWidth || this.cssWidth();
    const height = HEIGHT_CSS_PX;
    ctx.clearRect(0, 0, width, height);
    if (this.peaks === null) return;
    const colours = this.colours();
    const startX = this.frameToX(this.selection.start, width);
    const endX = this.frameToX(this.selection.end, width);

    ctx.fillStyle = colours.selection;
    ctx.fillRect(startX, 0, endX - startX, height);

    const middle = height / 2;
    const scale = middle - 4;
    for (let column = 0; column < width; column += 1) {
      const inside = column + 0.5 >= startX && column + 0.5 <= endX;
      ctx.fillStyle = inside ? colours.wave : colours.waveDim;
      const top = middle - this.peaks.maxs[column] * scale;
      const bottom = middle - this.peaks.mins[column] * scale;
      ctx.fillRect(column, top, 1, Math.max(1, bottom - top));
    }

    this.drawFades(startX, endX, height, colours);
    this.drawHandle(startX, height, colours);
    this.drawHandle(endX, height, colours);
  }

  // The same quarter-sine gain the server's afade=qsin applies, from the same
  // function the preview uses: the shaded area above the curve is what the
  // fades take away.
  drawFades(startX, endX, height, colours) {
    const { fadeIn, fadeOut } = this.fades;
    if (fadeIn <= 0 && fadeOut <= 0) return;
    const duration = (this.selection.end - this.selection.start) / this.sampleRate;
    const span = endX - startX;
    if (span < 1 || duration <= 0) return;
    const ctx = this.context;
    ctx.beginPath();
    ctx.moveTo(startX, 0);
    const steps = Math.max(2, Math.ceil(span));
    for (let step = 0; step <= steps; step += 1) {
      const t = (step / steps) * duration;
      const gain = fadeGainAt(t, duration, fadeIn, fadeOut);
      ctx.lineTo(startX + (step / steps) * span, height * (1 - gain));
    }
    ctx.lineTo(endX, 0);
    ctx.closePath();
    ctx.fillStyle = colours.fade;
    ctx.fill();
  }

  drawHandle(x, height, colours) {
    const ctx = this.context;
    ctx.fillStyle = colours.handle;
    ctx.fillRect(x - HANDLE_DRAWN_CSS_PX / 2, 0, HANDLE_DRAWN_CSS_PX, height);
    // Grips at both ends make the handle visible as something to grab.
    ctx.beginPath();
    ctx.arc(x, GRIP_CSS_PX / 2, GRIP_CSS_PX / 2, 0, Math.PI * 2);
    ctx.arc(x, height - GRIP_CSS_PX / 2, GRIP_CSS_PX / 2, 0, Math.PI * 2);
    ctx.fill();
  }

  // Which part a press at x picks up: the nearer handle within reach, else
  // the inside of the selection (to move it whole), else nothing.
  hitTest(x, width) {
    const startX = this.frameToX(this.selection.start, width);
    const endX = this.frameToX(this.selection.end, width);
    const reach = HANDLE_HIT_CSS_PX / 2;
    const toStart = Math.abs(x - startX);
    const toEnd = Math.abs(x - endX);
    if (toStart <= reach || toEnd <= reach) {
      if (toStart === toEnd) return x < startX ? "start" : "end";
      return toStart < toEnd ? "start" : "end";
    }
    if (x > startX && x < endX) return "move";
    return null;
  }

  localX(event) {
    return event.clientX - this.canvas.getBoundingClientRect().left;
  }

  onPointerDown(event) {
    if (this.peaks === null || this.drag !== null) return;
    const width = this.cssWidth();
    const x = this.localX(event);
    const edge = this.hitTest(x, width);
    if (edge === null) return;
    event.preventDefault();
    // Capture keeps the drag alive when the finger leaves the canvas.
    this.canvas.setPointerCapture(event.pointerId);
    this.drag = {
      pointerId: event.pointerId,
      edge,
      // Where inside the selection the press landed, so a "move" drag does not
      // jump the selection's start to the finger.
      offset: this.xToFrame(x, width) - this.selection.start,
    };
  }

  onPointerMove(event) {
    if (this.drag === null || event.pointerId !== this.drag.pointerId) return;
    event.preventDefault();
    const width = this.cssWidth();
    let frame = this.xToFrame(this.localX(event), width);
    if (this.drag.edge === "move") frame -= this.drag.offset;
    const next = moveEdge(this.selection, this.drag.edge, frame, this.length,
      this.limits.maxFrames, this.limits.minFrames);
    if (next.start === this.selection.start && next.end === this.selection.end) return;
    this.selection = next;
    this.draw();
    this.emit("change");
  }

  onPointerUp(event) {
    if (this.drag === null || event.pointerId !== this.drag.pointerId) return;
    if (this.canvas.hasPointerCapture(event.pointerId)) {
      this.canvas.releasePointerCapture(event.pointerId);
    }
    this.drag = null;
    this.emit("commit");
  }
}
