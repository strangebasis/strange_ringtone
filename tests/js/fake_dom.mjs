// A minimal stand-in for the browser, enough to run core/static/app.js under
// node --test. The elements come from the real index.html template, so a
// renamed id or a changed nudge step reaches these tests too. Only what app.js,
// waveform.js and player.js touch is faked; anything else stays undefined, so
// a new dependency on the DOM shows up as a failing test rather than passing
// silently.
import { readFileSync } from "node:fs";

const TEMPLATE = new URL("../../core/static/index.html", import.meta.url);
const APP = new URL("../../core/static/app.js", import.meta.url);

const TAG_RE = /<([a-zA-Z][\w-]*)((?:\s+[^\s=>/]+(?:="[^"]*")?)*)\s*\/?>/g;
const ATTR_RE = /([^\s=]+)(?:="([^"]*)")?/g;

function camel(name) {
  return name.replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
}

// Anything that is called, read or set and is not part of what the tests
// look at: the canvas context and the AudioParams.
function sink() {
  return new Proxy(function noop() {}, {
    // Not thenable, so awaiting one by mistake fails fast instead of hanging.
    get: (target, key) => (key === "then" ? undefined : key === "value" ? 1 : key in target ? target[key] : sink()),
    set: () => true,
    apply: () => undefined,
  });
}

export class FakeElement {
  constructor(tag, attributes) {
    this.tagName = tag.toUpperCase();
    this.attributes = attributes;
    this.listeners = new Map();
    this.dataset = {};
    for (const [name, value] of Object.entries(attributes)) {
      if (name.startsWith("data-")) this.dataset[camel(name.slice(5))] = value;
    }
    this.id = attributes.id;
    this.className = attributes.class ?? "";
    this.type = attributes.type;
    this.hidden = "hidden" in attributes;
    this.disabled = "disabled" in attributes;
    this.checked = "checked" in attributes;
    this.min = attributes.min;
    this.max = attributes.max;
    this.step = attributes.step;
    this.textContent = "";
    this.files = null;
    this.width = 300;
    this.height = 150;
    this.classes = new Set(this.className.split(/\s+/).filter(Boolean));
    this.classList = { toggle: (name, on) => (on ? this.classes.add(name) : this.classes.delete(name)) };
    this.style = { setProperty: (name, value) => { this.style[name] = value; } };
    this.children = [];
    if ("href" in attributes) this.href = attributes.href;
    this.rawValue = attributes.value ?? (this.type === "range" ? String((Number(this.min) + Number(this.max)) / 2) : "");
  }

  get value() {
    return this.rawValue;
  }

  // A range input snaps what it is given to its step, half up, and clamps it
  // to [min, max], as browsers do; everything else keeps the string.
  set value(text) {
    if (this.type !== "range") {
      this.rawValue = String(text);
      return;
    }
    const min = Number(this.min);
    const max = Number(this.max);
    const step = Number(this.step);
    const steps = Math.round(Math.round(((Number(text) - min) / step) * 1e9) / 1e9);
    const snapped = Math.min(Math.max(min + steps * step, min), max);
    this.rawValue = String(Number(snapped.toFixed(10)));
  }

  addEventListener(type, listener) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(listener);
  }

  /** Call every listener synchronously; returns what they returned (promises from async ones). */
  dispatch(type, init = {}) {
    const event = { type, target: this, preventDefault() {}, ...init };
    return (this.listeners.get(type) ?? []).map((listener) => listener(event));
  }

  removeAttribute(name) {
    delete this[name];
  }

  closest(selector) {
    return selector === "button[data-edge]" && this.tagName === "BUTTON" && "edge" in this.dataset ? this : null;
  }

  querySelector(selector) {
    if (!selector.startsWith(".")) return null;
    const wanted = selector.slice(1);
    return this.children.find((child) => child.classes.has(wanted)) ?? null;
  }

  getContext() {
    return sink();
  }

  getBoundingClientRect() {
    return { left: 0, top: 0, width: 343, height: 160 };
  }

  setPointerCapture() {}
  hasPointerCapture() { return false; }
  releasePointerCapture() {}
}

/** Every element of the template, by id, plus the nudge container and its buttons. */
function buildDom() {
  const text = readFileSync(TEMPLATE, "utf8");
  const byId = new Map();
  const all = [];
  for (const [, tag, attrText] of text.matchAll(TAG_RE)) {
    const attributes = {};
    for (const [, name, value] of attrText.matchAll(ATTR_RE)) attributes[name] = value ?? "";
    const element = new FakeElement(tag, attributes);
    all.push(element);
    if (element.id !== undefined) byId.set(element.id, element);
  }
  // app.js only queries .nudges inside #app, and only reads the buttons inside it.
  byId.get("app").children = all.filter((element) => element.classes.has("nudges"));
  const nudges = all.filter((element) => element.tagName === "BUTTON" && "edge" in element.dataset);
  return { byId, nudges };
}

class FakeAudioBuffer {
  constructor(channels, sampleRate) {
    this.channels = channels;
    this.numberOfChannels = channels.length;
    this.sampleRate = sampleRate;
    this.length = channels[0].length;
    this.duration = this.length / sampleRate;
  }

  getChannelData(index) {
    return this.channels[index];
  }

  copyToChannel() {}
}

// An AudioParam that records what player.js schedules on it.
class FakeParam {
  constructor() {
    this.calls = [];
  }

  setValueAtTime(value, time) { this.calls.push({ method: "setValueAtTime", value, time }); }
  setTargetAtTime(value, time) { this.calls.push({ method: "setTargetAtTime", value, time }); }
  setValueCurveAtTime(curve, time, duration) { this.calls.push({ method: "setValueCurveAtTime", time, duration }); }
  cancelScheduledValues(time) { this.calls.push({ method: "cancelScheduledValues", time }); }

  /** The last value set or aimed at. */
  lastValue() {
    const set = this.calls.filter((call) => "value" in call);
    return set.length === 0 ? undefined : set[set.length - 1].value;
  }
}

class FakeSource {
  constructor() {
    this.buffer = null;
    this.loop = false;
    this.starts = [];
    this.stopped = false;
  }

  connect() {}
  disconnect() {}
  addEventListener() {}
  start(...args) { this.starts.push(args); }
  stop() { this.stopped = true; }
}

// Every AudioContext app.js creates, in order, for the tests to inspect.
let contexts = [];

class FakeAudioContext {
  constructor() {
    // Suspended until resumed, as a context made outside a gesture is on iOS.
    this.state = "suspended";
    this.resumes = 0;
    this.currentTime = 0;
    this.destination = sink();
    this.gains = [];
    this.sources = [];
    this.decoded = [];
    contexts.push(this);
  }

  resume() {
    this.resumes += 1;
    this.state = "running";
    return Promise.resolve();
  }

  createGain() {
    const node = { gain: new FakeParam(), connect() {}, disconnect() {} };
    this.gains.push(node);
    return node;
  }

  createBufferSource() {
    const source = new FakeSource();
    this.sources.push(source);
    return source;
  }

  createBuffer(count, length, sampleRate) {
    return new FakeAudioBuffer(Array.from({ length: count }, () => new Float32Array(length)), sampleRate);
  }

  // The rendered .m4a: its content does not matter to app.js.
  decodeAudioData() {
    const buffer = new FakeAudioBuffer([new Float32Array(44100)], 44100);
    this.decoded.push(buffer);
    return Promise.resolve(buffer);
  }
}

let instance = 0;
let nextDecoded = null;
const realSetInterval = globalThis.setInterval;
const realSetTimeout = globalThis.setTimeout;

class FakeOfflineAudioContext {
  constructor(count, length, sampleRate) {
    this.sampleRate = sampleRate;
  }

  decodeAudioData() {
    return Promise.resolve(nextDecoded);
  }
}

/**
 * Install fresh fakes and load a fresh copy of app.js (a new query string is
 * a new module instance, so no state leaks between tests).
 * @param {{maxClipSeconds?: string, defaultTarget?: string, fetchEnabled?: string, fetch?: Function,
 *   navigator?: object}} options
 */
export async function loadApp(options = {}) {
  const { byId, nudges } = buildDom();
  const app = byId.get("app");
  app.dataset.maxClipSeconds = options.maxClipSeconds ?? "30";
  app.dataset.defaultTargetLufs = options.defaultTarget ?? "-14.0";
  app.dataset.fetchEnabled = options.fetchEnabled ?? "false";
  app.dataset.resultTtlSeconds = options.resultTtlSeconds ?? "600";

  const fetchCalls = [];
  const documentListeners = new Map();
  globalThis.document = {
    hidden: false,
    getElementById: (id) => byId.get(id) ?? null,
    addEventListener(type, listener) {
      if (!documentListeners.has(type)) documentListeners.set(type, []);
      documentListeners.get(type).push(listener);
    },
  };
  contexts = [];
  // The player's pump timer must not keep node running when a test that
  // started playback fails before stopping it.
  globalThis.setInterval = (callback, ms) => {
    const timer = realSetInterval(callback, ms);
    timer.unref();
    return timer;
  };
  // A result's expiry timer runs for the server's TTL, minutes: it must not
  // keep node alive after a test either. Short timers (polls, sleeps) stay
  // as they are, so a test can wait on them.
  const timerDelays = [];
  globalThis.setTimeout = (callback, ms, ...args) => {
    timerDelays.push(ms);
    const timer = realSetTimeout(callback, ms, ...args);
    if (ms >= 60_000) timer.unref();
    return timer;
  };
  globalThis.window = { devicePixelRatio: 2, matchMedia: () => ({ addEventListener() {} }), addEventListener() {} };
  globalThis.getComputedStyle = () => ({ getPropertyValue: () => "" });
  globalThis.ResizeObserver = class { observe() {} };
  globalThis.Element = FakeElement;
  globalThis.AudioContext = FakeAudioContext;
  globalThis.OfflineAudioContext = FakeOfflineAudioContext;
  globalThis.fetch = (url, init) => {
    fetchCalls.push({ url, init });
    return options.fetch(url, init);
  };
  Object.defineProperty(globalThis, "navigator", {
    value: options.navigator ?? {},
    configurable: true,
    writable: true,
  });

  instance += 1;
  await import(`${APP.href}?instance=${instance}`);
  return {
    el: (id) => {
      const element = byId.get(id);
      if (element === undefined) throw new Error("no #" + id + " in the template");
      return element;
    },
    nudge: (edge, step) => {
      const button = nudges.find((b) => b.dataset.edge === edge && b.dataset.step === step);
      if (button === undefined) throw new Error(`no ${edge} ${step} nudge in the template`);
      return app.children[0].dispatch("click", { target: button });
    },
    /** Fire a document event, such as visibilitychange. */
    fireDocument: (type) => (documentListeners.get(type) ?? []).map((listener) => listener({ type })),
    /** Every delay the page asked setTimeout for, in order. */
    timerDelays: () => [...timerDelays],
    /** The AudioContexts app.js created so far. */
    contexts: () => [...contexts],
    fetchCalls,
  };
}

/** Pick a "file" whose decoded audio is these channels at 44100 Hz. */
export async function pickFile(page, name, channels) {
  nextDecoded = new FakeAudioBuffer(channels, 44100);
  const input = page.el("file");
  input.files = [{ name, arrayBuffer: () => Promise.resolve(new ArrayBuffer(8)) }];
  await Promise.all(input.dispatch("change"));
}

/** Let pending promise chains and timers that are already due run. */
export async function settle(rounds = 20) {
  for (let i = 0; i < rounds; i += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
}

export function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/** Frames and channels of a 16-bit PCM WAV with a 44-byte header. */
export function wavShape(bytes) {
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const channels = view.getUint16(22, true);
  return { channels, frames: (bytes.byteLength - 44) / (2 * channels) };
}
