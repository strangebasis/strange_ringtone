// The trimmer: pick a file or fetch a YouTube video, select the hook, preview
// it, render it and hand it to iOS. The page's configuration arrives in
// escaped data-* attributes, because the CSP allows no inline script, and
// every dynamic text goes through textContent, so a file name, a video title
// or a server message can never become markup.
import { decodeFile, MAX_INPUT_BYTES, MAX_INPUT_SECONDS } from "./audio/decode.js";
import { gainForTarget, integratedLufs } from "./audio/loudness.js";
import { displayName, renderQuery, toneNameHeader } from "./audio/request.js";
import { suggestionRule, suggestSelection, suggestWindowStart, windowHeatmap } from "./audio/suggest.js";
import { clampSelection, encodeWav } from "./audio/wav.js";
import {
  FetchError, fetchAudio, fetchErrorMessage, fetchJob, formatStartTime, parseStartTime, parseYouTubeLink, pollJob,
} from "./fetch.js";
import { Player } from "./player.js";
import { canShareFile, fetchResult, isCancel, render, resultUrl, shareFile, ShareError } from "./share.js";
import { moveEdge, Waveform } from "./waveform.js";

// The server accepts 44100 Hz PCM only, and the decoder resamples to it.
const SAMPLE_RATE = 44100;
// The server refuses a clip under half a second ("too_short").
const MIN_FRAMES = SAMPLE_RATE / 2;
// Past this the limiter works hard; the plan's spike compared -14, -11 and -9.
const LOUD_WARNING_LUFS = -11;
const TARGET_MIN_LUFS = -24;
const TARGET_MAX_LUFS = -9;
const METER_DEBOUNCE_MS = 200;
const FALLBACK_NAME = "ringtone";

function byId(id) {
  const element = document.getElementById(id);
  if (element === null) throw new Error("missing element #" + id);
  return element;
}

const app = byId("app");

// The server accepts a DEFAULT_TARGET_LUFS with any number of decimals, but the
// slider moves in 0.1 LU steps (step="0.1") and renderQuery sends one decimal.
// Left as it is, an off-step baseline such as -14.25 would be snapped by the
// browser to -14.2, so the slider could never sit on it: no "(baseline)"
// label, a Reset that never disables, and a marker away from the thumb.
// Rounding it once here, half up as the range input snaps, makes the baseline
// a value the slider can hold and the query can send.
function roundToStep(value) {
  return Math.round(value * 10) / 10;
}

function readConfig() {
  // MAX_CLIP_SECONDS is an integer string and DEFAULT_TARGET_LUFS a float
  // string such as "-14.0"; the server validated both at startup.
  const maxClipSeconds = Number.parseInt(app.dataset.maxClipSeconds ?? "", 10);
  // parseFloat: a blank attribute is NaN and falls back; Number("") would be 0.
  const defaultTarget = Number.parseFloat(app.dataset.defaultTargetLufs ?? "");
  const resultTtl = Number(app.dataset.resultTtlSeconds ?? "");
  return {
    maxClipSeconds: Number.isInteger(maxClipSeconds) && maxClipSeconds > 0 ? maxClipSeconds : 30,
    defaultTarget: Number.isFinite(defaultTarget)
      ? roundToStep(Math.min(Math.max(defaultTarget, TARGET_MIN_LUFS), TARGET_MAX_LUFS))
      : -14,
    fetchEnabled: app.dataset.fetchEnabled === "true",
    // How long the server keeps a finished tone (RESULT_TTL_SECONDS).
    resultTtlSeconds: Number.isFinite(resultTtl) && resultTtl > 0 ? resultTtl : 600,
  };
}

const config = readConfig();
// Exactly the server's cap, in frames: no margin is needed (plan 7.1).
const MAX_FRAMES = config.maxClipSeconds * SAMPLE_RATE;

const el = {
  maxClipSeconds: byId("max-clip-seconds"),
  youtube: byId("youtube"),
  ytLink: byId("yt-link"),
  ytFetch: byId("yt-fetch"),
  ytStartRow: byId("yt-start-row"),
  ytStart: byId("yt-start"),
  ytStatus: byId("yt-status"),
  file: byId("file"),
  fileStatus: byId("file-status"),
  editor: byId("editor"),
  waveform: byId("waveform"),
  selectionReadout: byId("selection-readout"),
  nudges: app.querySelector(".nudges"),
  play: byId("play"),
  fadeInOn: byId("fade-in-on"),
  fadeIn: byId("fade-in"),
  fadeInValue: byId("fade-in-value"),
  fadeOutOn: byId("fade-out-on"),
  fadeOut: byId("fade-out"),
  fadeOutValue: byId("fade-out-value"),
  normalize: byId("normalize"),
  target: byId("target"),
  targetMarker: byId("target-marker"),
  targetBaseline: byId("target-baseline"),
  targetValue: byId("target-value"),
  targetReset: byId("target-reset"),
  meterReadout: byId("meter-readout"),
  targetWarning: byId("target-warning"),
  name: byId("name"),
  final: byId("final"),
  render: byId("render"),
  renderStatus: byId("render-status"),
  resultInfo: byId("result-info"),
  resultDuration: byId("result-duration"),
  resultLufs: byId("result-lufs"),
  resultGain: byId("result-gain"),
  deliver: byId("deliver"),
  share: byId("share"),
  fallback: byId("fallback"),
  downloadM4r: byId("download-m4r"),
  shareStatus: byId("share-status"),
};

const state = {
  audio: null,
  fileName: "",
  selection: { start: 0, end: 0 },
  measured: null,
  // Bumped by every change that would make a rendered result differ, so a
  // render that finishes after a change is not mistaken for the current one.
  generation: 0,
  result: null,
  // Forgets the result's downloads when the server's TTL has passed.
  resultTimer: null,
  busy: false,
  // The generation whose render is in flight, so a render abandoned by a new
  // file pick does not keep the new file's Hear final greyed out.
  renderToken: null,
  playing: null,
  // Bumped by every new file pick or fetch: whichever came last wins, and an
  // older one's result is dropped when it arrives.
  decodeToken: 0,
  // Stops the polling of a fetch that a newer pick or fetch replaced.
  fetchController: null,
  // The link the fetch in flight is for: its video, so a second tap on Fetch
  // for the same video does not open a second job (and spend a second
  // rate-limit slot), and the timestamp the link carries now. Such a tap
  // may bring another timestamp, and the fetch reads this when its answer
  // arrives, so the link in the box is the one that counts.
  fetchLink: null,
  // The second the window on its way starts at; null while the fetch in
  // flight is a look-up or a whole download. A tap for another part of the
  // same video replaces the fetch, a tap for the same part does not.
  fetchStart: null,
  // The video the server last called long (docs/PLAN.md 6.2): its ID, length,
  // title and heatmap, the timestamp of the link the start field was filled
  // for, what the page put in the field, and why. While it is set, Fetch for
  // the same video fetches the window the field names.
  longVideo: null,
};

let audioContext = null;
let player = null;
let meterTimer = null;

const waveform = new Waveform(el.waveform);

// --- formatting -----------------------------------------------------------------

function formatTime(seconds) {
  const tenths = Math.round(seconds * 10);
  const minutes = Math.floor(tenths / 600);
  const rest = (tenths - minutes * 600) / 10;
  return minutes + ":" + rest.toFixed(1).padStart(4, "0");
}

function formatLufs(value) {
  return Number.isFinite(value) ? value.toFixed(1) + " LUFS" : "silence";
}

function formatDb(value) {
  const rounded = Math.round(value * 10) / 10;
  return (rounded > 0 ? "+" : rounded < 0 ? "-" : "") + Math.abs(rounded).toFixed(1) + " dB";
}

function setStatus(element, text, isError = false) {
  element.textContent = text;
  element.classList.toggle("error", isError);
}

// --- settings read from the controls -------------------------------------------------

function fadeSettings() {
  return {
    fadeIn: el.fadeInOn.checked ? Number(el.fadeIn.value) : 0,
    fadeOut: el.fadeOutOn.checked ? Number(el.fadeOut.value) : 0,
  };
}

function targetLufs() {
  return Number(el.target.value);
}

function previewGainDb() {
  if (!el.normalize.checked || state.measured === null) return 0;
  return gainForTarget(state.measured, targetLufs());
}

function previewOptions() {
  return { ...fadeSettings(), gainDb: previewGainDb() };
}

// --- playback -------------------------------------------------------------------

// One AudioContext for the page, created and resumed inside the first click
// that needs it: iOS starts a context only from a user gesture, and caps how
// many a page may hold.
function ensurePlayer() {
  if (audioContext === null) {
    audioContext = new AudioContext();
    player = new Player(audioContext);
  }
  if (audioContext.state !== "running") {
    audioContext.resume().catch(() => {
      setStatus(el.renderStatus, "This browser would not start audio playback.", true);
    });
  }
  return player;
}

function selectionBuffer() {
  const { start, end } = state.selection;
  const channels = state.audio.channels;
  const buffer = audioContext.createBuffer(channels.length, end - start, state.audio.sampleRate);
  channels.forEach((data, index) => buffer.copyToChannel(data.subarray(start, end), index));
  return buffer;
}

function startPreview() {
  const buffer = selectionBuffer();
  player.play(buffer, 0, buffer.duration, previewOptions());
  state.playing = "preview";
  updateButtons();
}

function playFinal() {
  if (state.result === null || state.result.buffer === null) return;
  player.playPlain(state.result.buffer);
  state.playing = "final";
  updateButtons();
}

function stopPlayback() {
  if (player !== null) player.stop();
  state.playing = null;
  updateButtons();
}

function updateButtons() {
  el.play.textContent = state.playing === "preview" ? "Stop" : "Play selection";
  el.play.disabled = state.audio === null;
  if (state.busy) {
    el.render.textContent = "Rendering…";
  } else {
    el.render.textContent = state.playing === "final" ? "Stop final" : "Hear final";
  }
  el.render.disabled = state.busy || state.audio === null;
}

// --- the result -------------------------------------------------------------------

function clearResult() {
  if (state.playing === "final") stopPlayback();
  if (state.resultTimer !== null) {
    clearTimeout(state.resultTimer);
    state.resultTimer = null;
  }
  state.result = null;
  el.resultInfo.hidden = true;
  el.deliver.hidden = true;
  el.share.disabled = true;
  el.fallback.hidden = true;
  el.fallback.removeAttribute("href");
  el.downloadM4r.removeAttribute("href");
  setStatus(el.shareStatus, "");
}

// The server has deleted the tone by now, so the download links would only
// fail. The file and its decoded buffer are still here: the share sheet and
// playback keep working, and Hear final makes the downloads again.
function expireResult() {
  state.resultTimer = null;
  if (state.result === null || state.result.expired) return;
  state.result.expired = true;
  el.downloadM4r.hidden = true;
  el.downloadM4r.removeAttribute("href");
  el.fallback.hidden = true;
  el.fallback.removeAttribute("href");
  setStatus(el.renderStatus, "The download links have expired on the server. Tap Hear final to make the tone again.");
}

// A timer's delay is a 32-bit count of milliseconds: a longer TTL (legal for
// RESULT_TTL_SECONDS) would wrap and fire at once, so it is waited in slices.
const MAX_TIMER_MS = 2 ** 31 - 1;

function scheduleExpiry(expiresAt) {
  const remaining = expiresAt - Date.now();
  if (remaining <= 0) {
    expireResult();
    return;
  }
  state.resultTimer = setTimeout(() => {
    state.resultTimer = null;
    scheduleExpiry(expiresAt);
  }, Math.min(remaining, MAX_TIMER_MS));
}

// Anything that changes what a render would produce.
function invalidate() {
  state.generation += 1;
  if (state.result !== null) {
    clearResult();
    setStatus(el.renderStatus, "Changed since the last render. Tap Hear final to render it again.");
  }
}

function showInfo(info) {
  el.resultDuration.textContent = info.duration.toFixed(2) + " s";
  el.resultLufs.textContent = formatLufs(info.measured_lufs);
  el.resultGain.textContent = formatDb(info.applied_gain_db);
  el.resultInfo.hidden = false;
}

function showDeliver(result) {
  el.downloadM4r.href = resultUrl(result.id, "m4r");
  el.downloadM4r.hidden = false;
  el.fallback.href = resultUrl(result.id, "m4a");
  const available = canShareFile(result.file);
  // The button only appears where the share sheet can take this very file;
  // elsewhere the plain download link stands in for it.
  el.share.hidden = !available;
  el.share.disabled = !available;
  el.fallback.hidden = available;
  setStatus(el.shareStatus, available ? "Share sheet: available" : "Share sheet: not available");
  el.deliver.hidden = false;
}

function showFallback(text) {
  el.fallback.hidden = false;
  setStatus(el.shareStatus, text, true);
}

async function renderFinal() {
  if (state.busy || state.audio === null) return;
  stopPlayback();
  clearResult();
  const generation = state.generation;
  const audio = state.audio;
  const { start, end } = state.selection;
  // The server's TTL runs from when it stored the result, which is before the
  // page has fetched and decoded it; counting from here errs on the early side.
  const startedAt = Date.now();
  state.busy = true;
  state.renderToken = generation;
  updateButtons();
  setStatus(el.renderStatus, "Rendering…");
  try {
    const wav = encodeWav(state.audio.channels, state.audio.sampleRate, start, end);
    const query = renderQuery({ ...fadeSettings(), normalize: el.normalize.checked, targetLufs: targetLufs() });
    const info = await render(wav, query, toneNameHeader(el.name.value));
    if (generation !== state.generation) return;
    showInfo(info);
    setStatus(el.renderStatus, "Fetching the finished tone…");
    const { bytes, name } = await fetchResult(info.result_id);
    if (generation !== state.generation) return;
    const stem = name ?? (displayName(state.fileName) || FALLBACK_NAME);
    const file = new File([bytes], stem + ".m4a", { type: "audio/mp4" });
    let buffer = null;
    try {
      // decodeAudioData detaches the buffer it is given, and the bytes are
      // still needed for the share sheet, so it gets a copy.
      buffer = await audioContext.decodeAudioData(bytes.slice().buffer);
    } catch {
      buffer = null;
    }
    if (generation !== state.generation) return;
    state.result = { id: info.result_id, info, file, buffer, expired: false };
    scheduleExpiry(startedAt + config.resultTtlSeconds * 1000);
    showDeliver(state.result);
    if (buffer === null) {
      setStatus(el.renderStatus, "The tone is ready, but this browser could not play it. You can still save it.", true);
    } else if (document.hidden) {
      // The page was put away while the tone was being made: a loop that
      // starts in a background tab has no visibility change left to stop it.
      setStatus(el.renderStatus, "The tone is ready. Tap Hear final to play it.");
    } else {
      setStatus(el.renderStatus, "Playing the finished tone on a loop.");
      playFinal();
    }
  } catch (error) {
    if (generation !== state.generation) return;
    el.resultInfo.hidden = true;
    const text = error instanceof ShareError ? error.message : "The clip could not be prepared for the server.";
    setStatus(el.renderStatus, text, true);
  } finally {
    // A new file pick already freed the button for its own clip.
    if (state.renderToken === generation) {
      state.busy = false;
      state.renderToken = null;
    }
    updateButtons();
    // A setting changed under this render, so its result was dropped. A new
    // file (state.audio differs) has its own status line and says nothing here.
    if (generation !== state.generation && state.result === null && state.audio === audio) {
      // invalidate() only clears a finished result, and this render never
      // became one, so its numbers would otherwise stay up describing a
      // selection or setting that has since changed.
      el.resultInfo.hidden = true;
      setStatus(el.renderStatus, "Changed while rendering. Tap Hear final to render it again.");
    }
  }
}

// --- loudness ---------------------------------------------------------------------

// The previous measurement stays in use until the new one is ready, so the
// preview's level does not jump while a handle is dragged.
function scheduleMeter() {
  if (meterTimer !== null) clearTimeout(meterTimer);
  meterTimer = setTimeout(measureSelection, METER_DEBOUNCE_MS);
}

// The meter reads the raw selection, without fades or gain, as the server's
// first pass does, so the preview gain is the gain the server will apply.
function measureSelection() {
  meterTimer = null;
  if (state.audio === null) return;
  const { start, end } = state.selection;
  const views = state.audio.channels.map((data) => data.subarray(start, end));
  state.measured = integratedLufs(views, state.audio.sampleRate);
  updateLoudness();
}

function updateLoudness() {
  const target = targetLufs();
  const baseline = target === config.defaultTarget;
  el.targetValue.textContent = target.toFixed(1) + " LUFS" + (baseline ? " (baseline)" : "");
  el.target.disabled = !el.normalize.checked;
  el.targetReset.disabled = !el.normalize.checked || baseline;
  el.targetWarning.hidden = !(el.normalize.checked && target > LOUD_WARNING_LUFS);
  if (!el.normalize.checked) {
    el.meterReadout.textContent = "Levelling is off: the tone keeps the clip's own loudness.";
  } else if (state.measured === null) {
    el.meterReadout.textContent = "Measuring the selection…";
  } else {
    el.meterReadout.textContent = "The selection measures " + formatLufs(state.measured)
      + ", so the preview plays it at " + formatDb(previewGainDb()) + ".";
  }
  if (player !== null) player.update({ gainDb: previewGainDb() });
}

// --- selection --------------------------------------------------------------------

function updateSelectionReadout() {
  const { start, end } = state.selection;
  const rate = state.audio.sampleRate;
  const frames = end - start;
  const capped = frames === MAX_FRAMES ? " (the maximum)" : "";
  el.selectionReadout.textContent = formatTime(start / rate) + " to " + formatTime(end / rate)
    + ", " + (frames / rate).toFixed(1) + " s" + capped;
}

function applySelection(selection) {
  // clampSelection is the last word on the bounds the server enforces, so
  // whatever moved the selection, the WAV it produces is accepted.
  const [start, end] = clampSelection(selection.start, selection.end, state.audio.length, MAX_FRAMES, MIN_FRAMES);
  const changed = start !== state.selection.start || end !== state.selection.end;
  state.selection = { start, end };
  return changed;
}

function selectionChanged() {
  updateSelectionReadout();
  invalidate();
  scheduleMeter();
}

function selectionCommitted() {
  if (state.playing === "preview") startPreview();
}

function nudge(edge, seconds) {
  const frame = state.selection[edge] + Math.round(seconds * state.audio.sampleRate);
  const moved = moveEdge(state.selection, edge, frame, state.audio.length, MAX_FRAMES, MIN_FRAMES);
  if (!applySelection(moved)) return;
  waveform.setSelection(state.selection.start, state.selection.end);
  selectionChanged();
  selectionCommitted();
}

waveform.on("change", (selection) => {
  applySelection(selection);
  if (selection.start !== state.selection.start || selection.end !== state.selection.end) {
    waveform.setSelection(state.selection.start, state.selection.end);
  }
  selectionChanged();
});
waveform.on("commit", selectionCommitted);

el.nudges.addEventListener("click", (event) => {
  const button = event.target instanceof Element ? event.target.closest("button[data-edge]") : null;
  if (button === null || state.audio === null) return;
  const edge = button.dataset.edge;
  const step = Number(button.dataset.step);
  if ((edge === "start" || edge === "end") && Number.isFinite(step)) nudge(edge, step);
});

// --- file -------------------------------------------------------------------------

function decodeErrorMessage(error) {
  // decode.js throws a plain Error with a message meant for people (a file
  // that is too long); the browser's own decode failures are DOMExceptions
  // whose messages are not.
  if (error instanceof Error && error.name === "Error" && error.message) return error.message;
  return "This browser could not read that file. Try an MP3, M4A or WAV file.";
}

function resetForNewFile() {
  stopPlayback();
  clearResult();
  state.audio = null;
  // A render still running for the old clip is abandoned (its generation is
  // behind), so it must not keep this clip's Hear final greyed out.
  state.busy = false;
  state.renderToken = null;
  // The waveform held the old clip's samples too; the next file decodes
  // into memory of its own, so the old one must go first.
  waveform.clear();
  state.measured = null;
  state.generation += 1;
  el.editor.hidden = true;
  el.final.hidden = true;
  setStatus(el.renderStatus, "");
  updateLoudness();
  updateButtons();
}

function fileHint() {
  return "Any audio file up to " + Math.round(MAX_INPUT_SECONDS / 60) + " minutes long and "
    + MAX_INPUT_BYTES / (1024 * 1024) + " MB. It is decoded on this device; only the selection is sent.";
}

// What placed the selection (docs/PLAN.md 7.2), in the status line's words.
const RULE_WORDS = {
  timestamp: "The selection starts at the link's timestamp",
  // Rule 1 again, for a start the user typed into the start field.
  chosen: "The selection starts at the time you chose",
  heatmap: "The selection is on the most replayed stretch",
  loudness: "The selection starts on the loudest stretch",
  whole: "The whole clip is selected",
};

// A new pick or fetch replaces whatever was loading: the token makes an
// older decode's result stale, and the abort stops an older fetch's polling.
function supersede() {
  state.decodeToken += 1;
  if (state.fetchController !== null) {
    state.fetchController.abort();
    state.fetchController = null;
  }
  return state.decodeToken;
}

/**
 * Decode a picked or fetched file and open the editor on it. Shared by the
 * file picker and the YouTube fetch, so both get the same checks.
 * @param {File} file
 * @param {number} token from supersede(); a newer one drops this result
 * @param {{status: object, label: string, name: string, heatmap?: unknown, timestamp?: number | null,
 *   windowStart?: number | null, timestampWords?: string | null, after?: string}} source
 *   where to report, what to call the clip there, the name field's value,
 *   and the fetcher's hints for the suggestion. For a window of a longer
 *   video, windowStart is where it begins in the video: the heatmap arrives
 *   in the video's time and is moved to the window's before any rule reads
 *   it. timestampWords replaces rule 1's words, and after is added to the
 *   status line.
 */
async function loadAudio(file, token, {
  status, label, name, heatmap = null, timestamp = null, windowStart = null, timestampWords = null, after = "",
}) {
  let audio;
  try {
    audio = await decodeFile(file, SAMPLE_RATE);
  } catch (error) {
    if (token === state.decodeToken) setStatus(status, decodeErrorMessage(error), true);
    return;
  }
  if (token !== state.decodeToken) return;
  if (audio.length < MIN_FRAMES) {
    setStatus(status, "This clip is shorter than half a second, too short for a ringtone.", true);
    return;
  }
  state.audio = audio;
  state.fileName = file.name;
  el.name.value = name;

  const duration = audio.length / audio.sampleRate;
  const options = {
    heatmap: windowStart === null ? heatmap : windowHeatmap(heatmap, windowStart, duration),
    timestamp,
  };
  const suggestion = suggestSelection(audio.channels, audio.sampleRate, config.maxClipSeconds, options);
  state.selection = { start: -1, end: -1 };
  applySelection({
    start: Math.round(suggestion.start * audio.sampleRate),
    end: Math.round(suggestion.end * audio.sampleRate),
  });

  el.editor.hidden = false;
  el.final.hidden = false;
  // Set after the editor is shown, so the canvas has a width to draw into.
  waveform.setAudio(audio.channels, audio.sampleRate, audio.length, { maxFrames: MAX_FRAMES, minFrames: MIN_FRAMES });
  waveform.setSelection(state.selection.start, state.selection.end);
  waveform.setFades(fadeSettings().fadeIn, fadeSettings().fadeOut);
  // suggestSelection takes a clip no longer than the cap whole, from 0, before
  // any rule looks at it, so naming the t= or the heatmap there would be wrong.
  const rule = duration <= config.maxClipSeconds ? "whole" : suggestionRule(duration, options);
  const words = rule === "timestamp" && timestampWords !== null ? timestampWords : RULE_WORDS[rule];
  setStatus(status, label + ", " + formatTime(duration) + ". " + words + "; drag the handles to change it." + after);
  setStatus(el.renderStatus, "");
  updateButtons();
  selectionChanged();
}

el.file.addEventListener("change", async () => {
  const file = el.file.files !== null && el.file.files.length > 0 ? el.file.files[0] : null;
  if (file === null) return;
  resetForNewFile();
  // A second pick while the first file still decodes wins, and so does a
  // pick while a fetch runs.
  const token = supersede();
  setStatus(el.ytStatus, "");
  // The YouTube box starts over too: a long video it had asked about is no
  // longer what the next Fetch is for.
  hideStartRow();
  setStatus(el.fileStatus, "Reading the file…");
  await loadAudio(file, token, { status: el.fileStatus, label: file.name, name: displayName(file.name) });
});

// --- YouTube ----------------------------------------------------------------------

const FETCH_STATES = {
  queued: "Waiting in the queue…",
  running: "The server is downloading it…",
};

// The most of a longer video the server fetches in one go. FETCH_MAX_SECONDS
// may be set lower, never higher; the default start is as good a place for a
// shorter window.
const WINDOW_SECONDS = MAX_INPUT_SECONDS;

function hideStartRow() {
  state.longVideo = null;
  el.ytStartRow.hidden = true;
  el.ytStart.value = "";
}

/**
 * Show the start field for a video the server called long, filled in.
 * @param {{videoId: string, duration: number, title: string | null, heatmap: unknown}} video
 * @param {number | null} timestamp the timestamp of the link the field is filled for
 * @param {number} start whole seconds
 * @param {"link" | "replayed" | "zero"} source what the start is: the link's
 *   own timestamp, the most replayed ten minutes, or the beginning
 */
function showStartRow(video, timestamp, start, source) {
  state.longVideo = {
    videoId: video.videoId, duration: video.duration, title: video.title, heatmap: video.heatmap,
    timestamp, filled: formatStartTime(start), source,
  };
  el.ytStart.value = state.longVideo.filled;
  el.ytStartRow.hidden = false;
}

/**
 * Fill the start field for a long video and the link that names it, and say
 * what to fetch next. A link with a timestamp inside the video is fetched
 * from there at once. Otherwise the field gets the most replayed ten minutes
 * (the beginning, without a heatmap), the status line asks, and nothing is
 * downloaded until the user has seen the start and tapped Fetch again.
 * @param {{videoId: string, duration: number, title: string | null, heatmap: unknown}} video
 * @param {{videoId: string, timestamp: number | null}} link
 * @returns {{start: number, source: string} | null} the window to fetch now;
 *   null once the user has been asked
 */
function offerStart(video, link) {
  if (link.timestamp !== null && link.timestamp < video.duration) {
    showStartRow(video, link.timestamp, link.timestamp, "link");
    return { start: link.timestamp, source: "link" };
  }
  const replayed = suggestWindowStart(video.heatmap, video.duration, WINDOW_SECONDS);
  showStartRow(video, link.timestamp, replayed ?? 0, replayed === null ? "zero" : "replayed");
  setStatus(el.ytStatus, "This video is " + formatStartTime(video.duration) + " long, and the server fetches"
    + " up to 10 minutes of it at a time. The start time is set to "
    + (replayed === null ? "the beginning" : "the most replayed part")
    + ". Change it if you like, then tap Fetch.");
  return null;
}

/**
 * One job at the fetcher, from the request to its last status.
 * @returns {Promise<object | null>} pollJob's result with the job's ID; null
 *   when a newer pick or fetch replaced this one meanwhile
 */
async function runJob(videoId, start, controller, current) {
  const jobId = await fetchJob(videoId, start);
  if (!current()) return null;
  const result = await pollJob(jobId, {
    signal: controller.signal,
    onStatus: (status) => {
      if (current()) setStatus(el.ytStatus, FETCH_STATES[status]);
    },
  });
  return current() ? { ...result, jobId } : null;
}

/**
 * Fetch a YouTube video's audio through the fetcher and load it like a picked
 * file. Only the ID parsed on this device is sent. The t= timestamp stays here
 * and only moves the selection, unless the video is longer than the server
 * fetches whole: then the server says so, and a second request carries the
 * ID and a start time, the link's own or the one in the start field, which
 * the user sees and may change before anything is downloaded
 * (docs/PLAN.md 6.2).
 * @param {string} text a pasted or shared link
 */
async function fetchFromYouTube(text) {
  // The box is hidden then too; this keeps any other path from reaching a
  // fetcher the server did not announce.
  if (!config.fetchEnabled) return;
  const link = parseYouTubeLink(text);
  if (link === null) {
    setStatus(el.ytStatus, fetchErrorMessage("bad_link"), true);
    return;
  }
  // Whether a fetch for this very video is on its way.
  const running = state.fetchController !== null && state.fetchLink !== null
    && state.fetchLink.videoId === link.videoId;
  const known = state.longVideo;
  if (known === null || known.videoId !== link.videoId) {
    if (running) {
      // The same video is already on its way: a second tap would only queue
      // a second job that nobody collects. Only the timestamp can differ,
      // and the fetch on its way takes it from here when its answer
      // arrives. A different video still replaces the fetch.
      state.fetchLink = link;
      return;
    }
    hideStartRow();
    await runFetch(link, null);
    return;
  }
  // The server has already called this video long, and the start field is
  // showing: this tap fetches the part the field names.
  const untouched = el.ytStart.value.trim() === known.filled;
  let asked;
  if (untouched && link.timestamp !== known.timestamp) {
    // The same video under another link: a timestamp was added, changed or
    // taken away since the field was filled, and nobody has typed in the
    // field. So it is filled again for this link, as when the server first
    // called the video long; fetching the old start again, as if this link
    // had named it, would be wrong. A time the user typed stays (below).
    asked = offerStart(known, link);
    if (asked === null) {
      // Asked first, so a part of this video that is on its way, or that
      // has arrived and is still being decoded, is no longer wanted: it
      // would only replace the question. Nothing else can be in flight
      // here: picking a file takes the start field away.
      supersede();
      return;
    }
  } else {
    const start = parseStartTime(el.ytStart.value);
    if (start === null) {
      setStatus(el.ytStatus, fetchErrorMessage("bad_time"), true);
      return;
    }
    if (start >= known.duration) {
      setStatus(el.ytStatus, fetchErrorMessage("past_end"), true);
      return;
    }
    // The field as the page filled it keeps its reason; anything else in it
    // is the user's own choice.
    asked = { start, source: untouched ? known.source : "typed" };
  }
  // That very part is already on its way: a second tap would only queue a
  // second job. Another part replaces it, as another link would.
  if (running && state.fetchStart === asked.start) return;
  await runFetch(link, asked);
}

/**
 * @param {{videoId: string, timestamp: number | null}} link
 * @param {{start: number, source: string} | null} asked the window to fetch
 *   of a video already known to be long; null to find out first
 */
async function runFetch(link, asked) {
  resetForNewFile();
  const token = supersede();
  const controller = new AbortController();
  state.fetchController = controller;
  state.fetchLink = link;
  state.fetchStart = asked === null ? null : asked.start;
  const current = () => token === state.decodeToken;
  // The link as it is now: a tap on the same video may have changed its
  // timestamp while this fetch waited (fetchFromYouTube). Read only while
  // this fetch is the current one.
  const latest = () => state.fetchLink;
  // The picker's line goes back to its hint; a file picked earlier is no
  // longer what the editor shows.
  setStatus(el.fileStatus, fileHint());
  el.file.value = "";
  try {
    let chosen = asked;
    let done;
    if (chosen === null) {
      setStatus(el.ytStatus, "Fetching from YouTube…");
      done = await runJob(link.videoId, null, controller, current);
      if (done === null) return;
      if (done.status === "long") {
        const video = { videoId: link.videoId, duration: done.duration, title: done.title, heatmap: done.heatmap };
        // The link's own timestamp is fetched without asking, and shown in
        // the field for the next part; without one the user is asked first.
        chosen = offerStart(video, latest());
        if (chosen === null) {
          state.fetchController = null;
          return;
        }
        state.fetchStart = chosen.start;
      }
    }
    if (chosen !== null) {
      setStatus(el.ytStatus, "Fetching up to 10 minutes from " + formatStartTime(chosen.start) + "…");
      done = await runJob(link.videoId, chosen.start, controller, current);
      if (done === null) return;
      // A window is done and says where it begins; anything else is no answer to this request.
      if (done.status !== "done" || done.windowStart === null) throw new FetchError("bad_response");
    }
    setStatus(el.ytStatus, "Fetching the audio…");
    const bytes = await fetchAudio(done.jobId);
    if (!current()) return;
    state.fetchController = null;
    const title = done.title;
    // Decoded from memory like a picked file. The title is only the File's
    // name here and the name field's value; it never goes into a URL.
    const file = new File([bytes], (title ?? "youtube") + ".m4a", { type: "audio/mp4" });
    setStatus(el.ytStatus, "Reading the audio…");
    const source = {
      status: el.ytStatus,
      label: title ?? "The video",
      // The title itself: displayName would cut a last ".word" off it as if
      // it were a file extension. An empty name lets the server choose one.
      name: title ?? "",
      heatmap: done.heatmap,
      timestamp: latest().timestamp,
    };
    if (chosen !== null) {
      // The window begins with the fragment that holds the start, so a
      // little before it: the label says where, and a start the link or
      // the user named is where the selection begins (rule 1), counted
      // from the window's own beginning.
      const named = chosen.source === "link" || chosen.source === "typed";
      source.label = (title ?? "The video") + " from " + formatStartTime(done.windowStart);
      source.windowStart = done.windowStart;
      source.timestamp = named ? Math.max(0, chosen.start - done.windowStart) : null;
      source.timestampWords = chosen.source === "typed" ? RULE_WORDS.chosen : null;
      source.after = " For another part of the video, change the start time and tap Fetch.";
    }
    await loadAudio(file, token, source);
  } catch (error) {
    if (!current()) return;
    state.fetchController = null;
    setStatus(el.ytStatus, error instanceof FetchError ? error.message : fetchErrorMessage("failed"), true);
  }
}

el.ytFetch.addEventListener("click", () => {
  void fetchFromYouTube(el.ytLink.value);
});

for (const field of [el.ytLink, el.ytStart]) {
  field.addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    void fetchFromYouTube(el.ytLink.value);
  });
}

/**
 * The link a "Make Ringtone" Shortcut put in the address as #yt=<URL-encoded
 * link> (docs/PLAN.md 7.4), read once. A fragment is never sent to the
 * server, so no proxy log sees it; it is removed from the address and the
 * history entry before any request, so a reload or a copied address does not
 * carry it on.
 * @returns {string | null}
 */
function takeSharedLink() {
  const hash = location.hash;
  if (typeof hash !== "string" || !hash.startsWith("#yt=")) return null;
  history.replaceState(null, "", location.pathname + location.search);
  try {
    return decodeURIComponent(hash.slice(4));
  } catch {
    // Broken %-escapes: parsed as it is, which fails with plain words.
    return hash.slice(4);
  }
}

/** Fetch the #yt= link, if the address has one; at start-up and on hashchange. */
function useSharedLink() {
  const shared = takeSharedLink();
  if (shared === null) return;
  if (config.fetchEnabled) {
    el.ytLink.value = shared;
    void fetchFromYouTube(shared);
  } else {
    setStatus(el.fileStatus, "YouTube fetching is not enabled on this server. Pick an audio file instead.", true);
  }
}

// --- controls ---------------------------------------------------------------------

function updateFades() {
  el.fadeIn.disabled = !el.fadeInOn.checked;
  el.fadeOut.disabled = !el.fadeOutOn.checked;
  el.fadeInValue.textContent = Number(el.fadeIn.value).toFixed(1) + " s";
  el.fadeOutValue.textContent = Number(el.fadeOut.value).toFixed(1) + " s";
  const { fadeIn, fadeOut } = fadeSettings();
  waveform.setFades(fadeIn, fadeOut);
  if (player !== null) player.update({ fadeIn, fadeOut });
}

for (const control of [el.fadeInOn, el.fadeIn, el.fadeOutOn, el.fadeOut]) {
  control.addEventListener("input", () => {
    updateFades();
    invalidate();
  });
}

for (const control of [el.normalize, el.target]) {
  control.addEventListener("input", () => {
    updateLoudness();
    invalidate();
  });
}

el.targetReset.addEventListener("click", () => {
  el.target.value = String(config.defaultTarget);
  updateLoudness();
  invalidate();
});

el.name.addEventListener("input", invalidate);

el.play.addEventListener("click", () => {
  if (state.audio === null) return;
  ensurePlayer();
  if (state.playing === "preview") {
    stopPlayback();
    return;
  }
  startPreview();
});

el.render.addEventListener("click", () => {
  if (state.audio === null) return;
  // Created here, inside the click, before any await.
  ensurePlayer();
  if (state.playing === "final") {
    stopPlayback();
    return;
  }
  // An expired result still plays, but Hear final then makes the downloads
  // again rather than replaying what the server no longer has.
  if (state.result !== null && state.result.buffer !== null && !state.result.expired) {
    playFinal();
    return;
  }
  void renderFinal();
});

// Deliberately not async: navigator.share must run inside this click's user
// activation, so nothing may be awaited before it.
el.share.addEventListener("click", () => {
  const result = state.result;
  if (result === null) return;
  let shared;
  try {
    shared = shareFile(result.file);
  } catch (error) {
    showFallback("Share sheet: failed (" + String(error && error.name) + "). Use Download .m4a instead.");
    return;
  }
  shared.then(
    () => setStatus(el.shareStatus, "Share sheet: available"),
    (error) => {
      if (isCancel(error)) return;
      showFallback("Share sheet: failed (" + String(error && error.name) + "). Use Download .m4a instead.");
    },
  );
});

// A looping preview should not keep playing from a background tab.
document.addEventListener("visibilitychange", () => {
  if (document.hidden && state.playing !== null) stopPlayback();
});

// --- start-up ---------------------------------------------------------------------

function init() {
  el.maxClipSeconds.textContent = String(config.maxClipSeconds);
  // FETCH_ENABLED only reveals the box: the fetcher is a separate image the
  // proxy routes /fetch/ to, and without it the box would only fail.
  el.youtube.hidden = !config.fetchEnabled;
  setStatus(el.fileStatus, fileHint());
  el.target.min = String(TARGET_MIN_LUFS);
  el.target.max = String(TARGET_MAX_LUFS);
  el.target.value = String(config.defaultTarget);
  el.targetBaseline.value = String(config.defaultTarget);
  // CSSOM, not a style attribute: the CSP's style-src 'self' allows this.
  const at = (config.defaultTarget - TARGET_MIN_LUFS) / (TARGET_MAX_LUFS - TARGET_MIN_LUFS);
  el.targetMarker.style.setProperty("--at", String(at));
  updateFades();
  updateLoudness();
  updateButtons();
  useSharedLink();
  // Sharing to the Shortcut again while the app is open in that tab changes
  // only the fragment, which loads nothing: without this the link would
  // stay in the address bar, unread.
  window.addEventListener("hashchange", useSharedLink);
}

init();
